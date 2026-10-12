#!/usr/bin/env python3

import argparse
import re
import struct
import sys

BASE_DEFAULT = 0x600

# ---------------------------------------------------------------------------
# Terminal colors
# ---------------------------------------------------------------------------


class C:
    RESET = "\033[0m"
    BOLD = "\033[1m"
    DIM = "\033[2m"
    RED = "\033[31m"
    GREEN = "\033[32m"
    YELLOW = "\033[33m"
    BLUE = "\033[34m"
    MAGENTA = "\033[35m"
    CYAN = "\033[36m"
    GREY = "\033[90m"


def _disable_colors():
    for name in dir(C):
        if not name.startswith("_"):
            setattr(C, name, "")


def _enable_windows_ansi():
    if sys.platform != "win32":
        return
    try:
        import ctypes
        k = ctypes.windll.kernel32
        k.SetConsoleMode(k.GetStdHandle(-11), 7)
    except Exception:  # noqa: BLE001
        pass


# ---------------------------------------------------------------------------
# Union overlay helpers (little-endian)
# ---------------------------------------------------------------------------


class Frame:
    """Little-endian union view over 8 CAN data bytes."""

    def __init__(self, data: bytes):
        if len(data) < 8:
            data = data + b"\x00" * (8 - len(data))
        data = data[:8]
        self.raw = data
        self.u8 = list(data)
        self.s8 = list(struct.unpack("<8b", data))
        self.u16 = list(struct.unpack("<4H", data))
        self.s16 = list(struct.unpack("<4h", data))
        self.u32 = list(struct.unpack("<2I", data))
        self.s32 = list(struct.unpack("<2i", data))
        self.fp = list(struct.unpack("<2f", data))


# ---------------------------------------------------------------------------
# Named lookups
# ---------------------------------------------------------------------------

PRECHARGE_STATE = {
    0: "Error",
    1: "Idle",
    2: "Measure",
    3: "Precharge",
    4: "Run",
    5: "Enable Pack",
}

# 0x6FB data_u8[4]  (8-bit status flags)
PACK_STATUS_FLAGS = [
    (0x01, "Cell Over Voltage"),
    (0x02, "Cell Under Voltage"),
    (0x04, "Cell Over Temperature"),
    (0x08, "Measurement Untrusted"),
    (0x10, "CMU Comms Timeout (lost CMU)"),
    (0x20, "Vehicle Comms Timeout (lost EVDC)"),
    (0x40, "BMU in Setup mode"),
    (0x80, "CMU CAN bus power status"),
]

# 0x6FD data_u32[0]  (32-bit extended status flags)
EXT_STATUS_FLAGS = [
    (0x00000001, "Cell Over Voltage"),
    (0x00000002, "Cell Under Voltage"),
    (0x00000004, "Cell Over Temperature"),
    (0x00000008, "Measurement Untrusted (channel mismatch)"),
    (0x00000010, "CMU Comms Timeout (lost CMU)"),
    (0x00000020, "Vehicle Comms Timeout (lost EVDC)"),
    (0x00000040, "BMU in Setup mode"),
    (0x00000080, "CMU CAN bus power status"),
    (0x00000100, "Pack Isolation test failure"),
    (0x00000200, "SOC measurement not valid"),
    (0x00000400, "CAN 12V supply low - about to shut down"),
    (0x00000800, "Contactor stuck / not engaged"),
    (0x00001000, "CMU detected an extra cell present"),
]

# 0x6F7 data_u8[0]  precharge contactor driver status bits
PRECHARGE_DRIVER_FLAGS = [
    (0x01, "Contactor 1 driver ERROR"),
    (0x02, "Contactor 2 driver ERROR"),
    (0x04, "Contactor 1 driver output ON"),
    (0x08, "Contactor 2 driver output ON"),
    (0x10, "12V contactor supply OK"),
    (0x20, "Contactor 3 driver ERROR"),
    (0x40, "Contactor 3 driver output ON"),
]

EVDC_SWITCH_FLAGS = [
    (0x0020, "Ignition RUN"),
    (0x0040, "Ignition START"),
]


def _flags(value, table):
    """Return list of active flag names for an integer bitfield."""
    return [name for bit, name in table if value & bit]


def _cell_voltage(mv):
    """Interpret a signed 16-bit cell voltage reading per section 5.2."""
    if mv == -32768:
        return None, "cell not present / not monitored"
    if mv == -32767:
        return None, "EXTRA CELL fault (voltage on 'not present' cell)"
    if mv < 0:
        return -mv, f"channel mismatch / trust error ({-mv} mV from slow channel)"
    return mv, "OK"


# ---------------------------------------------------------------------------
# Per-ID decoders.  Each returns a list of (label, value_str) rows.
# ---------------------------------------------------------------------------


def _dec_heartbeat(f: Frame):
    dev_id = f.u32[0]
    serial = f.u32[1]
    ascii_id = "".join(chr(b) if 32 <= b < 127 else "." for b in f.raw[0:4])
    rows = [
        ("Device ID", f"0x{dev_id:08X}  (v4 ASCII: '{ascii_id}')"),
        ("Serial number", f"{serial}  (0x{serial:08X})"),
    ]
    return "BMU Heartbeat & Serial Number", rows


def _dec_cmu_group0(f: Frame, cmu):
    return f"CMU {cmu} status/temperature", [
        ("CMU serial", f"{f.u32[0]}  (0x{f.u32[0]:08X})"),
        ("PCB temperature", f"{f.s16[2] / 10.0:.1f} °C"),
        ("Cell temperature", f"{f.s16[3] / 10.0:.1f} °C"),
    ]


def _dec_cmu_group1(f: Frame, cmu):
    rows = []
    for i in range(4):
        val, note = _cell_voltage(f.s16[i])
        vs = f"{val} mV" if val is not None else "---"
        rows.append((f"Cell {i} voltage", f"{vs}  [{note}]"))
    return f"CMU {cmu} cell voltages 0-3", rows


def _dec_cmu_group2(f: Frame, cmu):
    rows = []
    for i in range(4):
        val, note = _cell_voltage(f.s16[i])
        vs = f"{val} mV" if val is not None else "---"
        rows.append((f"Cell {i + 4} voltage", f"{vs}  [{note}]"))
    return f"CMU {cmu} cell voltages 4-7", rows


def _dec_pack_soc(f: Frame):
    return "Pack State of Charge (SOC)", [
        ("SOC consumed", f"{f.fp[0]:.3f} Ah  (0 = full)"),
        ("SOC percent", f"{f.fp[1]:.1f} %"),
    ]


def _dec_balance_soc(f: Frame):
    return "Pack Balance SOC", [
        ("Balance SOC", f"{f.fp[0]:.3f} Ah supplied since balancing began"),
        ("Balance percent", f"{f.fp[1]:.1f} % mismatch this session"),
    ]


def _dec_charger_control(f: Frame):
    return "Charger Control Information", [
        ("Charging cell V error", f"{f.s16[0]} mV  (BalanceThreshold - maxCell; target 0)"),
        ("Cell temp margin", f"{f.s16[1] / 10.0:.1f} °C  (maxTemp - limit; keep <0)"),
        ("Discharging cell V error", f"{f.s16[2]} mV  (ZeroSOCThreshold - minCell)"),
        ("Total pack capacity", f"{f.u16[3]} Ah"),
    ]


def _dec_precharge(f: Frame):
    drv = f.u8[0]
    state = f.u8[1]
    active = _flags(drv, PRECHARGE_DRIVER_FLAGS)
    return "Precharge Status", [
        ("Driver status", f"0x{drv:02X}  " + (", ".join(active) if active else "(none set)")),
        ("Precharge state", f"{state} = {PRECHARGE_STATE.get(state, 'Unknown')}"),
        ("12V supply (v4-)", f"{f.u16[1]} mV  (v5+: see 0x10 bit above)"),
        ("Timer elapsed", "YES" if f.u8[6] & 0x01 else "no"),
        ("Timer counter", f"{f.u8[7]} x10ms = {f.u8[7] * 10} ms"),
    ]


def _dec_minmax_voltage(f: Frame):
    return "Minimum / Maximum Cell Voltage", [
        ("Min cell voltage", f"{f.u16[0]} mV  @ CMU {f.u8[4]} cell {f.u8[5]}"),
        ("Max cell voltage", f"{f.u16[1]} mV  @ CMU {f.u8[6]} cell {f.u8[7]}"),
    ]


def _dec_minmax_temp(f: Frame):
    return "Minimum / Maximum Cell Temperature", [
        ("Min cell temp", f"{f.s16[0] / 10.0:.1f} °C  @ CMU {f.u8[4]}"),
        ("Max cell temp", f"{f.s16[1] / 10.0:.1f} °C  @ CMU {f.u8[6]}"),
    ]


def _dec_pack_vi(f: Frame):
    return "Battery Pack Voltage / Current", [
        ("Pack voltage", f"{f.u32[0]} mV  ({f.u32[0] / 1000.0:.3f} V)"),
        ("Pack current", f"{f.s32[1]} mA  ({f.s32[1] / 1000.0:.3f} A)"),
    ]


def _dec_pack_status(f: Frame):
    flags = _flags(f.u8[4], PACK_STATUS_FLAGS)
    return "Battery Pack Status", [
        ("Balance thresh rising", f"{f.u16[0]} mV"),
        ("Balance thresh falling", f"{f.u16[1]} mV"),
        ("Status flags", f"0x{f.u8[4]:02X}  " + (", ".join(flags) if flags else "(none)")),
        ("CMU count", f"{f.u8[5]}"),
        ("BMU firmware build", f"{f.u16[3]}"),
    ]


def _dec_fan_status(f: Frame):
    return "Battery Pack Fan Status", [
        ("Fan 0 speed", f"{f.u16[0]} rpm"),
        ("Fan 1 speed", f"{f.u16[1]} rpm"),
        ("12V fans+contactors", f"{f.u16[2]} mA"),
        ("12V CMUs", f"{f.u16[3]} mA"),
    ]


def _dec_ext_status(f: Frame):
    flags = _flags(f.u32[0], EXT_STATUS_FLAGS)
    return "Extended Battery Pack Status", [
        ("Status flags", f"0x{f.u32[0]:08X}"),
        ("Active flags", ", ".join(flags) if flags else "(none)"),
        ("BMU HW version", f"{f.u8[4]}"),
        ("BMU model ID", f"{f.u8[5]}"),
    ]


def _dec_evdc(f: Frame):
    flags = _flags(f.u16[0], EVDC_SWITCH_FLAGS)
    return "EV Driver Controls Switch Position", [
        ("Switches", f"0x{f.u16[0]:04X}  " + (", ".join(flags) if flags else "(none)")),
    ]


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------


def decode_frame(can_id: int, data: bytes, base: int = BASE_DEFAULT):
    """Decode a single CAN frame.

    Returns a dict:
        {id, name, rows: [(label, value), ...], raw: bytes, known: bool}
    """
    f = Frame(data)
    off = can_id - base  # offset from BMU base ID

    name = None
    rows = None

    # EVDC incoming (fixed relative to EVDC base 0x500 -> 0x505 by default)
    if can_id == 0x505:
        name, rows = _dec_evdc(f)

    elif off == 0x000:  # 0x600
        name, rows = _dec_heartbeat(f)

    # CMU telemetry: 0x601 .. 0x6EF  (offset 0x01 .. 0xEF), groups of 3
    elif 0x001 <= off <= 0xEF:
        g = off - 0x001
        cmu = g // 3 + 1
        sub = g % 3
        if sub == 0:
            name, rows = _dec_cmu_group0(f, cmu)
        elif sub == 1:
            name, rows = _dec_cmu_group1(f, cmu)
        else:
            name, rows = _dec_cmu_group2(f, cmu)

    elif off == 0x0F4:
        name, rows = _dec_pack_soc(f)
    elif off == 0x0F5:
        name, rows = _dec_balance_soc(f)
    elif off == 0x0F6:
        name, rows = _dec_charger_control(f)
    elif off == 0x0F7:
        name, rows = _dec_precharge(f)
    elif off == 0x0F8:
        name, rows = _dec_minmax_voltage(f)
    elif off == 0x0F9:
        name, rows = _dec_minmax_temp(f)
    elif off == 0x0FA:
        name, rows = _dec_pack_vi(f)
    elif off == 0x0FB:
        name, rows = _dec_pack_status(f)
    elif off == 0x0FC:
        name, rows = _dec_fan_status(f)
    elif off == 0x0FD:
        name, rows = _dec_ext_status(f)
    elif off == 0x0F0:
        name, rows = "Factory config / calibration (reserved)", []
    elif 0x0F1 <= off <= 0x0F3:
        name, rows = "Reserved (future config system)", []
    elif off in (0x0FE, 0x0FF):
        name, rows = "Reserved (unused)", []

    known = name is not None
    if not known:
        name = "Unknown / not in TRI67.010"
        rows = []

    return {"id": can_id, "name": name, "rows": rows, "raw": f.raw, "known": known}


# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------


def format_decoded(d, color=True):
    hexbytes = " ".join(f"{b:02X}" for b in d["raw"])
    idcol = C.CYAN if d["known"] else C.RED
    lines = [
        f"{idcol}0x{d['id']:03X}{C.RESET}  "
        f"{C.BOLD}{d['name']}{C.RESET}  "
        f"{C.GREY}[{hexbytes}]{C.RESET}"
    ]
    for label, value in d["rows"]:
        lines.append(f"    {C.DIM}{label:<24}{C.RESET} {value}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Input parsing (CLI)
# ---------------------------------------------------------------------------

_HEXBYTE = re.compile(r"[0-9A-Fa-f]{2}")


def parse_line(line: str):
    """Parse one input line into (can_id, data_bytes) or None.

    Accepts:
        6FA 60 EA 00 00 F4 01 00 00
        0x6FA 60EA0000F4010000
        6FA#60EA0000F4010000            (candump)
        t6FA860EA0000F4010000           (SLCAN / Lawicel standard frame)
        T000006FA860EA0000F4010000      (SLCAN extended frame)
        (0.123456) can0 6FA#60EA0000F4010000
    """
    line = line.strip()
    if not line or line.startswith("#") or line.startswith(";"):
        return None

    # candump / hash style:  ID#DATA
    m = re.search(r"([0-9A-Fa-f]{1,3})#([0-9A-Fa-f]*)", line)
    if m:
        can_id = int(m.group(1), 16)
        data = bytes.fromhex(m.group(2))
        return can_id, data

    # SLCAN / Lawicel:  t<3-hex-id><1-dlc><data...>  or  T<8-hex-id><1-dlc><data...>
    m = re.match(r"^[tT]([0-9A-Fa-f]+)$", line)
    if m:
        body = m.group(1)
        idlen = 8 if line[0] == "T" else 3
        if len(body) >= idlen + 1:
            can_id = int(body[:idlen], 16)
            dlc = int(body[idlen], 16)
            data_hex = body[idlen + 1: idlen + 1 + dlc * 2]
            try:
                return can_id, bytes.fromhex(data_hex)
            except ValueError:
                return None

    # Whitespace separated: first token = ID, remaining = data bytes
    tokens = line.replace(",", " ").split()
    if not tokens:
        return None
    try:
        can_id = int(tokens[0], 16)
    except ValueError:
        return None

    rest = "".join(tokens[1:])
    if len(rest) % 2 != 0:
        # maybe ID and data were merged oddly; try to grab pairs
        rest = "".join(_HEXBYTE.findall(rest))
    try:
        data = bytes.fromhex(rest)
    except ValueError:
        return None
    return can_id, data


def main():
    ap = argparse.ArgumentParser(description="Decode Tritium BMS BMU->Vehicle CAN frames.")
    ap.add_argument("--base", default=hex(BASE_DEFAULT),
                    help="BMU CAN base ID in hex (default 0x600).")
    ap.add_argument("--no-color", action="store_true", help="Disable colored output.")
    ap.add_argument("frames", nargs="*",
                    help="Optional inline frame(s), e.g. \"6FA 60 EA 00 00 F4 01 00 00\". "
                         "If omitted, reads lines from stdin.")
    args = ap.parse_args()

    # Make sure the degree symbol etc. don't crash the Windows console.
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass

    if args.no_color or not sys.stdout.isatty():
        _disable_colors()
    else:
        _enable_windows_ansi()

    base = int(args.base, 16)

    def handle(line):
        parsed = parse_line(line)
        if not parsed:
            return
        can_id, data = parsed
        d = decode_frame(can_id, data, base=base)
        print(format_decoded(d))

    if args.frames:
        # Treat the joined args as one frame line.
        handle(" ".join(args.frames))
        return

    print(f"{C.DIM}Paste CAN frames (ID + 8 data bytes), one per line. "
          f"Ctrl+Z then Enter (Windows) / Ctrl+D (Unix) to end.{C.RESET}",
          file=sys.stderr)
    try:
        for line in sys.stdin:
            handle(line)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
