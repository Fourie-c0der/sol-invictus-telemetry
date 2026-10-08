# Tritium BMS — Web Dashboard

A browser-based, cross-platform telemetry dashboard for the Tritium BMS
(BMU → Vehicle CAN protocol, TRI67.010). It replaces the Windows-only Tkinter
desktop app with a small web server that runs the same on **Windows, macOS and a
Raspberry Pi**, and can be viewed from **any device** on the network — phone,
tablet or laptop — just by opening a URL.

It reads:

- **Live data** from the Tritium CAN-Ethernet multicast bridge (or plain UDP / a
  LAN relay), decoded with the same `bms_decode` logic as the desktop tools, and
- **Logged data** — it browses and replays the CSV files in `logs/` with a
  play/scrub timeline.

No external packages are required — **Python 3 standard library only** (no
`pip install`). If you can run `python`, you can run this.

---

## Quick start

### Windows
Double-click **`run.bat`** (or in a terminal):

```bash
python bms_web.py --open
```

### macOS
Double-click **`run.command`** (first time: `chmod +x run.command`), or:

```bash
python3 bms_web.py --open
```

### Raspberry Pi / Linux
```bash
chmod +x run.sh        # first time only
./run.sh
```

Then open the URL it prints — **http://localhost:8080** on the same machine, or
the **http://192.168.x.x:8080** LAN URL from another device.

### Try it without hardware (demo mode)
```bash
python bms_web.py --demo --open
```
This generates realistic synthetic telemetry so you can see everything working.

---

## What you get

- **Pack charge & power** — a voltage-based state-of-charge gauge plus pack
  voltage, power (kW), energy remaining (kWh), charge remaining (Ah) and
  charge/discharge state. (Same estimate as the desktop app.)
- **Pack telemetry tiles** — min/max cell voltage (with CMU:cell location), min/max
  temperature, pack V/A, balance thresholds, CMU count, precharge state, status
  flags, fans, SOC, balance SOC, capacity, firmware build.
- **Live trend charts** — cell voltage (min/max), pack voltage, pack current.
- **CMU cell table** — per-CMU serial, PCB/cell temperature and every cell
  voltage; the pack min/max cells are highlighted and any CMU in alarm turns red.
  Columns for cells that aren't fitted are hidden automatically.
- **Alarms** — cell over/under-voltage, over-temperature and BMS fault flags
  raise a banner (and an optional in-browser beep — click 🔇 to enable).
- **Replay tab** — pick any CSV log and scrub / play it back at 1×–60×.
- **Light / dark theme** toggle (dark by default, good in a vehicle).
- **CSV logging** — one row per second to `logs/bms_<timestamp>.csv`, in the
  **exact same format** as the original tools, so old and new logs are
  interchangeable.

---

## Multiple viewers (replacing run_relay_server / run_viewer)

With the web dashboard you usually don't need the separate relay/viewer split:
run it on the machine wired to the BMS and **everyone else just opens the URL** in
a browser. The BMS is only touched by the one server, so nobody gets kicked off
the bridge.

If some viewers are on a different network segment that can't reach this machine's
web port, you can still rebroadcast the raw CAN stream the old way:

```bash
python bms_web.py --relay                      # broadcast to this LAN
python bms_web.py --relay-to 192.168.100.45    # and/or to specific IPs
```

---

## Command-line options

```
python bms_web.py [options]

  --demo                 Serve synthetic demo data (no hardware).
  --port N               HTTP port (default 8080).
  --host ADDR            Bind address (default 0.0.0.0 = all interfaces).
  --open                 Open a browser window on start.

  --base 0xNNN           BMU CAN base ID for the default source (default 0x300).
  --iface IP             Local interface IP to join multicast on (default auto).
  --source SPEC          Add a data connection; repeatable. Examples:
                           tritium
                           mcast:239.255.60.60:4876
                           udp:0.0.0.0:4877:0x300
  --relay                Rebroadcast received frames over the LAN.
  --relay-to IP[,IP...]  Also relay directly to these IPs.
  --relay-port N         UDP relay port (default 4877).

  --logs-dir DIR         Folder for CSV logs / replay (default ./logs).
  --csv PATH             CSV log path (default logs/bms_<timestamp>.csv).
  --no-csv               Disable CSV logging.
  --verbose              Verbose HTTP request logging.
```

Defaults match this system: the live BMS multicast stream
(`239.255.60.60:4876`, base `0x300`), auto-detected interface, logging on.

---

## Files

| File | Purpose |
|------|---------|
| `bms_web.py` | The web server: HTTP + JSON API + live sources + CSV logging + replay. |
| `bms_core.py` | GUI-free telemetry state, demo source, battery/alarm calculations. |
| `bms_decode.py` | Shared CAN frame decoder (TRI67.010). *Copied from the original.* |
| `tritium_bridge.py` | Multicast/UDP receivers + LAN relay. *Copied from the original.* |
| `web/` | The dashboard front-end (HTML/CSS/JS, no external dependencies). |
| `logs/` | CSV logs (live logging writes here; the Replay tab reads from here). |
| `run.bat` / `run.command` / `run.sh` | One-click launchers for each platform. |

The two *copied* modules are unchanged from `TRITUMCODE-updated`, so decoding is
identical to the desktop dashboards.

---

## JSON API (for scripting / other tools)

| Endpoint | Returns |
|----------|---------|
| `GET /api/state` | Current telemetry snapshot + derived battery/alarms + connection status. |
| `GET /api/history` | Recent time series for the charts. |
| `GET /api/logs` | List of CSV logs in the logs folder. |
| `GET /api/log?name=<file>` | A log parsed into replay-ready samples. |
| `GET /api/config` | Source, mode, base ID and current settings. |
| `POST /api/settings` | Adjust battery limits / alarm thresholds live. |
| `POST /api/csv` | `{ "enabled": true|false }` to start/stop logging. |

---

## Notes

- **Firewall:** the first run may prompt to allow Python to accept network
  connections — allow it, so other devices can reach the dashboard and so the
  BMS multicast can be received.
- **Python version:** 3.7+ (developed/tested on 3.13). `tkinter` is **not**
  required, so this runs on a headless Pi.
- If no interface is on the BMS subnet (e.g. the cable is out), the dashboard
  starts anyway and connects automatically once the link appears — no restart
  needed. Use `--demo` to explore the UI in the meantime.
