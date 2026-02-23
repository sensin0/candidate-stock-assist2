$WshShell = New-Object -comObject WScript.Shell
$ShortcutPath = Join-Path $env:APPDATA "Microsoft\Windows\Start Menu\Programs\Startup\Ta-Chan2_AutoStart.lnk"
$Shortcut = $WshShell.CreateShortcut($ShortcutPath)
$Shortcut.TargetPath = "d:\Documents\antigravity\ta-chan2\Run_Full_Cycle.bat"
$Shortcut.WorkingDirectory = "d:\Documents\antigravity\ta-chan2"
$Shortcut.Save()
Write-Host "Shortcut created at $ShortcutPath"
