@echo off
cd /d "%~dp0"

echo Starting Web Dashboard...
powershell -NoProfile -ExecutionPolicy Bypass -Command "$client = New-Object Net.Sockets.TcpClient; $connect = $client.BeginConnect('127.0.0.1', 3000, $null, $null); $portOpen = $connect.AsyncWaitHandle.WaitOne(300); if ($portOpen) { try { $client.EndConnect($connect) } catch { $portOpen = $false } }; $client.Close(); if (-not $portOpen) { exit 1 }"
if %ERRORLEVEL% NEQ 0 (
    start "Cyclical Ranker Web" /min /D "%~dp0web-dashboard" cmd /k "npm run dev"
)

powershell -NoProfile -ExecutionPolicy Bypass -Command "$ready = $false; for ($i = 0; $i -lt 40; $i++) { $client = New-Object Net.Sockets.TcpClient; $connect = $client.BeginConnect('127.0.0.1', 3000, $null, $null); if ($connect.AsyncWaitHandle.WaitOne(500)) { try { $client.EndConnect($connect); $ready = $true; $client.Close(); break } catch {} }; $client.Close(); Start-Sleep -Milliseconds 500 }; if (-not $ready) { Write-Host 'Web server is still starting. Opening browser anyway.' }"
start http://localhost:3000
