@echo off
cd /d "%~dp0"

echo Waiting for network startup...
timeout /t 30 /nobreak >nul

powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\local_startup_update_guard.ps1"
if %ERRORLEVEL% EQU 10 (
    echo Startup update skipped. Local data was updated recently.
    exit /b 0
)
if %ERRORLEVEL% NEQ 0 exit /b %ERRORLEVEL%

call "%~dp0Run_Update_Only.bat"
if %ERRORLEVEL% NEQ 0 exit /b %ERRORLEVEL%

powershell -NoProfile -ExecutionPolicy Bypass -Command "New-Item -ItemType Directory -Force -Path '%~dp0.local' | Out-Null; (Get-Date).ToString('o') | Set-Content -Encoding UTF8 '%~dp0.local\last_startup_update.txt'"
