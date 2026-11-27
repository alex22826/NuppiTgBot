$ErrorActionPreference = "Stop"

$isAdministrator = ([Security.Principal.WindowsPrincipal] [Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole(
    [Security.Principal.WindowsBuiltInRole]::Administrator
)
if (-not $isAdministrator) {
    Start-Process -FilePath "$env:SystemRoot\System32\WindowsPowerShell\v1.0\powershell.exe" `
        -Verb RunAs `
        -ArgumentList "-NoProfile -ExecutionPolicy Bypass -File `"$PSCommandPath`""
    exit 0
}

$projectDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$startScript = Join-Path $projectDir "start_bot_scheduled.ps1"
$stopScript = Join-Path $projectDir "stop_bot_scheduled.ps1"
$powerShellExe = "$env:SystemRoot\System32\WindowsPowerShell\v1.0\powershell.exe"

foreach ($requiredFile in @($startScript, $stopScript, (Join-Path $projectDir "bot.py"))) {
    if (-not (Test-Path -LiteralPath $requiredFile)) {
        throw "Не найден обязательный файл: $requiredFile"
    }
}

$startAction = New-ScheduledTaskAction `
    -Execute $powerShellExe `
    -Argument "-NoProfile -NonInteractive -ExecutionPolicy Bypass -File `"$startScript`"" `
    -WorkingDirectory $projectDir
$stopAction = New-ScheduledTaskAction `
    -Execute $powerShellExe `
    -Argument "-NoProfile -NonInteractive -ExecutionPolicy Bypass -File `"$stopScript`"" `
    -WorkingDirectory $projectDir

$startTrigger = New-ScheduledTaskTrigger -Daily -At "07:00"
$stopTrigger = New-ScheduledTaskTrigger -Daily -At "19:00"
$startSettings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable -WakeToRun -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Hours 12)
$stopSettings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable -WakeToRun -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Minutes 10)

Register-ScheduledTask `
    -TaskName "Nuppi Bot Start 07-00" `
    -Action $startAction -Trigger $startTrigger -Settings $startSettings `
    -Description "Ежедневный запуск Telegram-бота Nuppi в 07:00." -Force | Out-Null

# Удаляем прежнюю вечернюю задачу, если раньше было установлено расписание до 21:00.
Unregister-ScheduledTask -TaskName "Nuppi Bot Stop 21-00" -Confirm:$false -ErrorAction SilentlyContinue
Register-ScheduledTask `
    -TaskName "Nuppi Bot Stop 19-00" `
    -Action $stopAction -Trigger $stopTrigger -Settings $stopSettings `
    -Description "Ежедневная остановка Telegram-бота Nuppi в 19:00." -Force | Out-Null

Write-Host "Расписание Nuppi установлено: запуск в 07:00, остановка в 19:00." -ForegroundColor Green
Get-ScheduledTask -TaskName "Nuppi Bot Start 07-00", "Nuppi Bot Stop 19-00" |
    Select-Object TaskName, State |
    Format-Table -AutoSize
