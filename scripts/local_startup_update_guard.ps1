$ProjectRoot = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$StateDir = Join-Path $ProjectRoot ".local"
$MarkerPath = Join-Path $StateDir "last_startup_update.txt"
$IntervalDays = 7

if (-not (Test-Path $StateDir)) {
    New-Item -ItemType Directory -Path $StateDir | Out-Null
}

if (Test-Path $MarkerPath) {
    $raw = Get-Content $MarkerPath -Raw
    $lastRun = [datetime]::MinValue
    if ([datetime]::TryParse($raw, [ref]$lastRun)) {
        $age = (Get-Date) - $lastRun
        if ($age.TotalDays -lt $IntervalDays) {
            $remaining = [math]::Ceiling($IntervalDays - $age.TotalDays)
            Write-Host "Last local update was $([math]::Round($age.TotalDays, 1)) days ago. Skipping startup update. Next check in about $remaining day(s)."
            exit 10
        }
    }
}

Write-Host "Local data is older than $IntervalDays days. Running startup update."
exit 0
