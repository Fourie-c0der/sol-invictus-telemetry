#!/usr/bin/env python3

import argparse
import collections
import csv
import datetime
import json
import os
import socket
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

from bms_core import (
    BmsState, DemoSource, PACK_FLAG_SHORT, PRECHARGE_STATE,
    compute_battery, eval_alarms, fill_minmax_from_cells,
    BATTERY_DEFAULTS, ALARM_DEFAULTS,
)

HERE = os.path.dirname(os.path.abspath(__file__))
WEB_DIR = os.path.join(HERE, "web")

# Scalar telemetry fields carried in the CSV log (same schema as the originals,
# so the files this server writes are readable by the old tools and vice-versa).
CSV_FIELDS = [
    "pack_mv", "pack_ma", "min_mv", "max_mv", "min_c", "max_c",
    "soc_ah", "soc_pct", "bal_ah", "bal_pct", "capacity_ah",
    "cmu_count", "prechg_state", "status_flags",
]

HISTORY_MAXLEN = 1200          # ~10 min at 2 Hz
HISTORY_HZ = 2.0               # history sample rate
CSV_HZ = 1.0                   # CSV row rate
MAX_REPLAY_SAMPLES = 4000      # downsample larger logs when replaying


# ===========================================================================
# Hub: owns the shared state, history, settings and CSV logger
# ===========================================================================


class Hub:
    def __init__(self, state, source_label, mode, base, logs_dir, csv_path):
        self.state = state
        self.source_label = source_label
        self.mode = mode
        self.base = base
        self.logs_dir = logs_dir

        self.lock = threading.Lock()
        self.history = collections.deque(maxlen=HISTORY_MAXLEN)
        self.settings = {
            "battery": dict(BATTERY_DEFAULTS),
            "alarms": dict(ALARM_DEFAULTS),
        }

        # CSV logging (schema identical to bms_dashboard_can.py)
        self.csv_path = csv_path
        self.csv_enabled = csv_path is not None
        self.csv_file = None
        self.csv_writer = None
        self.csv_cmus = None
        self._csv_grace = 0

        self._closing = False
        self._tick = 0

    # ---- settings ----------------------------------------------------------
    def get_settings(self):
        with self.lock:
            return json.loads(json.dumps(self.settings))  # deep copy

    def update_settings(self, patch):
        with self.lock:
            for group in ("battery", "alarms"):
                if group in patch and isinstance(patch[group], dict):
                    for k, v in patch[group].items():
                        if k in self.settings[group]:
                            self.settings[group][k] = v
        return self.get_settings()

    # ---- background sampler -------------------------------------------------
    def start_sampler(self):
        t = threading.Thread(target=self._sampler_loop, daemon=True)
        t.start()
        return t

    def _sampler_loop(self):
        period = 1.0 / HISTORY_HZ
        csv_every = max(1, round(HISTORY_HZ / CSV_HZ))
        while not self._closing:
            snap = self.state.snapshot()
            batt = compute_battery(snap, self.get_settings()["battery"])
            with self.lock:
                self.history.append({
                    "t": time.time(),
                    "min_mv": snap.get("min_mv"),
                    "max_mv": snap.get("max_mv"),
                    "pack_mv": snap.get("pack_mv"),
                    "pack_ma": snap.get("pack_ma"),
                    "soc_v": batt.get("soc_v"),
                })
            if self.csv_enabled and (self._tick % csv_every == 0):
                self._maybe_write_csv(snap)
            self._tick += 1
            time.sleep(period)

    def history_json(self):
        with self.lock:
            return list(self.history)

    # ---- CSV logging (ported from CanDashboard) ----------------------------
    def _csv_should_start(self, snap):
        if snap["frame_count"] <= 0:
            return False
        if snap["cmus"]:
            return True
        self._csv_grace += 1
        return self._csv_grace >= 10

    def _maybe_write_csv(self, snap):
        if self.csv_file is None and not self._csv_should_start(snap):
            return
        if self.csv_file is None:
            cmus = sorted(snap["cmus"])
            self.csv_cmus = cmus
            header = ["iso_time", "epoch"] + list(CSV_FIELDS)
            for n in cmus:
                header += [f"cmu{n}_serial", f"cmu{n}_pcb_c", f"cmu{n}_cell_c"]
                header += [f"cmu{n}_cell{i}_mv" for i in range(8)]
            os.makedirs(os.path.dirname(self.csv_path) or ".", exist_ok=True)
            self.csv_file = open(self.csv_path, "w", newline="", encoding="utf-8")
            self.csv_writer = csv.writer(self.csv_file)
            self.csv_writer.writerow(header)
            print(f"Logging to: {os.path.abspath(self.csv_path)}")

        now = datetime.datetime.now()
        row = [now.isoformat(timespec="milliseconds"), f"{now.timestamp():.3f}"]
        row += [snap.get(k) for k in CSV_FIELDS]
        for n in self.csv_cmus:
            c = snap["cmus"].get(n, {})
            cells = c.get("cells") or [None] * 8
            row += [c.get("serial"), c.get("pcb"), c.get("cell")] + list(cells[:8])
        self.csv_writer.writerow(row)
        self.csv_file.flush()

    def toggle_csv(self, enabled):
        with self.lock:
            if enabled and not self.csv_enabled:
                stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
                self.csv_path = os.path.join(self.logs_dir, f"bms_{stamp}.csv")
                self.csv_cmus = None
                self._csv_grace = 0
                self.csv_enabled = True
            elif not enabled and self.csv_enabled:
                self.csv_enabled = False
                self._csv_close()
        return self.csv_enabled

    def _csv_close(self):
        try:
            if self.csv_file:
                self.csv_file.close()
        except Exception:  # noqa: BLE001
            pass
        self.csv_file = None
        self.csv_writer = None

    def close(self):
        self._closing = True
        self._csv_close()


# ===========================================================================
# State -> view-model JSON (shared by live + replay)
# ===========================================================================


def snapshot_to_view(snap, settings):
    """Turn a raw state snapshot into the JSON the browser renders.

    Used for both the live stream and each replayed CSV row, so the front-end
    has exactly one rendering path.
    """
    batt = compute_battery(snap, settings["battery"])
    alarms, alarm_cmus = eval_alarms(snap, settings["alarms"])

    flags = snap.get("status_flags")
    flag_names = []
    if flags is not None:
        flag_names = [name for bit, name in PACK_FLAG_SHORT if flags & bit]

    prechg = snap.get("prechg_state")
    prechg_name = PRECHARGE_STATE.get(prechg) if prechg is not None else None

    cmus = []
    for n in sorted(snap["cmus"]):
        c = snap["cmus"][n]
        cmus.append({
            "n": n,
            "serial": c.get("serial"),
            "pcb": c.get("pcb"),
            "cell": c.get("cell"),
            "cells": list(c.get("cells") or [None] * 8),
        })

    pack = {k: snap.get(k) for k in (
        "pack_mv", "pack_ma", "min_mv", "max_mv", "min_c", "max_c",
        "min_v_cmu", "min_v_cell", "max_v_cmu", "max_v_cell",
        "min_c_cmu", "max_c_cmu", "soc_ah", "soc_pct", "bal_ah", "bal_pct",
        "capacity_ah", "cmu_count", "prechg_state", "status_flags",
        "bal_rising", "bal_falling", "fan0", "fan1", "fw_build",
        "bmu_serial", "device_id",
    )}
    pack["prechg_name"] = prechg_name
    pack["status_flag_names"] = flag_names

    return {
        "ts": snap.get("last_rx") or None,
        "frame_count": snap.get("frame_count", 0),
        "pack": pack,
        "battery": batt,
        "alarms": {"list": alarms, "cmus": alarm_cmus},
        "cmus": cmus,
    }


def connection_status(age_s):
    if age_s is None:
        return "nodata"
    if age_s < 2:
        return "live"
    if age_s < 6:
        return "slow"
    return "stale"


# ===========================================================================
# CSV replay
# ===========================================================================


def _num(s):
    if s is None:
        return None
    s = s.strip()
    if s == "":
        return None
    try:
        return int(s)
    except ValueError:
        pass
    try:
        return float(s)
    except ValueError:
        return s


def load_log(path, settings):
    """Parse a CSV log into a list of view-model samples (downsampled if large)."""
    with open(path, "r", newline="", encoding="utf-8") as fh:
        reader = csv.reader(fh)
        header = next(reader, None)
        if not header:
            return {"samples": [], "count": 0, "downsampled": False}
        rows = list(reader)

    # Which columns exist, and which CMUs are present.
    idx = {name: i for i, name in enumerate(header)}
    cmu_nums = sorted({
        int(name.split("_")[0][3:])
        for name in header
        if name.startswith("cmu") and "_serial" in name
    })

    total = len(rows)
    step = 1
    if total > MAX_REPLAY_SAMPLES:
        step = (total + MAX_REPLAY_SAMPLES - 1) // MAX_REPLAY_SAMPLES

    samples = []
    for r in range(0, total, step):
        row = rows[r]

        def cell(name):
            i = idx.get(name)
            return _num(row[i]) if i is not None and i < len(row) else None

        snap = {"cmus": {}, "frame_count": r + 1}
        for k in CSV_FIELDS:
            snap[k] = cell(k)
        for n in cmu_nums:
            cells = [cell(f"cmu{n}_cell{i}_mv") for i in range(8)]
            snap["cmus"][n] = {
                "serial": cell(f"cmu{n}_serial"),
                "pcb": cell(f"cmu{n}_pcb_c"),
                "cell": cell(f"cmu{n}_cell_c"),
                "cells": cells,
            }
        epoch = cell("epoch")
        snap["last_rx"] = epoch if isinstance(epoch, (int, float)) else None
        fill_minmax_from_cells(snap)

        view = snapshot_to_view(snap, settings)
        iso = None
        if "iso_time" in idx and idx["iso_time"] < len(row):
            iso = row[idx["iso_time"]] or None
        view["iso_time"] = iso
        samples.append(view)

    return {
        "samples": samples,
        "count": total,
        "returned": len(samples),
        "downsampled": step > 1,
        "step": step,
        "cmus": cmu_nums,
    }


def list_logs(logs_dir):
    out = []
    if not os.path.isdir(logs_dir):
        return out
    for name in sorted(os.listdir(logs_dir), reverse=True):
        if not name.lower().endswith(".csv"):
            continue
        p = os.path.join(logs_dir, name)
        try:
            st = os.stat(p)
        except OSError:
            continue
        out.append({
            "name": name,
            "size": st.st_size,
            "mtime": st.st_mtime,
        })
    return out


# ===========================================================================
# HTTP handler
# ===========================================================================

CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
    ".ico": "image/x-icon",
}


def make_handler(hub, verbose=False):
    class Handler(BaseHTTPRequestHandler):
        server_version = "TritiumBMSWeb/1.0"

        def log_message(self, fmt, *args):
            if verbose:
                super().log_message(fmt, *args)

        # -- helpers ---------------------------------------------------------
        def _send_json(self, obj, code=200):
            body = json.dumps(obj, allow_nan=False, default=_json_default).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def _send_file(self, path):
            ext = os.path.splitext(path)[1].lower()
            ctype = CONTENT_TYPES.get(ext, "application/octet-stream")
            try:
                with open(path, "rb") as fh:
                    body = fh.read()
            except OSError:
                self._send_text("Not found", 404)
                return
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def _send_text(self, text, code=200):
            body = text.encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        # -- routing ---------------------------------------------------------
        def do_HEAD(self):
            self.do_GET()

        def do_GET(self):
            u = urlparse(self.path)
            path = u.path
            if path == "/" or path == "/index.html":
                return self._send_file(os.path.join(WEB_DIR, "index.html"))
            if path.startswith("/api/"):
                return self._api_get(path, parse_qs(u.query))
            # static files from web/ (no traversal)
            rel = path.lstrip("/")
            safe = os.path.normpath(os.path.join(WEB_DIR, rel))
            if safe.startswith(WEB_DIR) and os.path.isfile(safe):
                return self._send_file(safe)
            return self._send_text("Not found", 404)

        def do_POST(self):
            u = urlparse(self.path)
            length = int(self.headers.get("Content-Length", 0) or 0)
            raw = self.rfile.read(length) if length else b""
            try:
                body = json.loads(raw.decode("utf-8")) if raw else {}
            except (ValueError, UnicodeDecodeError):
                return self._send_json({"error": "bad json"}, 400)

            if u.path == "/api/settings":
                return self._send_json(hub.update_settings(body))
            if u.path == "/api/csv":
                enabled = bool(body.get("enabled"))
                return self._send_json({"enabled": hub.toggle_csv(enabled)})
            return self._send_json({"error": "unknown endpoint"}, 404)

        def _api_get(self, path, qs):
            if path == "/api/state":
                snap = hub.state.snapshot()
                view = snapshot_to_view(snap, hub.get_settings())
                age = (time.time() - snap["last_rx"]) if snap.get("last_rx") else None
                view["source"] = hub.source_label
                view["mode"] = hub.mode
                view["base"] = f"0x{hub.base:03X}"
                view["csv_enabled"] = hub.csv_enabled
                view["connection"] = {
                    "status": connection_status(age),
                    "age_s": age,
                    "age_ms": (age * 1000.0) if age is not None else None,
                }
                return self._send_json(view)

            if path == "/api/history":
                return self._send_json({"history": hub.history_json(),
                                        "hz": HISTORY_HZ})

            if path == "/api/config":
                return self._send_json({
                    "source": hub.source_label,
                    "mode": hub.mode,
                    "base": f"0x{hub.base:03X}",
                    "settings": hub.get_settings(),
                    "csv_enabled": hub.csv_enabled,
                })

            if path == "/api/settings":
                return self._send_json(hub.get_settings())

            if path == "/api/logs":
                return self._send_json({"logs": list_logs(hub.logs_dir)})

            if path == "/api/log":
                name = (qs.get("name") or [""])[0]
                if not name or "/" in name or "\\" in name or ".." in name:
                    return self._send_json({"error": "bad name"}, 400)
                p = os.path.join(hub.logs_dir, name)
                if not os.path.isfile(p):
                    return self._send_json({"error": "not found"}, 404)
                try:
                    data = load_log(p, hub.get_settings())
                except Exception as e:  # noqa: BLE001
                    return self._send_json({"error": f"parse failed: {e}"}, 500)
                data["name"] = name
                return self._send_json(data)

            return self._send_json({"error": "unknown endpoint"}, 404)

    return Handler


def _json_default(o):
    # Make any stray non-serialisable value safe rather than 500-ing.
    return str(o)


# ===========================================================================
# Source wiring (live) — reuses tritium_bridge, mirrors bms_dashboard_can.py
# ===========================================================================


def build_live_sources(args, state):
    """Return (MultiReceiver, label, base, relay) for the live BMS stream."""
    from tritium_bridge import (
        TritiumReceiver, UdpTextReceiver, MultiReceiver, FrameRelay,
        default_iface_ip, list_local_ipv4s, _same_subnet24,
        MCAST_GROUP, MCAST_PORT, BMS_BASE, BMS_IP, RELAY_PORT,
    )

    base = int(args.base, 16) if args.base else BMS_BASE
    iface = args.iface or default_iface_ip()

    # Warn (don't fail) if no NIC is on the BMS subnet — matches the desktop app.
    local = list_local_ipv4s()
    if not any(_same_subnet24(ip, BMS_IP) for ip in local):
        print("=" * 62)
        print("  NOTE: no network interface is on the BMS subnet")
        print(f"  (need a 192.168.0.x address to reach {BMS_IP}).")
        print(f"  Local IPs found: {sorted(local) or '(none)'}")
        print("  The dashboard will connect automatically once the BMS link")
        print("  appears — no restart needed. (Use --demo to try it now.)")
        print("=" * 62)

    relay = None
    if args.relay or args.relay_to:
        dests = []
        if args.relay:
            from tritium_bridge import _broadcast_dests
            dests += _broadcast_dests()
        if args.relay_to:
            dests += [ip.strip() for ip in args.relay_to.split(",") if ip.strip()]
        relay = FrameRelay(port=args.relay_port, dests=dests)
        print(f"RELAY ON — re-serving frames on UDP {args.relay_port} to {', '.join(dests)}")

    def make_cb(b):
        if relay is None:
            return lambda cid, data: state.update_from_frame(cid, data, b)

        def cb(cid, data):
            state.update_from_frame(cid, data, b)
            relay.send(cid, data)
        return cb

    def build_one(spec):
        parts = spec.split(":")
        kind = parts[0].strip().lower()
        if kind in ("tritium", "mcast", "multicast"):
            group = parts[1] if len(parts) > 1 and parts[1] else MCAST_GROUP
            port = int(parts[2]) if len(parts) > 2 and parts[2] else MCAST_PORT
            b = int(parts[3], 16) if len(parts) > 3 and parts[3] else base
            src = TritiumReceiver(make_cb(b), iface_ip=iface, group=group, port=port)
            return src, f"mcast {group}:{port} @0x{b:03X}"
        if kind == "udp":
            host = parts[1] if len(parts) > 1 and parts[1] else "0.0.0.0"
            port = int(parts[2]) if len(parts) > 2 and parts[2] else 5000
            b = int(parts[3], 16) if len(parts) > 3 and parts[3] else 0x600
            src = UdpTextReceiver(make_cb(b), host=host, port=port)
            return src, f"udp {host}:{port} @0x{b:03X}"
        raise ValueError(f"unknown source kind {kind!r}")

    specs = args.source or ["tritium"]
    sources, labels = [], []
    for spec in specs:
        try:
            src, lbl = build_one(spec)
        except (ValueError, IndexError) as e:
            print(f"Ignoring bad --source {spec!r}: {e}")
            continue
        sources.append(src)
        labels.append(lbl)
    if not sources:
        src, lbl = build_one("tritium")
        sources, labels = [src], [lbl]

    label = "  +  ".join(labels)
    if relay:
        label += f"  [RELAY:{args.relay_port}]"
    return MultiReceiver(sources), label, base, relay


# ===========================================================================
# main
# ===========================================================================


def _lan_ips():
    try:
        from tritium_bridge import list_local_ipv4s
        return sorted(list_local_ipv4s())
    except Exception:  # noqa: BLE001
        return []


def main():
    ap = argparse.ArgumentParser(
        description="Cross-platform WEB dashboard for the Tritium BMS telemetry."
    )
    ap.add_argument("--host", default="0.0.0.0",
                    help="HTTP bind address (default 0.0.0.0 = all interfaces).")
    ap.add_argument("--port", type=int, default=8080,
                    help="HTTP port (default 8080).")
    ap.add_argument("--demo", action="store_true",
                    help="Serve synthetic demo data instead of the live BMS.")
    ap.add_argument("--iface", default=None,
                    help="Local interface IP to join multicast on (default auto).")
    ap.add_argument("--base", default=None,
                    help="BMU CAN base ID for the default source (default 0x300).")
    ap.add_argument("--source", action="append", default=None, metavar="SPEC",
                    help="Add a data connection; repeatable. e.g. 'tritium', "
                         "'mcast:239.255.60.60:4876', 'udp:0.0.0.0:4877:0x300'.")
    ap.add_argument("--relay", action="store_true",
                    help="Rebroadcast received frames over the LAN.")
    ap.add_argument("--relay-to", default=None, metavar="IP[,IP...]",
                    help="Also relay directly to these IPs (comma-separated).")
    ap.add_argument("--relay-port", type=int, default=4877,
                    help="UDP port to relay on (default 4877).")
    ap.add_argument("--logs-dir", default=os.path.join(HERE, "logs"),
                    help="Directory for CSV logs / replay (default ./logs).")
    ap.add_argument("--csv", default=None, help="CSV log path (default logs/bms_<ts>.csv).")
    ap.add_argument("--no-csv", action="store_true", help="Disable CSV logging.")
    ap.add_argument("--open", action="store_true", help="Open a browser on start.")
    ap.add_argument("--verbose", action="store_true", help="Verbose HTTP logging.")
    args = ap.parse_args()

    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass

    logs_dir = args.logs_dir
    os.makedirs(logs_dir, exist_ok=True)

    state = BmsState()

    # ---- data source ----
    if args.demo:
        base = int(args.base, 16) if args.base else 0x600
        source = DemoSource(state, base=base)
        label = f"DEMO (synthetic)  @0x{base:03X}"
        mode = "demo"
        relay = None
    else:
        source, label, base, relay = build_live_sources(args, state)
        mode = "live"

    # ---- CSV path ----
    if args.no_csv:
        csv_path = None
    elif args.csv:
        csv_path = args.csv
    else:
        stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        csv_path = os.path.join(logs_dir, f"bms_{stamp}.csv")

    hub = Hub(state, label, mode, base, logs_dir, csv_path)
    hub.start_sampler()
    source.start()

    handler = make_handler(hub, verbose=args.verbose)
    httpd = ThreadingHTTPServer((args.host, args.port), handler)

    url_host = "localhost" if args.host in ("0.0.0.0", "") else args.host
    print("=" * 62)
    print("  Tritium BMS  —  WEB dashboard")
    print("=" * 62)
    print(f"  Mode      : {mode.upper()}   ({label})")
    print(f"  CSV log   : {'OFF' if csv_path is None else csv_path}")
    print(f"  Open      : http://{url_host}:{args.port}")
    if args.host in ("0.0.0.0", ""):
        for ip in _lan_ips():
            print(f"              http://{ip}:{args.port}   (other devices on the LAN)")
    print("  Stop      : press Ctrl+C")
    print("=" * 62)

    if args.open:
        threading.Thread(
            target=lambda: (time.sleep(0.6), webbrowser.open(f"http://{url_host}:{args.port}")),
            daemon=True,
        ).start()

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down…")
    finally:
        hub.close()
        try:
            if hasattr(source, "stop"):
                source.stop()
        except Exception:  # noqa: BLE001
            pass
        if relay is not None:
            relay.close()
        httpd.shutdown()
        print("Web dashboard closed.")


if __name__ == "__main__":
    main()
