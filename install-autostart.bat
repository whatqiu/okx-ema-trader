@echo off
rem Install or remove the platform backend as a Windows logon startup item.
rem Run with "install" to add, "remove" to delete, no arg to print status.
rem
rem Why a startup item and not a service:
rem  - The platform reads its proxy from HKCU\...\Internet Settings, which is
rem    per-user; running as SYSTEM would lose the proxy and 502 OKX again.
rem  - A service would also need a separate logon (with stored credentials) to
rem    access the user's HKCU hive; a startup item runs as the user, so the
rem    proxy and the venv both Just Work.
rem
rem The startup item launches uvicorn windowless via pythonw so the console
rem does not pop up on every login. Logs go to the same platform.log the
rem interactive start-platform.bat writes to.

setlocal
set "STARTUP_DIR=%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup"
set "VBS=%STARTUP_DIR%\okx-ema-trader-platform.vbs"
set "HERE=%~dp0"
set "HERE=%HERE:~0,-1%"

if /i "%~1"=="install" goto install
if /i "%~1"=="remove"  goto remove
:status
if exist "%VBS%" (
  echo status: installed  ^(logon starts uvicorn in background^)
  echo   %VBS%
) else (
  echo status: not installed
  echo   use: %~nx0 install
)
goto :eof

:install
if not exist "%STARTUP_DIR%" mkdir "%STARTUP_DIR%"
rem wscript.shell Run with 0 = hidden window; the third arg "False" = no wait.
> "%VBS%" echo Set WshShell = CreateObject("WScript.Shell")
>> "%VBS%" echo WshShell.Run "cmd /c cd /d ""%HERE%\platform\backend"" && ""%HERE%\.venv\Scripts\pythonw.exe"" -m uvicorn main:app --host 127.0.0.1 --port 8788", 0, False
echo installed: %VBS%
echo Logon will start uvicorn on 127.0.0.1:8788 in the background.
echo Stop it from Task Manager ^(pythonw.exe^) or with: %~nx0 remove
goto :eof

:remove
if exist "%VBS%" del "%VBS%" && echo removed: %VBS%
if not exist "%VBS%" echo not installed, nothing to remove
goto :eof