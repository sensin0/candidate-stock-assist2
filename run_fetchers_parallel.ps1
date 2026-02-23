# Run Go and Python fetchers in parallel
# Called from Run_Full_Cycle.bat

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path

$goJob = Start-Job -ScriptBlock {
    param($dir)
    Set-Location $dir
    & ".\fetch_quotes.exe" 2>&1
} -ArgumentList $scriptDir

$pyJob = Start-Job -ScriptBlock {
    param($dir)
    Set-Location $dir
    & python populate_financials_fast.py 2>&1
} -ArgumentList $scriptDir

# Wait for both jobs
$goJob, $pyJob | Wait-Job | Out-Null

# Display results
Write-Host "`n--- Go Fetcher Output ---" -ForegroundColor Cyan
Receive-Job $goJob

Write-Host "`n--- Python Fetcher Output ---" -ForegroundColor Cyan
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
