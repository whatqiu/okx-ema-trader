@echo off
cd /d "%~dp0"
"%~dp0.venv\Scripts\python.exe" tools\start_platform.py
if errorlevel 1 pause
