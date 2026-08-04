param(
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$HarnessArgs
)

$ErrorActionPreference = 'Stop'
$HarnessRoot = Split-Path -Parent $PSScriptRoot

function Test-Python311 {
    param([Parameter(Mandatory = $true)][string]$Path)
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) { return $false }
    $Version = & $Path -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')" 2>$null
    return $LASTEXITCODE -eq 0 -and $Version -eq '3.11'
}

$Candidates = @()
if ($env:CUSTOMS_PYTHON) { $Candidates += $env:CUSTOMS_PYTHON }
$PathPython = Get-Command python -ErrorAction SilentlyContinue
if ($null -ne $PathPython) { $Candidates += $PathPython.Source }
if ($IsWindows) {
    $UvPythonRoot = Join-Path $env:APPDATA 'uv\python'
    if (Test-Path -LiteralPath $UvPythonRoot -PathType Container) {
        $Candidates += Get-ChildItem -LiteralPath $UvPythonRoot -Directory -Filter 'cpython-3.11*' |
            ForEach-Object { Join-Path $_.FullName 'python.exe' }
    }
}

$Python = $Candidates | Where-Object { Test-Python311 $_ } | Select-Object -First 1
if ($null -eq $Python) {
    throw 'Python 3.11 is required. Install it, or set CUSTOMS_PYTHON to its executable path.'
}

$PreviousBytecode = $env:PYTHONDONTWRITEBYTECODE
$env:PYTHONDONTWRITEBYTECODE = '1'
Push-Location $HarnessRoot
try {
    & $Python -B harness\eng.py @HarnessArgs
    exit $LASTEXITCODE
}
finally {
    Pop-Location
    $env:PYTHONDONTWRITEBYTECODE = $PreviousBytecode
}
