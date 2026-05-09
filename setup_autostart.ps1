$appPath = Split-Path -Parent $MyInvocation.MyCommand.Path
$vbsPath = Join-Path $appPath "run_silent.vbs"
$startupFolder = "$env:APPDATA\Microsoft\Windows\Start Menu\Programs\Startup"
$shortcutPath = Join-Path $startupFolder "Transcribe App.lnk"

$WshShell = New-Object -ComObject WScript.Shell
$shortcut = $WshShell.CreateShortcut($shortcutPath)
$shortcut.TargetPath = "wscript.exe"
$shortcut.Arguments = "`"$vbsPath`""
$shortcut.WorkingDirectory = $appPath
$shortcut.Description = "Transcribe App"
$shortcut.Save()

Write-Host "Done! Transcribe App will now start silently on every boot." -ForegroundColor Green
Write-Host "No console window, just the tray icon." -ForegroundColor Yellow
