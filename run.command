#!/bin/bash
# ============================================================
#  Tritium BMS - WEB dashboard launcher (macOS)
#  Double-click in Finder to start (opens in Terminal), or run
#  from a terminal. Opens the dashboard in your browser.
#
#  Pass-through arguments work too, e.g.:
#      ./run.command --demo          (synthetic data, no hardware)
#      ./run.command --port 9000     (different port)
#      ./run.command --relay         (re-serve the stream to the LAN)
#
#  First time only: make it double-clickable with
#      chmod +x run.command
# ============================================================
cd "$(dirname "$0")" || exit 1

# Prefer python3 (standard on macOS via Homebrew / python.org).
if command -v python3 >/dev/null 2>&1; then
    PY=python3
elif command -v python >/dev/null 2>&1; then
    PY=python
else
    echo "[ERROR] Python 3 was not found."
    echo "Install it from https://www.python.org/downloads/ or:  brew install python"
    exit 1
fi

echo "Starting the Tritium BMS web dashboard..."
echo "(macOS may ask to allow incoming network connections - click Allow.)"
echo "Press Ctrl+C to stop."
echo
exec "$PY" bms_web.py --open "$@"
