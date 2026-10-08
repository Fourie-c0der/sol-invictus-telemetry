@echo off
REM ============================================================
REM  Tritium BMS - WEB dashboard launcher (Windows)
REM  Double-click to start. Opens the dashboard in your browser.
REM
REM  Any extra arguments are passed through, e.g.:
REM      run.bat --demo            (synthetic data, no hardware)
REM      run.bat --port 9000       (use a different port)
REM      run.bat --relay           (also re-serve the stream to the LAN)
REM ============================================================
cd /d "%~dp0"

REM ---- Find Python (python, then the py launcher) ----
set "PY=python"
where python >nul 2>&1
if errorlevel 1 (
    where py >nul 2>&1
    if errorlevel 1 (
        echo [ERROR] Python was not found on PATH.
        echo Install Python 3 from https://www.python.org/downloads/ and
        echo tick "Add Python to PATH" during installation.
        echo.
        pause
        exit /b 1
    )
    set "PY=py"
)

echo Starting the Tritium BMS web dashboard...
echo (A Windows Firewall prompt may appear the first time - click "Allow".)
echo Close this window to stop the server.
echo.
%PY% bms_web.py --open %*

echo.
echo Dashboard stopped.
pause
