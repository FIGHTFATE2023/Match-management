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
echo   比赛管理系统 · 公网入口
echo   本机唯一后端 + 隧道转发，本机 / 局域网 / 公网共用同一份数据
echo   默认 Cloudflare 临时入口；固定域名请用 start-public.cmd --tunnel ngrok
echo.
"%PY%" public_tunnel.py %*
pause
