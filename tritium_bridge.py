#!/usr/bin/env python3

import re
import socket
import struct
import subprocess
import sys
import threading

from bms_decode import parse_line

MCAST_GROUP = "239.255.60.60"
MCAST_PORT = 4876
BRIDGE_MAGIC = b"Tritium"
HEADER_LEN = 16
RECORD_LEN = 14
BMS_BASE = 0x300  # this unit's configured CAN base ID


def parse_datagram(payload: bytes):
    """Parse one bridge datagram into a list of (can_id, flags, data) tuples.

    Returns [] if the datagram is too short or not a Tritium bridge packet.
    """
    if len(payload) < HEADER_LEN:
        return []
    # Validate the "Tritium" magic (bytes 1..8) so stray traffic on the
    # multicast port is never mis-parsed as CAN records.
    if payload[1:8] != BRIDGE_MAGIC:
        return []
    frames = []
    body = payload[HEADER_LEN:]
    off = 0
    while off + RECORD_LEN <= len(body):
        rec = body[off:off + RECORD_LEN]
        can_id = int.from_bytes(rec[0:4], "big")
        flags = rec[4]
        dlc = rec[5]
        data = rec[6:14]
        frames.append((can_id, flags, data))
        off += RECORD_LEN
    return frames


BMS_IP = "192.168.0.125"


def _same_subnet24(a, b):
    return a.split(".")[:3] == b.split(".")[:3]


def list_local_ipv4s():
    """Return the set of this machine's local IPv4 addresses.

    Combines three methods so EVERY adapter is found — multiple Ethernet
    ports and multiple Wi-Fi adapters alike, which matters for joining the
    multicast group on all of them.
    """
    ips = set()
    # 1) Hostname resolution (fast, but can miss some NICs on Windows).
    try:
        for res in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ips.add(res[4][0])
    except Exception:  # noqa: BLE001
        pass
    # 2) The "connect" trick reveals the routed source IP toward the BMS.
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect((BMS_IP, MCAST_PORT))
        ips.add(s.getsockname()[0])
        s.close()
    except Exception:  # noqa: BLE001
        pass
    # 3) Windows: parse `ipconfig` so we catch EVERY adapter (several
    #    Ethernet + several Wi-Fi), which methods 1/2 can miss.
    if sys.platform == "win32":
        try:
            out = subprocess.run(["ipconfig"], capture_output=True, text=True,
                                 timeout=5).stdout
            for m in re.finditer(r"IPv4 Address[.\s]*:\s*([0-9]+(?:\.[0-9]+){3})", out):
                ips.add(m.group(1))
        except Exception:  # noqa: BLE001
            pass
    ips.discard("0.0.0.0")
    ips.discard("127.0.0.1")
    return ips


def default_iface_ip(peer_ip=BMS_IP):
    """Pick the local interface IP on the same /24 subnet as the BMS.

    Falls back to the routed source IP, then 0.0.0.0 (OS chooses).
    """
    ips = list_local_ipv4s()
    for ip in ips:
        if _same_subnet24(ip, peer_ip):
            return ip
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect((peer_ip, MCAST_PORT))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:  # noqa: BLE001
        return "0.0.0.0"


def _open_bound_socket(port=MCAST_PORT):
    """UDP socket bound to all interfaces on the multicast port (no joins yet)."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
    except (AttributeError, OSError):
        pass  # not available / not needed on Windows
    sock.bind(("", port))
    sock.settimeout(1.0)
    return sock


def _rejoin(sock, ip, group=MCAST_GROUP):
    """(Re)join the multicast group on one interface.

    Drops any stale membership first, so an adapter that was unplugged and
    re-plugged (or renewed its DHCP lease) is joined cleanly again.
    """
    gaddr = socket.inet_aton(group)
    try:
        iaddr = socket.inet_aton(ip)
    except OSError:
        return False
    try:
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_DROP_MEMBERSHIP, gaddr + iaddr)
    except OSError:
        pass  # wasn't joined / interface gone — fine
    try:
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, gaddr + iaddr)
        return True
    except OSError:
        return False  # some virtual adapters can't join; ignore


def _join_everywhere(sock, iface_ip=None, group=MCAST_GROUP):
    """Join the group on every interface currently present.

    Covers the built-in Ethernet port, USB network adapters, Wi-Fi — whichever
    carries the BMS link — on a multi-adapter PC.
    """
    join_ips = set(list_local_ipv4s())
    if iface_ip and iface_ip != "0.0.0.0":
        join_ips.add(iface_ip)
    join_ips.add("0.0.0.0")  # default interface, as a fallback
    joined = 0
    for ip in join_ips:
        if _rejoin(sock, ip, group):
            joined += 1
    return joined


def open_multicast_socket(iface_ip=None, group=MCAST_GROUP, port=MCAST_PORT):
    """Create a UDP socket joined to the multicast group on all interfaces."""
    sock = _open_bound_socket(port)
    _join_everywhere(sock, iface_ip, group)
    return sock


class TritiumReceiver(threading.Thread):
    """Background thread: receive bridge datagrams and invoke a per-frame callback.

    on_frame(can_id, data)  is called for every CAN record received.
    """

    daemon = True

    def __init__(self, on_frame, iface_ip=None, group=MCAST_GROUP, port=MCAST_PORT):
        super().__init__()
        self.on_frame = on_frame
        self.iface_ip = iface_ip if iface_ip is not None else default_iface_ip()
        self.group = group
        self.port = port
        self.running = True
        self.sock = None

    def stop(self):
        self.running = False
        try:
            if self.sock:
                self.sock.close()
        except Exception:  # noqa: BLE001
            pass

    def run(self):
        self.sock = _open_bound_socket(self.port)
        _join_everywhere(self.sock, self.iface_ip, self.group)
        while self.running:
            try:
                payload, _addr = self.sock.recvfrom(65535)
            except socket.timeout:
                # Stream quiet for 1 s: the link may have moved (cable swapped
                # from the Ethernet port to a USB adapter, adapter plugged in
                # after launch, DHCP renewal, ...). Re-scan the interfaces and
                # re-join the group so reception resumes without a restart.
                if self.running:
                    _join_everywhere(self.sock, self.iface_ip, self.group)
                continue
            except OSError:
                break
            for can_id, flags, data in parse_datagram(payload):
                if flags & 0x80:
                    continue  # bridge's own heartbeat record, not a bus frame
                self.on_frame(can_id, data)


class UdpTextReceiver(threading.Thread):
    """Receive ASCII CAN frames over plain (unicast or broadcast) UDP.

    One frame per line, in any format bms_decode.parse_line understands
    (candump 'ID#DATA', SLCAN 't...', or spaced hex). Lets a second gateway
    that forwards over Wi-Fi / a different subnet feed the same dashboard.
    """

    daemon = True

    def __init__(self, on_frame, host="0.0.0.0", port=5000):
        super().__init__()
        self.on_frame = on_frame
        self.host = host
        self.port = port
        self.running = True
        self.sock = None

    def stop(self):
        self.running = False
        try:
            if self.sock:
                self.sock.close()
        except Exception:  # noqa: BLE001
            pass

    def run(self):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind((self.host, self.port))
        self.sock.settimeout(1.0)
        while self.running:
            try:
                data, _addr = self.sock.recvfrom(65535)
            except socket.timeout:
                continue
            except OSError:
                break
            text = data.decode("ascii", "replace")
            for line in text.replace("\r", "\n").split("\n"):
                line = line.strip()
                if not line:
                    continue
                parsed = parse_line(line)
                if parsed:
                    self.on_frame(parsed[0], parsed[1])


class MultiReceiver:
    """Run several receiver threads at once, all feeding one dashboard.

    Any mix of TritiumReceiver / UdpTextReceiver (multicast on every NIC,
    plain UDP on specific IPs, multiple groups/ports) can be combined so the
    data is picked up over Ethernet, Wi-Fi, or any configured connection.
    """

    def __init__(self, sources=None):
        self.sources = list(sources or [])

    def add(self, source):
        self.sources.append(source)
        return source

    def start(self):
        for s in self.sources:
            s.start()

    def stop(self):
        for s in self.sources:
            try:
                s.stop()
            except Exception:  # noqa: BLE001
                pass

    def is_alive(self):
        return any(s.is_alive() for s in self.sources)


RELAY_PORT = 4877


def _broadcast_dests():
    """One broadcast address per subnet this machine is on.

    Using subnet-directed broadcasts (e.g. 192.168.0.255) rather than also
    the limited 255.255.255.255 avoids delivering duplicate copies of every
    frame to hosts that would receive both. Falls back to the limited
    broadcast only if no interfaces could be enumerated.
    """
    dests = set()
    for ip in list_local_ipv4s():
        parts = ip.split(".")
        if len(parts) == 4:
            dests.add(".".join(parts[:3]) + ".255")
    return sorted(dests) if dests else ["255.255.255.255"]


class FrameRelay:
    """Rebroadcast received CAN frames over UDP so other people on the router
    (Wi-Fi or Ethernet) can pick them up — even when the router won't forward
    multicast to wireless clients.

    Each frame is re-emitted as one ASCII candump line 'ID#DATA' to the LAN
    broadcast address(es). Remote users run:
        python bms_dashboard_can.py --source udp:0.0.0.0:<port>:0x300
    """

    def __init__(self, port=RELAY_PORT, dests=None):
        self.port = port
        self.dests = dests if dests is not None else _broadcast_dests()
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        self.count = 0

    def send(self, can_id, data):
        line = f"{can_id:X}#{data.hex()}\n".encode("ascii")
        for d in self.dests:
            try:
                self.sock.sendto(line, (d, self.port))
            except OSError:
                pass
        self.count += 1

    def close(self):
        try:
            self.sock.close()
        except Exception:  # noqa: BLE001
            pass


if __name__ == "__main__":
    # Quick self-test: print decoded frames from the live stream for ~5 seconds.
    import time
    from bms_decode import decode_frame, format_decoded, _enable_windows_ansi
    _enable_windows_ansi()

    iface = default_iface_ip()
    print(f"Joining {MCAST_GROUP}:{MCAST_PORT} via interface {iface}, base 0x{BMS_BASE:03X}")

    seen = {}

    def show(can_id, data):
        # Print each distinct ID once so the output is readable.
        if can_id in seen:
            return
        seen[can_id] = True
        d = decode_frame(can_id, data, base=BMS_BASE)
        print(format_decoded(d))

    rx = TritiumReceiver(show, iface_ip=iface)
    rx.start()
    time.sleep(5)
    rx.stop()
    print(f"\nDone. Saw {len(seen)} distinct CAN IDs.")
