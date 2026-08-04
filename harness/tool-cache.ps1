# Shared fail-closed path and lock parsing for externally cached tools.
# This file is dot-sourced by standalone harness scripts and performs no action
# when loaded.

function Get-CustomsTomlSectionText {
    param(
        [Parameter(Mandatory = $true)][string]$Text,
        [Parameter(Mandatory = $true)][string]$Section
    )

    if ($Section -notmatch '^[A-Za-z0-9_.-]+$') {
        throw "Unsafe TOML section name: $Section"
    }
    $Found = 0
    $Collect = $false
    $Lines = [Collections.Generic.List[string]]::new()
    foreach ($Line in ($Text -split "`r?`n")) {
        $Header = [Regex]::Match($Line, '^\s*\[([^\]]+)\]\s*(?:#.*)?$')
        if ($Header.Success) {
            $Collect = $Header.Groups[1].Value -ceq $Section
            if ($Collect) {
                $Found += 1
                if ($Found -gt 1) {
                    throw "Duplicate [$Section] section in TOML document"
                }
            }
            continue
        }
        if ($Collect) { $Lines.Add($Line) }
    }
    if ($Found -ne 1) {
        throw "Missing [$Section] section in TOML document"
    }
    return $Lines -join "`n"
}

function Get-CustomsTomlQuotedString {
    param(
        [Parameter(Mandatory = $true)][string]$SectionText,
        [Parameter(Mandatory = $true)][string]$Name,
        [Parameter(Mandatory = $true)][string]$Context
    )

    if ($Name -notmatch '^[A-Za-z0-9_.-]+$') {
        throw "Unsafe TOML key name: $Name"
    }
    $Pattern = '(?m)^\s*' + [Regex]::Escape($Name) + '\s*=\s*"([^"\r\n]+)"\s*(?:#.*)?$'
    $Matches = [Regex]::Matches($SectionText, $Pattern)
    if ($Matches.Count -ne 1) {
        throw "$Context must contain exactly one quoted $Name value"
    }
    return $Matches[0].Groups[1].Value
}

function Get-CustomsTomlTopLevelQuotedString {
    param(
        [Parameter(Mandatory = $true)][string]$Text,
        [Parameter(Mandatory = $true)][string]$Name
    )

    $TopLevel = [Collections.Generic.List[string]]::new()
    foreach ($Line in ($Text -split "`r?`n")) {
        if ($Line -match '^\s*\[') { break }
        $TopLevel.Add($Line)
    }
    return Get-CustomsTomlQuotedString `
        -SectionText ($TopLevel -join "`n") `
        -Name $Name `
        -Context 'TOML top level'
}

function Get-CustomsTomlNonnegativeInteger {
    param(
        [Parameter(Mandatory = $true)][string]$SectionText,
        [Parameter(Mandatory = $true)][string]$Name,
        [Parameter(Mandatory = $true)][string]$Context
    )

    if ($Name -notmatch '^[A-Za-z0-9_.-]+$') {
        throw "Unsafe TOML key name: $Name"
    }
    $Pattern = '(?m)^\s*' + [Regex]::Escape($Name) + '\s*=\s*([0-9]+)\s*(?:#.*)?$'
    $Matches = [Regex]::Matches($SectionText, $Pattern)
    if ($Matches.Count -ne 1) {
        throw "$Context must contain exactly one non-negative integer $Name value"
    }
    $Value = [Int64]0
    if (-not [Int64]::TryParse($Matches[0].Groups[1].Value, [ref]$Value)) {
        throw "$Context $Name value is outside the supported integer range"
    }
    return $Value
}

function Expand-CustomsEnvironmentPathTemplate {
    param(
        [Parameter(Mandatory = $true)][string]$Template,
        [Parameter(Mandatory = $true)][string]$Label
    )

    $Expanded = [Regex]::Replace(
        $Template,
        '\$\{([A-Za-z_][A-Za-z0-9_]*)\}',
        {
            param($Match)
            $Value = [Environment]::GetEnvironmentVariable($Match.Groups[1].Value)
            if ([string]::IsNullOrWhiteSpace($Value)) {
                throw "$Label requires environment variable $($Match.Groups[1].Value)"
            }
            return $Value
        }
    )
    if ($Expanded.Contains('${')) {
        throw "$Label contains an invalid environment template"
    }
    if (-not [IO.Path]::IsPathRooted($Expanded)) {
        throw "$Label must resolve to an absolute path"
    }
    $FullPath = [IO.Path]::GetFullPath($Expanded).TrimEnd('\', '/')
    $Root = [IO.Path]::GetPathRoot($FullPath).TrimEnd('\', '/')
    if ($FullPath -eq $Root) {
        throw "$Label must not resolve to a filesystem root"
    }
    return $FullPath
}

function Test-CustomsPathWithin {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$Parent
    )
    $FullPath = [IO.Path]::GetFullPath($Path).TrimEnd('\', '/')
    $FullParent = [IO.Path]::GetFullPath($Parent).TrimEnd('\', '/')
    return $FullPath.StartsWith(
        $FullParent + [IO.Path]::DirectorySeparatorChar,
        [StringComparison]::OrdinalIgnoreCase
    )
}

function Assert-CustomsPathWithin {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$Parent,
        [Parameter(Mandatory = $true)][string]$Label
    )
    $FullPath = [IO.Path]::GetFullPath($Path).TrimEnd('\', '/')
    if (-not (Test-CustomsPathWithin -Path $FullPath -Parent $Parent)) {
        throw "$Label escaped its required parent: $FullPath"
    }
    return $FullPath
}

function Assert-CustomsNoReparseBetween {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$Boundary,
        [Parameter(Mandatory = $true)][string]$Label
    )
    $Current = [IO.Path]::GetFullPath($Path).TrimEnd('\', '/')
    $FullBoundary = [IO.Path]::GetFullPath($Boundary).TrimEnd('\', '/')
    if ($Current -ne $FullBoundary -and -not (
        Test-CustomsPathWithin -Path $Current -Parent $FullBoundary
    )) {
        throw "$Label escaped its reparse inspection boundary"
    }
    while ($true) {
        if (Test-Path -LiteralPath $Current) {
            $Item = Get-Item -LiteralPath $Current -Force
            if (($Item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
                throw "$Label contains a link or reparse point: $Current"
            }
        }
        if ($Current -eq $FullBoundary) { return }
        $Parent = Split-Path -Parent $Current
        if ([string]::IsNullOrWhiteSpace($Parent) -or $Parent -eq $Current) {
            throw "$Label reparse inspection did not reach its boundary"
        }
        $Current = $Parent.TrimEnd('\', '/')
    }
}

function Assert-CustomsPlainDirectoryTree {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$Boundary,
        [Parameter(Mandatory = $true)][string]$Label
    )

    $FullPath = Assert-CustomsPathWithin -Path $Path -Parent $Boundary -Label $Label
    if (-not (Test-Path -LiteralPath $FullPath -PathType Container)) {
        throw "$Label directory is missing: $FullPath"
    }
    Assert-CustomsNoReparseBetween -Path $FullPath -Boundary $Boundary -Label $Label
    $NestedReparse = Get-ChildItem -LiteralPath $FullPath -Recurse -Force -ErrorAction Stop |
        Where-Object { ($_.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0 } |
        Select-Object -First 1
    if ($null -ne $NestedReparse) {
        throw "$Label contains a link or reparse point: $($NestedReparse.FullName)"
    }
    return $FullPath
}

function Remove-CustomsPlainDirectoryTree {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$Boundary,
        [Parameter(Mandatory = $true)][string]$Label
    )

    $FullPath = Assert-CustomsPlainDirectoryTree `
        -Path $Path -Boundary $Boundary -Label $Label
    Remove-Item -LiteralPath $FullPath -Recurse -Force -ErrorAction Stop
}

function Get-CustomsExternalToolsRoot {
    param(
        [Parameter(Mandatory = $true)][string]$EngineeringRoot,
        [Parameter(Mandatory = $true)][string]$WorkspaceRoot
    )

    $LayoutPath = Join-Path $EngineeringRoot 'config\local-layout.toml'
    if (-not (Test-Path -LiteralPath $LayoutPath -PathType Leaf)) {
        throw "Local layout configuration is missing: $LayoutPath"
    }
    $LayoutText = [IO.File]::ReadAllText($LayoutPath)
    $External = Get-CustomsTomlSectionText -Text $LayoutText -Section 'external'
    $CacheTemplate = Get-CustomsTomlQuotedString `
        -SectionText $External -Name 'cache_root' -Context '[external]'
    $ToolsTemplate = Get-CustomsTomlQuotedString `
        -SectionText $External -Name 'tools_root' -Context '[external]'
    $CacheRoot = Expand-CustomsEnvironmentPathTemplate `
        -Template $CacheTemplate -Label 'external.cache_root'
    $ToolsRoot = Expand-CustomsEnvironmentPathTemplate `
        -Template $ToolsTemplate -Label 'external.tools_root'
    if (-not (Test-CustomsPathWithin -Path $ToolsRoot -Parent $CacheRoot)) {
        throw 'external.tools_root must be strictly below external.cache_root'
    }
    $FullWorkspace = [IO.Path]::GetFullPath($WorkspaceRoot).TrimEnd('\', '/')
    if (
        (Test-CustomsPathWithin -Path $ToolsRoot -Parent $FullWorkspace) -or
        (Test-CustomsPathWithin -Path $FullWorkspace -Parent $ToolsRoot) -or
        $ToolsRoot -eq $FullWorkspace
    ) {
        throw 'external.tools_root must be outside the workspace'
    }
    Assert-CustomsNoReparseBetween `
        -Path $ToolsRoot -Boundary $CacheRoot -Label 'External tool cache'
    return $ToolsRoot
}

function Get-CustomsToolTreeInventory {
    param(
        [Parameter(Mandatory = $true)][string]$Root,
        [Parameter(Mandatory = $true)][string]$ToolsRoot,
        [Parameter(Mandatory = $true)][string]$Label
    )

    $FullRoot = Assert-CustomsPathWithin -Path $Root -Parent $ToolsRoot -Label $Label
    if (-not (Test-Path -LiteralPath $FullRoot -PathType Container)) {
        throw "$Label tree root is missing: $FullRoot"
    }
    Assert-CustomsNoReparseBetween -Path $FullRoot -Boundary $ToolsRoot -Label $Label
    $Items = @(Get-ChildItem -LiteralPath $FullRoot -Recurse -Force -ErrorAction Stop)
    $Reparse = $Items |
        Where-Object { ($_.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0 } |
        Select-Object -First 1
    if ($null -ne $Reparse) {
        throw "$Label tree contains a link or reparse point: $($Reparse.FullName)"
    }

    $Lines = [Collections.Generic.List[string]]::new()
    $FileCount = 0
    $Bytes = [Int64]0
    foreach ($File in $Items | Where-Object { -not $_.PSIsContainer }) {
        $Relative = $File.FullName.Substring($FullRoot.Length + 1).Replace('\', '/')
        if ($Relative -notmatch '^[\x20-\x7e]+$' -or $Relative.Contains('|')) {
            throw "$Label tree contains a path outside the canonical ASCII inventory format"
        }
        $Length = [Int64]$File.Length
        $Digest = (Get-FileHash -LiteralPath $File.FullName -Algorithm SHA256).Hash.ToLowerInvariant()
        $Lines.Add("$Relative|$Length|$Digest")
        $FileCount += 1
        $Bytes += $Length
    }
    $Lines.Sort([StringComparer]::Ordinal)
    $Payload = [Text.Encoding]::UTF8.GetBytes(($Lines -join "`n") + "`n")
    $Hasher = [Security.Cryptography.SHA256]::Create()
    try {
        $TreeSha256 = ([BitConverter]::ToString($Hasher.ComputeHash($Payload))).Replace('-', '').ToLowerInvariant()
    }
    finally {
        $Hasher.Dispose()
    }
    return [pscustomobject]@{
        tree_sha256 = $TreeSha256
        file_count = $FileCount
        bytes = $Bytes
    }
}

function Enter-CustomsToolBootstrapLock {
    param(
        [Parameter(Mandatory = $true)][string]$ToolRoot,
        [Parameter(Mandatory = $true)][string]$ToolsRoot,
        [Parameter(Mandatory = $true)][string]$ToolName
    )

    if ($ToolName -notmatch '^[a-z][a-z0-9-]{0,31}$') {
        throw 'Tool bootstrap lock name is invalid.'
    }
    $FullToolRoot = Assert-CustomsPathWithin `
        -Path $ToolRoot -Parent $ToolsRoot -Label "$ToolName tool root"
    Assert-CustomsNoReparseBetween `
        -Path $FullToolRoot -Boundary $ToolsRoot -Label "$ToolName tool root"
    New-Item -ItemType Directory -Force -Path $FullToolRoot | Out-Null
    $LockPath = Assert-CustomsPathWithin `
        -Path (Join-Path $FullToolRoot '.bootstrap.lock') `
        -Parent $FullToolRoot `
        -Label "$ToolName bootstrap lock"
    try {
        New-Item -ItemType Directory -Path $LockPath -ErrorAction Stop | Out-Null
    }
    catch {
        throw "$ToolName bootstrap is already running or requires stale-lock review: $LockPath"
    }
    $Nonce = [Guid]::NewGuid().ToString('N')
    $Marker = [ordered]@{
        kind = 'customs-uat-tool-bootstrap-lock'
        schema_version = '1.0'
        tool = $ToolName
        pid = $PID
        nonce = $Nonce
        created_at_utc = [DateTime]::UtcNow.ToString('o')
    }
    try {
        [IO.File]::WriteAllText(
            (Join-Path $LockPath 'owner.json'),
            (($Marker | ConvertTo-Json -Compress) + "`n"),
            [Text.UTF8Encoding]::new($false)
        )
    }
    catch {
        if (Test-Path -LiteralPath $LockPath -PathType Container) {
            Remove-CustomsPlainDirectoryTree `
                -Path $LockPath -Boundary $FullToolRoot -Label "$ToolName failed bootstrap lock"
        }
        throw
    }
    return [pscustomobject]@{
        path = $LockPath
        nonce = $Nonce
        tool = $ToolName
        pid = $PID
    }
}

function Exit-CustomsToolBootstrapLock {
    param(
        [Parameter(Mandatory = $true)]$Lock,
        [Parameter(Mandatory = $true)][string]$ToolRoot
    )

    $LockPath = Assert-CustomsPathWithin `
        -Path ([string]$Lock.path) -Parent $ToolRoot -Label 'Tool bootstrap lock release'
    Assert-CustomsNoReparseBetween `
        -Path $LockPath -Boundary $ToolRoot -Label 'Tool bootstrap lock release'
    $MarkerPath = Join-Path $LockPath 'owner.json'
    if (-not (Test-Path -LiteralPath $MarkerPath -PathType Leaf)) {
        throw 'Tool bootstrap lock owner marker is missing; refusing to remove it.'
    }
    try {
        $Marker = Get-Content -Raw -Encoding UTF8 -LiteralPath $MarkerPath | ConvertFrom-Json
    }
    catch {
        throw 'Tool bootstrap lock owner marker is unreadable; refusing to remove it.'
    }
    if (
        $Marker.kind -ne 'customs-uat-tool-bootstrap-lock' -or
        $Marker.schema_version -ne '1.0' -or
        $Marker.tool -ne $Lock.tool -or
        $Marker.nonce -ne $Lock.nonce -or
        [int]$Marker.pid -ne [int]$Lock.pid
    ) {
        throw 'Tool bootstrap lock ownership changed; refusing to remove it.'
    }
    $LockEntries = @(Get-ChildItem -LiteralPath $LockPath -Force)
    if ($LockEntries.Count -ne 1 -or $LockEntries[0].Name -ne 'owner.json') {
        throw 'Tool bootstrap lock contains unexpected entries; refusing to remove it.'
    }
    Remove-CustomsPlainDirectoryTree `
        -Path $LockPath -Boundary $ToolRoot -Label 'Tool bootstrap lock release'
}

function Install-CustomsToolCandidate {
    param(
        [Parameter(Mandatory = $true)][string]$CandidateRoot,
        [Parameter(Mandatory = $true)][string]$InstallRoot,
        [Parameter(Mandatory = $true)][string]$ToolRoot,
        [Parameter(Mandatory = $true)][string]$ToolsRoot,
        [Parameter(Mandatory = $true)][string]$Label,
        [Parameter(Mandatory = $true)][scriptblock]$VerifyInstalled
    )

    $FullCandidate = Assert-CustomsPathWithin `
        -Path $CandidateRoot -Parent $ToolRoot -Label "$Label candidate"
    $FullInstall = Assert-CustomsPathWithin `
        -Path $InstallRoot -Parent $ToolRoot -Label "$Label install"
    if ($FullCandidate -eq $FullInstall) {
        throw "$Label candidate and install paths must differ"
    }
    Assert-CustomsNoReparseBetween `
        -Path $FullCandidate -Boundary $ToolsRoot -Label "$Label candidate"
    Assert-CustomsNoReparseBetween `
        -Path $FullInstall -Boundary $ToolsRoot -Label "$Label install"
    if (-not (Test-Path -LiteralPath $FullCandidate -PathType Container)) {
        throw "$Label candidate directory is missing"
    }
    Assert-CustomsPlainDirectoryTree `
        -Path $FullCandidate -Boundary $ToolsRoot -Label "$Label candidate" | Out-Null

    $BackupRoot = Assert-CustomsPathWithin `
        -Path (Join-Path $ToolRoot ('.rollback-' + [Guid]::NewGuid().ToString('N'))) `
        -Parent $ToolRoot `
        -Label "$Label rollback"
    $HadExisting = Test-Path -LiteralPath $FullInstall
    if ($HadExisting) {
        Assert-CustomsPlainDirectoryTree `
            -Path $FullInstall -Boundary $ToolsRoot -Label "$Label existing install" | Out-Null
        Move-Item -LiteralPath $FullInstall -Destination $BackupRoot -ErrorAction Stop
    }
    try {
        Move-Item -LiteralPath $FullCandidate -Destination $FullInstall -ErrorAction Stop
        & $VerifyInstalled $FullInstall
    }
    catch {
        $OriginalError = $_
        try {
            if (Test-Path -LiteralPath $FullInstall) {
                Remove-CustomsPlainDirectoryTree `
                    -Path $FullInstall -Boundary $ToolsRoot -Label "$Label failed install"
            }
            if ($HadExisting -and (Test-Path -LiteralPath $BackupRoot)) {
                Move-Item -LiteralPath $BackupRoot -Destination $FullInstall -ErrorAction Stop
            }
        }
        catch {
            throw "$Label transition failed and rollback requires manual review: $($_.Exception.Message)"
        }
        throw $OriginalError
    }
    if ($HadExisting -and (Test-Path -LiteralPath $BackupRoot)) {
        Remove-CustomsPlainDirectoryTree `
            -Path $BackupRoot -Boundary $ToolsRoot -Label "$Label verified rollback backup"
    }
}
