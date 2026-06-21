@echo off
cd /d "%~dp0"

echo Waiting for network startup...
timeout /t 120 /nobreak >nul

call "%~dp0Run_Update_Only.bat"
