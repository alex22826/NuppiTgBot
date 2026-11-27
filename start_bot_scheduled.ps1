$ErrorActionPreference = "Stop"

$projectDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$pythonPath = Join-Path $projectDir "Virtualenv\Scripts\python.exe"
$botPath = Join-Path $projectDir "bot.py"
$logDir = Join-Path $projectDir "logs"
$now = Get-Date
$startTime = [TimeSpan]::FromHours(7)
$stopTime = [TimeSpan]::FromHours(19)

# Если компьютер включили слишком поздно, пропущенная утренняя задача не
# должна запускать бота после окончания рабочего времени.
if ($now.TimeOfDay -lt $startTime -or $now.TimeOfDay -ge $stopTime) {
    exit 0
}

if (-not (Test-Path -LiteralPath $pythonPath) -or -not (Test-Path -LiteralPath $botPath)) {
    throw "Не найден Python или bot.py в $projectDir"
}

# Не запускаем вторую копию, если бот уже был открыт вручную.
$projectMarker = $projectDir.ToLowerInvariant()
$existingBot = Get-CimInstance Win32_Process | Where-Object {
    $_.Name -in @("python.exe", "pythonw.exe") -and
    $_.CommandLine -and
    $_.CommandLine.ToLowerInvariant().Contains($projectMarker) -and
    $_.CommandLine -match '(?i)(?:^|[\\/\s"])(?:bot\.py)(?:[\s"]|$)'
}
if ($existingBot) {
    exit 0
}

New-Item -ItemType Directory -Path $logDir -Force | Out-Null
$logPath = Join-Path $logDir ("bot-" + $now.ToString("yyyy-MM-dd") + ".log")
Set-Location -LiteralPath $projectDir
& $pythonPath $botPath *>> $logPath
exit $LASTEXITCODE
