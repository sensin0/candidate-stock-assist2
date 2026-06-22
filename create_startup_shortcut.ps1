$WshShell = New-Object -comObject WScript.Shell
$ShortcutPath = Join-Path $env:APPDATA "Microsoft\Windows\Start Menu\Programs\Startup\CyclicalRanker_OnLoginUpdate.lnk"
$Shortcut = $WshShell.CreateShortcut($ShortcutPath)
$ProjectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$Shortcut.TargetPath = Join-Path $ProjectRoot "Run_Update_After_Login.bat"
$Shortcut.WorkingDirectory = $ProjectRoot
$Shortcut.Save()
Write-Host "Shortcut created at $ShortcutPath"
