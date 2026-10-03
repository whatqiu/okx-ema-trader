@echo off
rem Desktop app: native window, no console, automatic free port.
rem Keep this file ASCII-only (cmd.exe reads .bat as GBK).
cd /d "%~dp0platform\backend"
"%~dp0.venv\Scripts\pythonw.exe" desktop.py
if errorlevel 1 (
    echo Failed to start. Trying console mode for diagnostics...
    "%~dp0.venv\Scripts\python.exe" desktop.py
    pause
)
