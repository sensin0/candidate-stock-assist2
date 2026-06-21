# Run Go and Python fetchers in parallel
# Called from Run_Full_Cycle.bat

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path

$goJob = Start-Job -ScriptBlock {
    param($dir)
    Set-Location $dir
    & go run fetch_quotes.go 2>&1
} -ArgumentList $scriptDir

$pyJob = Start-Job -ScriptBlock {
    param($dir)
    Set-Location $dir
    & python populate_financials_fast.py 2>&1
} -ArgumentList $scriptDir

$startedAt = Get-Date
Write-Host "Started data update at $($startedAt.ToString('HH:mm:ss')). This can take a while for the full Japan stock list." -ForegroundColor Yellow
Write-Host "Progress will appear below while the two fetchers are running.`n" -ForegroundColor Yellow

while (($goJob.State -eq 'Running') -or ($pyJob.State -eq 'Running')) {
    $elapsed = [int]((Get-Date) - $startedAt).TotalSeconds

    $goOutput = Receive-Job $goJob
    if ($goOutput) {
        Write-Host "`n--- Price Fetcher ---" -ForegroundColor Cyan
        $goOutput
    }

    $pyOutput = Receive-Job $pyJob
    if ($pyOutput) {
        Write-Host "`n--- Financial Fetcher ---" -ForegroundColor Cyan
        $pyOutput
    }

    Write-Host "Still updating... elapsed ${elapsed}s | price=$($goJob.State) financial=$($pyJob.State)" -ForegroundColor DarkGray
    Start-Sleep -Seconds 10
}

Write-Host "`n--- Price Fetcher Final Output ---" -ForegroundColor Cyan
Receive-Job $goJob

Write-Host "`n--- Financial Fetcher Final Output ---" -ForegroundColor Cyan
Receive-Job $pyJob

# Check for failures
$failed = $false
if ($goJob.State -eq 'Failed') {
    Write-Host "ERROR: Go fetcher failed!" -ForegroundColor Red
    $failed = $true
}
if ($pyJob.State -eq 'Failed') {
    Write-Host "ERROR: Python fetcher failed!" -ForegroundColor Red
    $failed = $true
}

Remove-Job $goJob, $pyJob -Force

if ($failed) { exit 1 }
