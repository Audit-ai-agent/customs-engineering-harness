[CmdletBinding()]
param(
    [switch]$VerifyOnly,
    [switch]$Force
)

$ErrorActionPreference = 'Stop'
if ($VerifyOnly -and $Force) { throw '-VerifyOnly and -Force are mutually exclusive.' }

. (Join-Path $PSScriptRoot 'tool-cache.ps1')

$HarnessRoot = Split-Path -Parent $PSScriptRoot
$HarnessDirectory = Split-Path -Leaf $HarnessRoot

function Find-WorkspaceRoot {
    $Cursor = Get-Item -LiteralPath $PSScriptRoot
    while ($null -ne $Cursor) {
        if (
            (Test-Path -LiteralPath (Join-Path $Cursor.FullName $HarnessDirectory) -PathType Container) -and
            (Test-Path -LiteralPath (Join-Path (Join-Path $Cursor.FullName $HarnessDirectory) 'config\workspace.toml') -PathType Leaf)
        ) {
            return $Cursor.FullName
        }
        $Cursor = $Cursor.Parent
    }
    throw 'Unable to locate the project workspace root.'
}

function Assert-UvExecutable {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$ExpectedVersion,
        [Parameter(Mandatory = $true)][string]$ExpectedSha256
    )
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        throw "Pinned uv executable is missing: $Path"
    }
    $ActualSha256 = (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($ActualSha256 -ne $ExpectedSha256) {
        throw "Pinned uv executable checksum mismatch: expected=$ExpectedSha256 actual=$ActualSha256"
    }
    $VersionLines = & $Path --version 2>&1
    $VersionSucceeded = $?
    $VersionOutput = ($VersionLines | Select-Object -First 1).ToString()
    if (-not $VersionSucceeded -or $VersionOutput -notmatch ('^uv\s+' + [Regex]::Escape($ExpectedVersion) + '(\s|$)')) {
        throw "Pinned uv executable version mismatch: expected=$ExpectedVersion actual=$VersionOutput"
    }
}

$WorkspaceRoot = Find-WorkspaceRoot
$EngineeringRoot = $HarnessRoot
$LockPath = Join-Path $EngineeringRoot 'config\versions.lock.toml'
$LockText = [IO.File]::ReadAllText($LockPath)
$Version = Get-CustomsTomlTopLevelQuotedString -Text $LockText -Name 'uv'
if ($Version -notmatch '^[0-9]+(?:\.[0-9]+){1,3}(?:[-+][A-Za-z0-9.-]+)?$') {
    throw 'Pinned uv version is not a safe path component.'
}
$UvLock = Get-CustomsTomlSectionText -Text $LockText -Section 'tools.uv_windows_x64'
$ArchiveUrl = Get-CustomsTomlQuotedString `
    -SectionText $UvLock -Name 'archive_url' -Context '[tools.uv_windows_x64]'
$ArchiveSha256 = (Get-CustomsTomlQuotedString `
    -SectionText $UvLock -Name 'archive_sha256' -Context '[tools.uv_windows_x64]').ToLowerInvariant()
$ExecutableSha256 = (Get-CustomsTomlQuotedString `
    -SectionText $UvLock -Name 'executable_sha256' -Context '[tools.uv_windows_x64]').ToLowerInvariant()
foreach ($Digest in @($ArchiveSha256, $ExecutableSha256)) {
    if ($Digest -notmatch '^[0-9a-f]{64}$') {
        throw 'Pinned uv SHA-256 is invalid.'
    }
}
$ToolsRoot = Get-CustomsExternalToolsRoot `
    -EngineeringRoot $EngineeringRoot -WorkspaceRoot $WorkspaceRoot
$UvToolsRoot = Assert-CustomsPathWithin `
    -Path (Join-Path $ToolsRoot 'uv') -Parent $ToolsRoot -Label 'uv tool root'
$InstallRoot = Assert-CustomsPathWithin `
    -Path (Join-Path $UvToolsRoot "v$Version") -Parent $UvToolsRoot -Label 'uv install root'
$UvPath = Join-Path $InstallRoot 'uv.exe'
Assert-CustomsNoReparseBetween `
    -Path $UvPath -Boundary $ToolsRoot -Label 'uv executable'

if ($VerifyOnly) {
    Assert-UvExecutable -Path $UvPath -ExpectedVersion $Version -ExpectedSha256 $ExecutableSha256
    Write-Output $UvPath
    return
}

if ((Test-Path -LiteralPath $UvPath -PathType Leaf) -and -not $Force) {
    Assert-UvExecutable -Path $UvPath -ExpectedVersion $Version -ExpectedSha256 $ExecutableSha256
    Write-Output $UvPath
    return
}

$BootstrapLock = Enter-CustomsToolBootstrapLock `
    -ToolRoot $UvToolsRoot -ToolsRoot $ToolsRoot -ToolName 'uv'
try {
    $StageRoot = Assert-CustomsPathWithin `
        -Path (Join-Path $UvToolsRoot ('.bootstrap-' + [Guid]::NewGuid().ToString('N'))) `
        -Parent $UvToolsRoot `
        -Label 'uv bootstrap stage'
    Assert-CustomsNoReparseBetween `
        -Path $StageRoot -Boundary $ToolsRoot -Label 'uv bootstrap stage'
    $ArchivePath = Join-Path $StageRoot 'uv.zip'
    $ExtractRoot = Join-Path $StageRoot 'extract'
    $CandidateRoot = Join-Path $StageRoot 'candidate'
    try {
        New-Item -ItemType Directory -Force -Path $CandidateRoot | Out-Null
        Invoke-WebRequest -UseBasicParsing -Uri $ArchiveUrl -OutFile $ArchivePath
        $ActualArchiveSha256 = (Get-FileHash -LiteralPath $ArchivePath -Algorithm SHA256).Hash.ToLowerInvariant()
        if ($ActualArchiveSha256 -ne $ArchiveSha256) {
            throw "uv archive checksum mismatch: expected=$ArchiveSha256 actual=$ActualArchiveSha256"
        }
        Expand-Archive -LiteralPath $ArchivePath -DestinationPath $ExtractRoot -Force
        $ExtractedUv = @(Get-ChildItem -LiteralPath $ExtractRoot -Recurse -File -Filter uv.exe)
        if ($ExtractedUv.Count -ne 1) {
            throw 'Downloaded uv archive must contain exactly one uv.exe.'
        }
        Copy-Item -LiteralPath $ExtractedUv[0].FullName -Destination (Join-Path $CandidateRoot 'uv.exe')
        Assert-UvExecutable `
            -Path (Join-Path $CandidateRoot 'uv.exe') `
            -ExpectedVersion $Version `
            -ExpectedSha256 $ExecutableSha256
        $VerifyInstalled = {
            param($Root)
            Assert-UvExecutable `
                -Path (Join-Path $Root 'uv.exe') `
                -ExpectedVersion $Version `
                -ExpectedSha256 $ExecutableSha256
        }
        Install-CustomsToolCandidate `
            -CandidateRoot $CandidateRoot `
            -InstallRoot $InstallRoot `
            -ToolRoot $UvToolsRoot `
            -ToolsRoot $ToolsRoot `
            -Label 'uv' `
            -VerifyInstalled $VerifyInstalled
    }
    finally {
        if (Test-Path -LiteralPath $StageRoot) {
            Remove-CustomsPlainDirectoryTree `
                -Path $StageRoot -Boundary $ToolsRoot -Label 'uv bootstrap stage'
        }
    }
}
finally {
    Exit-CustomsToolBootstrapLock -Lock $BootstrapLock -ToolRoot $UvToolsRoot
}

Write-Output $UvPath
