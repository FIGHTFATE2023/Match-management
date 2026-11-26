@echo off
cd /d "%~dp0"
set "PY=python"
if exist ".venv\Scripts\python.exe" set "PY=.venv\Scripts\python.exe"
"%PY%" -c "import flask" >nul 2>&1
if errorlevel 1 (
  echo Installing Flask ...
  "%PY%" -m pip install -r requirements.txt
)
echo.
echo   Match Management System - Public Entry
echo   One backend + tunnel: local / LAN / public share the same data
echo   Default: Cloudflare temporary tunnel
echo   Fixed URL: start-public.cmd --tunnel ngrok
echo.
"%PY%" backend\public_tunnel.py %*
pause
