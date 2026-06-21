@echo off
cd /d "%~dp0"

echo Starting Web Dashboard...
powershell -NoProfile -ExecutionPolicy Bypass -Command "$client = New-Object Net.Sockets.TcpClient; $connect = $client.BeginConnect('127.0.0.1', 3000, $null, $null); $portOpen = $connect.AsyncWaitHandle.WaitOne(300); if ($portOpen) { try { $client.EndConnect($connect) } catch { $portOpen = $false } }; $client.Close(); if (-not $portOpen) { Start-Process cmd.exe -ArgumentList '/k cd /d ""%CD%\web-dashboard"" && npm run dev' -WindowStyle Minimized }"

powershell -NoProfile -Command "Start-Sleep -Seconds 3"
start http://localhost:3000
