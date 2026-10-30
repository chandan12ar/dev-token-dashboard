# Restart AI Fluency Insights on a fresh process so it reloads the latest
# code, then open it in your browser. Invoked by Restart-Insights.bat.
$ErrorActionPreference = 'SilentlyContinue'
$port = 8787
$src  = Join-Path (Split-Path (Split-Path $PSScriptRoot -Parent) -Parent) 'src'

Write-Host ''
Write-Host '  AI Fluency Insights - restart' -ForegroundColor Cyan
Write-Host '  -----------------------------'

if (-not (Test-Path (Join-Path $src 'ai_fluency_insights'))) {
    Write-Host "  ERROR: src\ai_fluency_insights not found - run this from the repository's scripts\windows folder." -ForegroundColor Red
    Start-Sleep -Seconds 4
    exit 1
}

# 1) Stop every running copy - matched by command line AND by the port, so no
#    stale duplicates survive (Python's socket reuse lets several share a port).
#    Also matches the pre-rename dev_token_dashboard.py and the packaged .exe.
Write-Host '  Stopping running instances...'
$killed = 0
Get-CimInstance Win32_Process |
    Where-Object { $_.Name -match '^pythonw?[\d.]*\.exe$' -and ($_.CommandLine -like '*ai_fluency_insights*' -or $_.CommandLine -like '*dev_token_dashboard*') } |
    ForEach-Object { if (Stop-Process -Id $_.ProcessId -Force -PassThru) { $killed++ } }
Get-Process -Name 'AIFluencyInsights' |
    ForEach-Object { if (Stop-Process -Id $_.Id -Force -PassThru) { $killed++ } }
foreach ($procId in ((Get-NetTCPConnection -LocalPort $port -State Listen).OwningProcess | Select-Object -Unique)) {
    if (Stop-Process -Id $procId -Force -PassThru) { $killed++ }
}
Write-Host "  Stopped $killed instance(s)."
Start-Sleep -Milliseconds 700

# 2) Launch one fresh, windowless instance (survives after this window closes).
Write-Host '  Starting fresh instance...'
$pyw = (Get-Command pythonw.exe -ErrorAction SilentlyContinue).Source
if (-not $pyw) { $pyw = (Get-Command python.exe -ErrorAction SilentlyContinue).Source }
if (-not $pyw) {
    Write-Host '  ERROR: Python not found on PATH.' -ForegroundColor Red
    Start-Sleep -Seconds 4
    exit 1
}
# Runs this checkout's code directly, so no reinstall is needed after editing it.
$env:PYTHONPATH = $src
Start-Process -FilePath $pyw -ArgumentList ('-m ai_fluency_insights --no-browser --port {0}' -f $port) -WindowStyle Hidden
Start-Sleep -Seconds 2

# 3) Confirm it is up, then open the browser.
try {
    $r = Invoke-WebRequest "http://localhost:$port/" -UseBasicParsing -TimeoutSec 6
    if ($r.StatusCode -eq 200) {
        Write-Host "  Dashboard is up at http://localhost:$port/" -ForegroundColor Green
    }
} catch {
    Write-Host "  Started, but couldn't confirm it yet - opening anyway." -ForegroundColor Yellow
}
Start-Process "http://localhost:$port/"
Start-Sleep -Seconds 1
