@echo off
rem ASCII only on purpose: "chcp 65001" inside a UTF-8 .bat makes cmd mis-parse
rem the Chinese lines below it and run their fragments as commands.
cd /d "%~dp0"
set PYTHONPATH=%~dp0src
set CONSOLE_PORT=8787

echo ==========================================================
echo   OKX EMA console
echo   Open http://127.0.0.1:%CONSOLE_PORT%/ in your browser
echo   Demo account only. Ctrl+C to stop.
echo ==========================================================
echo.

if not exist "%~dp0.venv\Scripts\python.exe" (
    echo ERROR: .venv not found at %~dp0.venv
    echo Create it first:  python -m venv .venv
    goto :end
)
if not exist "%~dp0src\okx_ema_trader\console_server.py" (
    echo ERROR: package not found at %~dp0src\okx_ema_trader
    goto :end
)

"%~dp0.venv\Scripts\python.exe" -m okx_ema_trader.console_server --port %CONSOLE_PORT% --open

:end
pause
