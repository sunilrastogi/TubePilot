@echo off
cd /d "%~dp0"
title TubePilot
set "PY=python"
where py >nul 2>nul && set "PY=py -3"
if not exist ".env" (
  echo .env not found - running setup first...
  call setup.bat /auto
)
echo Starting TubePilot on http://127.0.0.1:8791  ^(close this window to stop^)
start "" cmd /c "ping -n 3 127.0.0.1 >nul & start http://127.0.0.1:8791"
%PY% server.py
echo.
echo The server stopped. If it failed to start, port 8791 may be in use - see README Troubleshooting.
pause
