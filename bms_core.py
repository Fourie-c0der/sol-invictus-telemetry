#!/usr/bin/env python3

import random
import struct
import threading
import time

from bms_decode import Frame, PRECHARGE_STATE  # noqa: F401  (re-exported)

BASE_DEFAULT = 0x600

# Short names for the 8-bit pack status flags (0x_FB data_u8[4]).
PACK_FLAG_SHORT = [
    (0x01, "OVER V"),
    (0x02, "UNDER V"),
    (0x04, "OVER TEMP"),
    (0x08, "UNTRUSTED"),
    (0x10, "CMU TIMEOUT"),
    (0x20, "EVDC TIMEOUT"),
    (0x40, "SETUP MODE"),
    (0x80, "CMU PWR OK"),
]


# ===========================================================================
# Shared telemetry state (updated by data thread, read by HTTP threads)
# ===========================================================================


class BmsState:
    """Thread-safe accumulator of the latest decoded BMS telemetry.

    Ported verbatim (logic-wise) from bms_dashboard.BmsState so the web view is
    consistent with the desktop decoder.
    """

    def __init__(self):
        self.lock = threading.Lock()
        self.frame_count = 0
        self.last_rx = 0.0

        # BMU heartbeat
        self.bmu_serial = None
        self.device_id = None

        # min/max voltage
        self.min_mv = None
        self.max_mv = None
        self.min_v_cmu = self.min_v_cell = None
        self.max_v_cmu = self.max_v_cell = None

        # min/max temperature
        self.min_c = self.max_c = None
        self.min_c_cmu = self.max_c_cmu = None

        # pack voltage/current
        self.pack_mv = None
        self.pack_ma = None

        # pack status
        self.bal_rising = None
        self.bal_falling = None
        self.status_flags = None
        self.cmu_count = None
        self.fw_build = None

        # fans
        self.fan0 = self.fan1 = None

        # SOC / balance
        self.soc_ah = self.soc_pct = None
        self.bal_ah = self.bal_pct = None

        # charger control (pack capacity)
        self.capacity_ah = None

        # precharge
        self.prechg_state = None

        # CMUs: {cmu_num: {'serial','pcb','cell','cells':[8]}}
        self.cmus = {}

    @staticmethod
    def _cellval(mv):
        """Interpret a signed cell voltage. None = cell not present."""
        if mv in (-32768, -32767):
            return None
        return -mv if mv < 0 else mv

    def update_from_frame(self, can_id, data, base=BASE_DEFAULT):
        f = Frame(data)
        off = can_id - base
        with self.lock:
            self.frame_count += 1
            self.last_rx = time.time()

            if off == 0x000:
                self.device_id = f.u32[0]
                self.bmu_serial = f.u32[1]
            elif 0x001 <= off <= 0x0EF:
                g = off - 0x001
                cmu = g // 3 + 1
                sub = g % 3
                c = self.cmus.setdefault(
                    cmu, {"serial": None, "pcb": None, "cell": None,
                          "cells": [None] * 8}
                )
                if sub == 0:
                    c["serial"] = f.u32[0]
                    c["pcb"] = f.s16[2] / 10.0
                    c["cell"] = f.s16[3] / 10.0
                elif sub == 1:
                    for i in range(4):
                        c["cells"][i] = self._cellval(f.s16[i])
                else:
                    for i in range(4):
                        c["cells"][i + 4] = self._cellval(f.s16[i])
            elif off == 0x0F4:
                self.soc_ah, self.soc_pct = f.fp[0], f.fp[1]
            elif off == 0x0F5:
                self.bal_ah, self.bal_pct = f.fp[0], f.fp[1]
            elif off == 0x0F6:
                self.capacity_ah = f.u16[3]
            elif off == 0x0F7:
                self.prechg_state = f.u8[1]
            elif off == 0x0F8:
                self.min_mv, self.max_mv = f.u16[0], f.u16[1]
                self.min_v_cmu, self.min_v_cell = f.u8[4], f.u8[5]
                self.max_v_cmu, self.max_v_cell = f.u8[6], f.u8[7]
            elif off == 0x0F9:
                self.min_c, self.max_c = f.s16[0] / 10.0, f.s16[1] / 10.0
                self.min_c_cmu, self.max_c_cmu = f.u8[4], f.u8[6]
            elif off == 0x0FA:
                self.pack_mv, self.pack_ma = f.u32[0], f.s32[1]
            elif off == 0x0FB:
                self.bal_rising, self.bal_falling = f.u16[0], f.u16[1]
                self.status_flags = f.u8[4]
                self.cmu_count = f.u8[5]
                self.fw_build = f.u16[3]
            elif off == 0x0FC:
                self.fan0, self.fan1 = f.u16[0], f.u16[1]

    def snapshot(self):
        with self.lock:
            snap = dict(self.__dict__)
            snap.pop("lock", None)
            snap["cmus"] = {
                k: {"serial": v["serial"], "pcb": v["pcb"],
                    "cell": v["cell"], "cells": list(v["cells"])}
                for k, v in self.cmus.items()
            }
            return snap


# ===========================================================================
# Demo data source (no hardware required)
# ===========================================================================


def _enc_cmu_group0(serial, pcb_c, cell_c):
    return struct.pack("<I", serial) + struct.pack(
        "<hh", int(pcb_c * 10), int(cell_c * 10)
    )


def _enc_cmu_cells(cells4):
    return struct.pack("<4h", *[(-32768 if c is None else int(c)) for c in cells4])


class DemoSource(threading.Thread):
    """Generate realistic frames locally, with live jitter, so the dashboard can
    be verified without a BMS attached. Mirrors the reference screenshot."""

    daemon = True

    def __init__(self, state, base=BASE_DEFAULT, hz=10):
        super().__init__()
        self.state = state
        self.base = base
        self.period = 1.0 / hz
        self.running = True
        self.cmu_nom = {
            1: {"serial": 33693, "pcb": 21.8, "cell": 10.1,
                "cells": [3725, 3721, 3755, 3801, 3900, 3919, 3994, None]},
            2: {"serial": 33692, "pcb": 22.6, "cell": 11.2,
                "cells": [3726, 3724, 3784, 3767, 3904, 3912, 3976, None]},
        }

    def stop(self):
        self.running = False

    def run(self):
        b = self.base
        s = self.state
        while self.running:
            live = {}
            all_cells = []  # (cmu, cell_idx, mv)
            for n, nom in self.cmu_nom.items():
                cells = []
                for i, c in enumerate(nom["cells"]):
                    if c is None:
                        cells.append(None)
                    else:
                        cells.append(c + random.randint(-2, 2))
                        all_cells.append((n, i, cells[-1]))
                live[n] = {
                    "serial": nom["serial"],
                    "pcb": nom["pcb"] + random.uniform(-0.2, 0.2),
                    "cell": nom["cell"] + random.uniform(-0.2, 0.2),
                    "cells": cells,
                }

            for n, d in live.items():
                cid = b + 0x001 + (n - 1) * 3
                s.update_from_frame(cid, _enc_cmu_group0(d["serial"], d["pcb"], d["cell"]), b)
                s.update_from_frame(cid + 1, _enc_cmu_cells(d["cells"][0:4]), b)
                s.update_from_frame(cid + 2, _enc_cmu_cells(d["cells"][4:8]), b)

            mn = min(all_cells, key=lambda x: x[2])
            mx = max(all_cells, key=lambda x: x[2])
            s.update_from_frame(
                b + 0x0F8,
                struct.pack("<HH", mn[2], mx[2]) + bytes([mn[0], mn[1], mx[0], mx[1]]),
                b,
            )
            s.update_from_frame(
                b + 0x0F9,
                struct.pack("<hh", 101, 111) + bytes([1, 0, 2, 0]),
                b,
            )
            pack_mv = 53013 + random.randint(-40, 40)
            pack_ma = 656 + random.randint(-30, 30)
            s.update_from_frame(b + 0x0FA, struct.pack("<Ii", pack_mv, pack_ma), b)
            s.update_from_frame(
                b + 0x0FB,
                struct.pack("<HH", 4150, 4145) + bytes([0x80, len(live)]) + struct.pack("<H", 1234),
                b,
            )
            s.update_from_frame(b + 0x0FC, struct.pack("<4H", 0, 0, 0, 0), b)
            # Charger control: total pack capacity (Ah) in data_u16[3].
            s.update_from_frame(b + 0x0F6, struct.pack("<4H", 0, 0, 0, 51), b)
            s.update_from_frame(b + 0x0F4, struct.pack("<2f", -47.99, 100.0), b)
            s.update_from_frame(b + 0x0F5, struct.pack("<2f", 0.0, 0.0), b)
            s.update_from_frame(b + 0x0F7, bytes([0x14, 4, 0, 0, 0, 0, 0, 0]), b)

            time.sleep(self.period)


# ===========================================================================
# Derived values (battery meter + alarms) — GUI-free ports
# ===========================================================================

# Defaults for the voltage-based charge estimate. Tunable at runtime.
BATTERY_DEFAULTS = {
    "empty_mv": 3000,        # cell voltage treated as 0 %
    "full_mv": 4150,         # fallback 'full' if balance threshold unknown
    "full_override": None,   # None -> use BMS balance threshold when known
    "discharge_positive": True,
    "curr_deadband_ma": 100,
}

ALARM_DEFAULTS = {
    "cell_hi_mv": 4150,
    "cell_lo_mv": 2900,
    "temp_hi_c": 55.0,
}


def _cells_of(snap):
    return [v for c in snap["cmus"].values() for v in (c["cells"] or []) if v is not None]


def compute_battery(snap, opts=None):
    """Voltage-based state-of-charge, power and energy estimate.

    Ported from bms_dashboard_can.BatteryMeter._compute so the web gauge matches
    the desktop one. Any field may be None when data is missing.
    """
    o = dict(BATTERY_DEFAULTS)
    if opts:
        o.update(opts)

    cells = _cells_of(snap)
    avg_mv = sum(cells) / len(cells) if cells else None
    sum_mv = sum(cells) if cells else None

    full_mv = o["full_override"] or snap.get("bal_rising") or o["full_mv"]
    empty_mv = o["empty_mv"]
    soc_v = None
    if avg_mv is not None and full_mv > empty_mv:
        soc_v = max(0.0, min(100.0, (avg_mv - empty_mv) / (full_mv - empty_mv) * 100.0))

    pack_mv, pack_ma = snap.get("pack_mv"), snap.get("pack_ma")
    if pack_mv:
        pack_v = pack_mv / 1000.0
    elif sum_mv:
        pack_v = sum_mv / 1000.0
    else:
        pack_v = None

    power_w = None
    if pack_mv and pack_ma is not None:
        power_w = (pack_mv / 1000.0) * (pack_ma / 1000.0)

    cap = snap.get("capacity_ah")
    remaining_ah = (cap * soc_v / 100.0) if (cap and soc_v is not None) else None
    energy_kwh = (remaining_ah * pack_v / 1000.0) if (remaining_ah is not None and pack_v) else None

    direction = None
    if pack_ma is not None:
        signed = pack_ma if o["discharge_positive"] else -pack_ma
        if signed > o["curr_deadband_ma"]:
            direction = "DISCHARGING"
        elif signed < -o["curr_deadband_ma"]:
            direction = "CHARGING"
        else:
            direction = "IDLE"

    return {
        "soc_v": soc_v,
        "avg_mv": avg_mv,
        "pack_v": pack_v,
        "power_w": power_w,
        "remaining_ah": remaining_ah,
        "capacity_ah": cap,
        "energy_kwh": energy_kwh,
        "direction": direction,
        "soc_bms": snap.get("soc_pct"),
        "full_mv": full_mv,
        "empty_mv": empty_mv,
    }


def eval_alarms(snap, thresholds=None):
    """Return a list of active alarm strings and the set of CMUs in alarm.

    Ported from bms_dashboard_can.CanDashboard._eval_alarms.
    """
    t = dict(ALARM_DEFAULTS)
    if thresholds:
        t.update(thresholds)

    alarms, cmus = [], set()
    hi, lo, thc = t["cell_hi_mv"], t["cell_lo_mv"], t["temp_hi_c"]
    for n, c in snap["cmus"].items():
        for i, v in enumerate(c["cells"] or []):
            if v is None:
                continue
            if v >= hi:
                alarms.append(f"CMU{n} C{i} OVER-V {v}mV")
                cmus.add(n)
            elif v <= lo:
                alarms.append(f"CMU{n} C{i} UNDER-V {v}mV")
                cmus.add(n)
        ct = c.get("cell")
        if ct is not None and ct >= thc:
            alarms.append(f"CMU{n} OVER-TEMP {ct:.1f}C")
            cmus.add(n)

    flags = snap.get("status_flags")
    if flags:
        for bit, name in PACK_FLAG_SHORT:
            if bit != 0x80 and (flags & bit):
                alarms.append(f"BMS: {name}")

    return alarms, sorted(cmus)


def fill_minmax_from_cells(snap):
    """Populate min/max cell voltage + location from the per-CMU cell readings.

    Live frames carry these directly (0x_F8); replayed CSV rows do not, so we
    derive them here so the web view highlights the right rows in both cases.
    Only fills fields that are currently None.
    """
    best_min = best_max = None  # (mv, cmu, cell)
    for n, c in snap["cmus"].items():
        for i, v in enumerate(c["cells"] or []):
            if v is None:
                continue
            if best_min is None or v < best_min[0]:
                best_min = (v, n, i)
            if best_max is None or v > best_max[0]:
                best_max = (v, n, i)
    if best_min:
        if snap.get("min_mv") is None:
            snap["min_mv"] = best_min[0]
        if snap.get("min_v_cmu") is None:
            snap["min_v_cmu"], snap["min_v_cell"] = best_min[1], best_min[2]
    if best_max:
        if snap.get("max_mv") is None:
            snap["max_mv"] = best_max[0]
        if snap.get("max_v_cmu") is None:
            snap["max_v_cmu"], snap["max_v_cell"] = best_max[1], best_max[2]
    return snap
