param(
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$HarnessArgs
)

$ErrorActionPreference = 'Stop'
$EngineeringRoot = Split-Path -Parent $PSScriptRoot
$PinnedNodeDir = Join-Path $env:LOCALAPPDATA 'CustomsUAT\cache\tools\node\v22.23.1'
if (Test-Path -LiteralPath (Join-Path $PinnedNodeDir 'node.exe') -PathType Leaf) {
    $env:PATH = "$PinnedNodeDir;$env:PATH"
}
$BundledUv = Join-Path $env:LOCALAPPDATA 'CustomsUAT\cache\tools\uv\v0.11.28\uv.exe'
if ((Test-Path -LiteralPath $BundledUv -PathType Leaf) -and $null -eq (Get-Command uv -ErrorAction SilentlyContinue)) {
    $env:PATH = "$(Split-Path -Parent $BundledUv);$env:PATH"
}
$PythonCandidates = @(
    (Join-Path $env:APPDATA 'uv\python\cpython-3.11.15-windows-x86_64-none\python.exe'),
    (Join-Path $env:APPDATA 'uv\python\cpython-3.11-windows-x86_64-none\python.exe')
)

$Python = $null
foreach ($Candidate in $PythonCandidates) {
    if (-not (Test-Path -LiteralPath $Candidate -PathType Leaf)) { continue }
    $Version = & $Candidate -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')"
    if ($LASTEXITCODE -eq 0 -and $Version -eq '3.11') {
        $Python = $Candidate
        break
    }
}

if ($null -eq $Python) {
    $PathPython = Get-Command python -ErrorAction SilentlyContinue
    if ($null -ne $PathPython) {
        $Version = & $PathPython.Source -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')"
        if ($LASTEXITCODE -eq 0 -and $Version -eq '3.11') { $Python = $PathPython.Source }
    }
}

if ($null -eq $Python) {
    throw 'Python 3.11 is required. Install it once; the harness does not create its own environment.'
}

$PreviousBytecode = $env:PYTHONDONTWRITEBYTECODE
$env:PYTHONDONTWRITEBYTECODE = '1'
Push-Location $EngineeringRoot
try {
    & $Python -B harness\eng.py @HarnessArgs
    exit $LASTEXITCODE
}
finally {
    Pop-Location
    $env:PYTHONDONTWRITEBYTECODE = $PreviousBytecode
}
