$ErrorActionPreference = 'Stop'
$taskProjectRoot = Split-Path -Parent $PSScriptRoot
$taskLauncher = Join-Path $taskProjectRoot 'launch.bat'
$taskIcon = Join-Path $taskProjectRoot 'voice_workbench\assets\app.ico'
$taskShortcutPath = Join-Path $taskProjectRoot '声笺.lnk'
if (!(Test-Path -LiteralPath $taskLauncher) -or !(Test-Path -LiteralPath $taskIcon)) {
    throw '启动文件或程序图标不存在。'
}
$taskShell = New-Object -ComObject WScript.Shell
$taskShortcut = $taskShell.CreateShortcut($taskShortcutPath)
$taskShortcut.TargetPath = $taskLauncher
$taskShortcut.WorkingDirectory = $taskProjectRoot
$taskShortcut.IconLocation = "$taskIcon,0"
$taskShortcut.Description = '声笺 · 本地配音工作台'
$taskShortcut.Save()
Write-Output "已创建：$taskShortcutPath"
