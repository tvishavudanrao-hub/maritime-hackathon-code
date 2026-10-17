@echo off
cd /d "%~dp0"
echo Tideline maritime demo opening at http://127.0.0.1:3001
start "" "http://127.0.0.1:3001"
where py >nul 2>&1
if %errorlevel%==0 (
  py -3 server.py
) else (
  python server.py
)
pause
