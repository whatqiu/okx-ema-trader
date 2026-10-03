@echo off
rem Start the trading platform (FastAPI backend + built frontend).
rem NOTE: keep this file ASCII-only. cmd.exe reads .bat as GBK, so UTF-8
rem Chinese comments turn into garbage "commands".
rem After changing the frontend, rebuild first:
rem   cd platform\frontend && npm run build
cd /d "%~dp0platform\backend"

rem If 8788 is already bound, an instance is already running - just use it.
netstat -ano | findstr ":8788 " | findstr "LISTENING" >nul 2>&1
if %errorlevel%==0 (
    echo Port 8788 is already in use - the platform is already running.
    echo Open http://127.0.0.1:8788 in your browser.
    pause
    exit /b 0
)

echo Starting platform at http://127.0.0.1:8788 ...
"%~dp0.venv\Scripts\python.exe" -m uvicorn main:app --host 127.0.0.1 --port 8788
pause
