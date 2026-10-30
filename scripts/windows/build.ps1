# Build AI Fluency Insights into a single Windows .exe with Nuitka.
#
# Run this on a Windows machine (Nuitka produces a Windows binary only on
# Windows). It needs Python 3.8+ on PATH; Nuitka downloads its C compiler
# automatically the first time. The source code is compiled to machine code,
# so the shipped .exe cannot be turned back into readable Python.
#
#   powershell -ExecutionPolicy Bypass -File scripts\windows\build.ps1
#
# Result: dist\AIFluencyInsights.exe  (one file, no Python needed to run it).

$ErrorActionPreference = 'Stop'

# --- Edit these for your organization -------------------------------------
$Company   = 'Your Company'
$Product   = 'AI Fluency Insights'
$Copyright = 'Internal use only'
# An optional .ico makes the exe and its window use your icon; leave blank to skip.
$IconPath  = ''
# --------------------------------------------------------------------------

$repo = Split-Path (Split-Path $PSScriptRoot -Parent) -Parent
$src  = Join-Path $repo 'src'
$pkg  = Join-Path $src 'ai_fluency_insights'
$entry = Join-Path $PSScriptRoot 'afi_entry.py'

# Read the version straight from the package so the exe metadata stays in sync.
$verLine = Select-String -Path (Join-Path $pkg '__init__.py') -Pattern '__version__\s*=\s*"([^"]+)"'
$Version = $verLine.Matches[0].Groups[1].Value
Write-Host "Building $Product $Version" -ForegroundColor Cyan

$python = (Get-Command python -ErrorAction SilentlyContinue).Source
if (-not $python) { throw 'python not found on PATH.' }

# Make sure Nuitka is available (installs into the current environment).
& $python -c "import nuitka" 2>$null
if ($LASTEXITCODE -ne 0) {
    Write-Host 'Installing Nuitka...' -ForegroundColor Yellow
    & $python -m pip install --upgrade nuitka
}

# The package must be importable while Nuitka analyses it.
$env:PYTHONPATH = $src

$distDir = Join-Path $repo 'dist'

$nuitkaArgs = @(
    '-m', 'nuitka',
    '--onefile',
    '--standalone',
    '--assume-yes-for-downloads',
    '--windows-console-mode=disable',          # no black window on normal launch
    '--company-name',  $Company,
    '--product-name',  $Product,
    '--file-version',  $Version,
    '--product-version', $Version,
    '--file-description', 'Local Claude Code usage dashboard',
    '--copyright', $Copyright,
    '--include-package=ai_fluency_insights',
    '--include-module=ai_fluency_insights.server.app',   # imported lazily in cli.main
    "--include-data-dir=$pkg\web=ai_fluency_insights\web",
    "--include-data-dir=$pkg\resources=ai_fluency_insights\resources",
    '--output-dir', $distDir,
    '--output-filename=AIFluencyInsights.exe',
    '--remove-output'
)
if ($IconPath -and (Test-Path $IconPath)) {
    $nuitkaArgs += "--windows-icon-from-ico=$IconPath"
}
$nuitkaArgs += $entry

& $python @nuitkaArgs

$exe = Join-Path $distDir 'AIFluencyInsights.exe'
if (Test-Path $exe) {
    Write-Host ''
    Write-Host "Built: $exe" -ForegroundColor Green
    Write-Host 'Quick check:' -ForegroundColor Cyan
    & $exe --version
    Write-Host ''
    Write-Host 'Next: test it on a clean machine, then (recommended) code-sign it.'
} else {
    throw 'Build finished but AIFluencyInsights.exe was not produced.'
}
