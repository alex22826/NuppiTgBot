$ErrorActionPreference = "Stop"

$projectDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$projectMarker = $projectDir.ToLowerInvariant()

# Сначала завершаем экземпляр утренней задачи, если он ещё выполняется.
Stop-ScheduledTask -TaskName "Nuppi Bot Start 07-00" -ErrorAction SilentlyContinue

# Дополнительно останавливаем bot.py, если он был запущен вручную из этого
# проекта. Django-сайт и другие процессы Python не затрагиваются.
$processes = @(Get-CimInstance Win32_Process)
$rootIds = @(
    $processes | Where-Object {
        $_.Name -in @("python.exe", "pythonw.exe") -and
        $_.CommandLine -and
        $_.CommandLine.ToLowerInvariant().Contains($projectMarker) -and
        $_.CommandLine -match '(?i)(?:^|[\\/\s"])(?:bot\.py)(?:[\s"]|$)'
    } | ForEach-Object { [int]$_.ProcessId }
)

$targetIds = [System.Collections.Generic.HashSet[int]]::new()
function Add-ProcessTree([int]$processId) {
    if (-not $targetIds.Add($processId)) {
        return
    }
    foreach ($child in $processes | Where-Object { $_.ParentProcessId -eq $processId }) {
        Add-ProcessTree ([int]$child.ProcessId)
    }
}
foreach ($rootId in $rootIds) {
    Add-ProcessTree $rootId
}

# Сначала завершаем дочерние процессы, затем их запускатели.
$orderedIds = @($targetIds) | Sort-Object -Descending
foreach ($processId in $orderedIds) {
    Stop-Process -Id $processId -Force -ErrorAction SilentlyContinue
}
