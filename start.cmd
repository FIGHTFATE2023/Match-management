@echo off
cd /d "%~dp0"
set "PY=python"
if exist ".venv\Scripts\python.exe" set "PY=.venv\Scripts\python.exe"
if not defined PORT set "PORT=8768"
"%PY%" -c "import flask" >nul 2>&1
if errorlevel 1 (
  echo Installing Flask ...
  "%PY%" -m pip install -r requirements.txt
)
echo.
echo   Match Management System
echo   Local : http://127.0.0.1:%PORT%
"%PY%" -c "import socket; print('   LAN   : http://' + socket.gethostbyname(socket.gethostname()) + ':%PORT%' + '  (refer to ipconfig)')"
echo   Public: run start-public.cmd  (tunnel to this same backend)
echo   Press Ctrl+C to stop
echo.
"%PY%" backend\app.py
pause
