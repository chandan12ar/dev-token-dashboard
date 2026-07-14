# Restart the Dev Token Dashboard on a fresh process so it reloads the latest
# code, then open it in your browser. Invoked by Restart-Dashboard.bat.
$ErrorActionPreference = 'SilentlyContinue'
$port   = 8787
$script = Join-Path $PSScriptRoot 'dev_token_dashboard.py'

Write-Host ''
Write-Host '  Dev Token Dashboard - restart' -ForegroundColor Cyan
Write-Host '  -----------------------------'

if (-not (Test-Path $script)) {
    Write-Host "  ERROR: dev_token_dashboard.py not found next to this script." -ForegroundColor Red
    Start-Sleep -Seconds 4
    exit 1
}

# 1) Stop every running copy - matched by command line AND by the port, so no
#    stale duplicates survive (Python's socket reuse lets several share a port).
Write-Host '  Stopping running instances...'
$killed = 0
Get-CimInstance Win32_Process |
    Where-Object { ($_.Name -eq 'python.exe' -or $_.Name -eq 'pythonw.exe') -and $_.CommandLine -like '*dev_token_dashboard*' } |
    ForEach-Object { if (Stop-Process -Id $_.ProcessId -Force -PassThru) { $killed++ } }
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
Start-Process -FilePath $pyw -ArgumentList ('"{0}" --no-browser --port {1}' -f $script, $port) -WindowStyle Hidden
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
