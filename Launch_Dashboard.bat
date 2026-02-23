@echo off
cd /d "%~dp0\web-dashboard"

echo Starting Web Dashboard...
start http://localhost:3000

call npm start
pause
