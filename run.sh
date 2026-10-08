#!/bin/bash
# ============================================================
#  Tritium BMS - WEB dashboard launcher (Linux / Raspberry Pi)
#
#  Usage:
#      chmod +x run.sh        # first time only
#      ./run.sh               # live BMS stream, dashboard on :8080
#      ./run.sh --demo        # synthetic data (no hardware needed)
#      ./run.sh --port 9000   # different port
#      ./run.sh --relay       # also re-serve the stream to the LAN
#
#  On a headless Pi it prints the LAN URL to open from another
#  device (phone / laptop). Run at boot via systemd/cron if wanted.
# ============================================================
cd "$(dirname "$0")" || exit 1

if command -v python3 >/dev/null 2>&1; then
    PY=python3
elif command -v python >/dev/null 2>&1; then
    PY=python
else
    echo "[ERROR] Python 3 was not found."
    echo "Install it with:  sudo apt install python3    (Raspberry Pi OS / Debian)"
    exit 1
fi

echo "Starting the Tritium BMS web dashboard..."
echo "Open the http://... URL shown below from any device on the network."
echo "Press Ctrl+C to stop."
echo
exec "$PY" bms_web.py "$@"
