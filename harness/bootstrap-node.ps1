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

function Assert-NodeDistribution {
    param(
        [Parameter(Mandatory = $true)][string]$Root,
        [Parameter(Mandatory = $true)][string]$ExpectedVersion,
        [Parameter(Mandatory = $true)][string]$ExpectedNodeSha256,
        [Parameter(Mandatory = $true)][string]$ExpectedNpmSha256,
        [Parameter(Mandatory = $true)][string]$ExpectedTreeSha256,
        [Parameter(Mandatory = $true)][Int64]$ExpectedFileCount,
        [Parameter(Mandatory = $true)][Int64]$ExpectedBytes,
        [Parameter(Mandatory = $true)][string]$ToolsRoot
    )
    $NodePath = Join-Path $Root 'node.exe'
    $NpmPath = Join-Path $Root 'npm.cmd'
    foreach ($Required in @($NodePath, $NpmPath)) {
        if (-not (Test-Path -LiteralPath $Required -PathType Leaf)) {
            throw "Pinned Node distribution file is missing: $Required"
        }
    }
    $NodeSha256 = (Get-FileHash $NodePath -Algorithm SHA256).Hash.ToLowerInvariant()
    $NpmSha256 = (Get-FileHash $NpmPath -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($NodeSha256 -ne $ExpectedNodeSha256) {
        throw "Pinned Node executable checksum mismatch: expected=$ExpectedNodeSha256 actual=$NodeSha256"
    }
    if ($NpmSha256 -ne $ExpectedNpmSha256) {
        throw "Pinned npm command checksum mismatch: expected=$ExpectedNpmSha256 actual=$NpmSha256"
    }
    $VersionLines = & $NodePath --version 2>&1
    $Succeeded = $?
    $VersionOutput = ($VersionLines | Select-Object -First 1).ToString().Trim()
    if (-not $Succeeded -or $VersionOutput -ne "v$ExpectedVersion") {
        throw "Pinned Node version mismatch: expected=v$ExpectedVersion actual=$VersionOutput"
    }
    $Inventory = Get-CustomsToolTreeInventory `
        -Root $Root -ToolsRoot $ToolsRoot -Label 'Node distribution'
    if (
        $Inventory.tree_sha256 -ne $ExpectedTreeSha256 -or
        [Int64]$Inventory.file_count -ne $ExpectedFileCount -or
        [Int64]$Inventory.bytes -ne $ExpectedBytes
    ) {
        throw (
            'Pinned Node distribution inventory mismatch: ' +
            "expected=$ExpectedTreeSha256/$ExpectedFileCount/$ExpectedBytes " +
            "actual=$($Inventory.tree_sha256)/$($Inventory.file_count)/$($Inventory.bytes)"
        )
    }
}

$WorkspaceRoot = Find-WorkspaceRoot
$EngineeringRoot = $HarnessRoot
$LockText = [IO.File]::ReadAllText((Join-Path $EngineeringRoot 'config\versions.lock.toml'))
$Reproducibility = Get-CustomsTomlSectionText -Text $LockText -Section 'reproducibility'
$Version = Get-CustomsTomlQuotedString `
    -SectionText $Reproducibility -Name 'frontend_node' -Context '[reproducibility]'
if ($Version -notmatch '^[0-9]+(?:\.[0-9]+){1,3}(?:[-+][A-Za-z0-9.-]+)?$') {
    throw 'Pinned Node version is not a safe path component.'
}
$NodeLock = Get-CustomsTomlSectionText -Text $LockText -Section 'tools.node_windows_x64'
$ArchiveUrl = Get-CustomsTomlQuotedString `
    -SectionText $NodeLock -Name 'archive_url' -Context '[tools.node_windows_x64]'
$ArchiveSha256 = (Get-CustomsTomlQuotedString `
    -SectionText $NodeLock -Name 'archive_sha256' -Context '[tools.node_windows_x64]').ToLowerInvariant()
$NodeSha256 = (Get-CustomsTomlQuotedString `
    -SectionText $NodeLock -Name 'executable_sha256' -Context '[tools.node_windows_x64]').ToLowerInvariant()
$NpmSha256 = (Get-CustomsTomlQuotedString `
    -SectionText $NodeLock -Name 'npm_cmd_sha256' -Context '[tools.node_windows_x64]').ToLowerInvariant()
$InventorySchema = Get-CustomsTomlQuotedString `
    -SectionText $NodeLock `
    -Name 'distribution_inventory_schema' `
    -Context '[tools.node_windows_x64]'
if ($InventorySchema -ne 'relative-path-size-file-sha256-lines-v1') {
    throw "Unsupported Node distribution inventory schema: $InventorySchema"
}
$TreeSha256 = (Get-CustomsTomlQuotedString `
    -SectionText $NodeLock `
    -Name 'distribution_tree_sha256' `
    -Context '[tools.node_windows_x64]').ToLowerInvariant()
$TreeFileCount = Get-CustomsTomlNonnegativeInteger `
    -SectionText $NodeLock `
    -Name 'distribution_file_count' `
    -Context '[tools.node_windows_x64]'
$TreeBytes = Get-CustomsTomlNonnegativeInteger `
    -SectionText $NodeLock `
    -Name 'distribution_bytes' `
    -Context '[tools.node_windows_x64]'
foreach ($Digest in @($ArchiveSha256, $NodeSha256, $NpmSha256, $TreeSha256)) {
    if ($Digest -notmatch '^[0-9a-f]{64}$') { throw 'Pinned Node SHA-256 is invalid.' }
}

$ToolsRoot = Get-CustomsExternalToolsRoot `
    -EngineeringRoot $EngineeringRoot -WorkspaceRoot $WorkspaceRoot
$NodeRoot = Assert-CustomsPathWithin `
    -Path (Join-Path $ToolsRoot 'node') -Parent $ToolsRoot -Label 'Node tool root'
$InstallRoot = Assert-CustomsPathWithin `
    -Path (Join-Path $NodeRoot "v$Version") -Parent $NodeRoot -Label 'Node install root'
Assert-CustomsNoReparseBetween `
    -Path $InstallRoot -Boundary $ToolsRoot -Label 'Node install root'

if ($VerifyOnly) {
    Assert-NodeDistribution `
        -Root $InstallRoot `
        -ExpectedVersion $Version `
        -ExpectedNodeSha256 $NodeSha256 `
        -ExpectedNpmSha256 $NpmSha256 `
        -ExpectedTreeSha256 $TreeSha256 `
        -ExpectedFileCount $TreeFileCount `
        -ExpectedBytes $TreeBytes `
        -ToolsRoot $ToolsRoot
    Write-Output $InstallRoot
    return
}
if ((Test-Path -LiteralPath (Join-Path $InstallRoot 'node.exe') -PathType Leaf) -and -not $Force) {
    Assert-NodeDistribution `
        -Root $InstallRoot `
        -ExpectedVersion $Version `
        -ExpectedNodeSha256 $NodeSha256 `
        -ExpectedNpmSha256 $NpmSha256 `
        -ExpectedTreeSha256 $TreeSha256 `
        -ExpectedFileCount $TreeFileCount `
        -ExpectedBytes $TreeBytes `
        -ToolsRoot $ToolsRoot
    Write-Output $InstallRoot
    return
}

$BootstrapLock = Enter-CustomsToolBootstrapLock `
    -ToolRoot $NodeRoot -ToolsRoot $ToolsRoot -ToolName 'node'
try {
    $StageRoot = Assert-CustomsPathWithin `
        -Path (Join-Path $NodeRoot ('.bootstrap-' + [Guid]::NewGuid().ToString('N'))) `
        -Parent $NodeRoot `
        -Label 'Node bootstrap stage'
    Assert-CustomsNoReparseBetween `
        -Path $StageRoot -Boundary $ToolsRoot -Label 'Node bootstrap stage'
    $ArchivePath = Join-Path $StageRoot 'node.zip'
    $ExtractRoot = Join-Path $StageRoot 'extract'
    try {
        New-Item -ItemType Directory -Force -Path $StageRoot | Out-Null
        Invoke-WebRequest -UseBasicParsing -Uri $ArchiveUrl -OutFile $ArchivePath
        $ActualArchiveSha256 = (Get-FileHash $ArchivePath -Algorithm SHA256).Hash.ToLowerInvariant()
        if ($ActualArchiveSha256 -ne $ArchiveSha256) {
            throw "Node archive checksum mismatch: expected=$ArchiveSha256 actual=$ActualArchiveSha256"
        }
        Expand-Archive -LiteralPath $ArchivePath -DestinationPath $ExtractRoot -Force
        $Candidates = @(
            Get-ChildItem -LiteralPath $ExtractRoot -Recurse -File -Filter node.exe |
                ForEach-Object { $_.Directory.FullName } |
                Sort-Object -Unique
        )
        if ($Candidates.Count -ne 1) {
            throw 'Downloaded Node archive must contain exactly one Node distribution.'
        }
        $CandidateRoot = $Candidates[0]
        Assert-NodeDistribution `
            -Root $CandidateRoot `
            -ExpectedVersion $Version `
            -ExpectedNodeSha256 $NodeSha256 `
            -ExpectedNpmSha256 $NpmSha256 `
            -ExpectedTreeSha256 $TreeSha256 `
            -ExpectedFileCount $TreeFileCount `
            -ExpectedBytes $TreeBytes `
            -ToolsRoot $ToolsRoot
        $VerifyInstalled = {
            param($Root)
            Assert-NodeDistribution `
                -Root $Root `
                -ExpectedVersion $Version `
                -ExpectedNodeSha256 $NodeSha256 `
                -ExpectedNpmSha256 $NpmSha256 `
                -ExpectedTreeSha256 $TreeSha256 `
                -ExpectedFileCount $TreeFileCount `
                -ExpectedBytes $TreeBytes `
                -ToolsRoot $ToolsRoot
        }
        Install-CustomsToolCandidate `
            -CandidateRoot $CandidateRoot `
            -InstallRoot $InstallRoot `
            -ToolRoot $NodeRoot `
            -ToolsRoot $ToolsRoot `
            -Label 'Node' `
            -VerifyInstalled $VerifyInstalled
    }
    finally {
        if (Test-Path -LiteralPath $StageRoot) {
            Remove-CustomsPlainDirectoryTree `
                -Path $StageRoot -Boundary $ToolsRoot -Label 'Node bootstrap stage'
        }
    }
}
finally {
    Exit-CustomsToolBootstrapLock -Lock $BootstrapLock -ToolRoot $NodeRoot
}

Write-Output $InstallRoot
