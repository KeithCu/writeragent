#Requires -Version 5.1
<#
.SYNOPSIS
    Clean / repair the LibreOffice extension cache for localwriter.

.DESCRIPTION
    Fixes common cache corruption issues: revoked flags, stale lock files,
    ghost installs, and bundled junction conflicts.

.EXAMPLE
    .\clean-cache.ps1              # Fix revoked flags + remove stale locks
    .\clean-cache.ps1 -Nuke        # Wipe the entire user extension cache
    .\clean-cache.ps1 -Unbundle    # Remove bundled junction from LO share\extensions (needs Admin)
#>

[CmdletBinding()]
param(
    [switch]$Nuke,
    [switch]$Unbundle
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$ProjectRoot  = Split-Path $PSScriptRoot
$ExtensionId  = "org.extension.writeragent"
$ExtensionOxt = "WriterAgent.oxt"
$BundleName   = "WriterAgent"

# ── Helpers ──────────────────────────────────────────────────────────────────

function Write-Step  { param([string]$T) Write-Host "[*] $T" -ForegroundColor White }
function Write-OK    { param([string]$T) Write-Host "[OK] $T" -ForegroundColor Green }
function Write-Warn  { param([string]$T) Write-Host "[!!] $T" -ForegroundColor Yellow }
function Write-Err   { param([string]$T) Write-Host "[X] $T" -ForegroundColor Red }
function Write-Info  { param([string]$T) Write-Host "    $T" -ForegroundColor Gray }

# ── Cache probing ────────────────────────────────────────────────────────────

function Find-CacheDir {
    # Standard location
    $candidate = Join-Path $env:APPDATA "LibreOffice\4\user\uno_packages"
    if (Test-Path $candidate) { return $candidate }
    # Fallback: search under the profile
    $profileDir = Join-Path $env:APPDATA "LibreOffice"
    if (Test-Path $profileDir) {
        $found = Get-ChildItem $profileDir -Recurse -Directory -Filter "uno_packages" -ErrorAction SilentlyContinue | Select-Object -First 1
        if ($found) { return $found.FullName }
    }
    return $null
}

function Find-LOExtDir {
    foreach ($p in @(
        "${env:ProgramFiles}\LibreOffice\share\extensions",
        "${env:ProgramFiles(x86)}\LibreOffice\share\extensions",
        "C:\Program Files\LibreOffice\share\extensions"
    )) {
        if (Test-Path $p) { return $p }
    }
    return $null
}

# ── Unbundle mode ────────────────────────────────────────────────────────────

if ($Unbundle) {
    $loExtDir = Find-LOExtDir
    if (-not $loExtDir) {
        Write-Err "LibreOffice share\extensions not found"
        exit 1
    }
    $junctionPath = Join-Path $loExtDir $BundleName
    if (Test-Path $junctionPath) {
        $item = Get-Item $junctionPath -Force
        if ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) {
            Write-Step "Removing bundled junction: $junctionPath -> $($item.Target)"
            cmd /c "rmdir `"$junctionPath`"" 2>$null
            Write-OK "Bundled junction removed"
        } else {
            Write-Warn "$junctionPath is a real directory (not a junction), skipping"
            Write-Info "Remove manually if needed: Remove-Item '$junctionPath' -Recurse -Force"
        }
    } else {
        Write-OK "No bundled junction to remove"
    }
    exit 0
}

# ── Find cache ───────────────────────────────────────────────────────────────

$CacheDir = Find-CacheDir
if (-not $CacheDir) {
    Write-Err "Could not find uno_packages cache directory"
    exit 1
}
Write-OK "Cache dir: $CacheDir"

# ── Nuke mode ────────────────────────────────────────────────────────────────

if ($Nuke) {
    Write-Host ""
    Write-Warn "Wiping entire user extension cache."
    Write-Info "You will need to reinstall extensions with unopkg afterwards."

    # Remove lock first
    $loLock = Join-Path (Split-Path (Split-Path $CacheDir)) ".lock"
    if (Test-Path $loLock) { Remove-Item $loLock -Force -ErrorAction SilentlyContinue }

    $cacheSub = Join-Path $CacheDir "cache"
    if (Test-Path $cacheSub) { Remove-Item $cacheSub -Recurse -Force }
    $pmap = Join-Path $CacheDir "uno_packages.pmap"
    if (Test-Path $pmap) { Remove-Item $pmap -Force }

    Write-OK "Cache wiped: $cacheSub"
    Write-Info "Restart LibreOffice to regenerate, then reinstall extensions."
    exit 0
}

# ── Repair mode (default) ─────────────────────────────────────────────────

Write-Host ""
Write-Host "=== Repairing extension cache ===" -ForegroundColor Cyan
Write-Host ""

$fixed = 0

# 1. Remove stale lock files
Write-Step "Checking for stale locks..."
$lockFiles = Get-ChildItem $CacheDir -Recurse -Filter "*.lock" -ErrorAction SilentlyContinue
foreach ($lock in $lockFiles) {
    try {
        Remove-Item $lock.FullName -Force
        Write-Info "Removed: $($lock.FullName)"
        $fixed++
    } catch {
        Write-Warn "Could not remove lock: $($lock.FullName)"
    }
}

# Also check the LO user profile lock (only if LO is not running)
$loLock = Join-Path (Split-Path (Split-Path $CacheDir)) ".lock"
$loRunning = $null -ne (Get-Process -Name "soffice*" -ErrorAction SilentlyContinue)
if ((Test-Path $loLock) -and (-not $loRunning)) {
    Remove-Item $loLock -Force -ErrorAction SilentlyContinue
    Write-Info "Removed stale LO lock: $loLock"
    $fixed++
}

# 2. Fix revoked flags in backenddb files
Write-Step "Checking for revoked extensions..."
$registryDir = Join-Path $CacheDir "cache\registry"
if (Test-Path $registryDir) {
    $dbFiles = Get-ChildItem $registryDir -Recurse -Filter "backenddb.xml" -ErrorAction SilentlyContinue
    foreach ($db in $dbFiles) {
        $content = Get-Content $db.FullName -Raw -Encoding UTF8
        if ($content -match 'revoked="true"') {
            $content = $content -replace ' revoked="true"', ''
            $utf8 = New-Object System.Text.UTF8Encoding($false)
            [System.IO.File]::WriteAllText($db.FullName, $content, $utf8)
            Write-Info "Fixed revoked flags: $(Split-Path (Split-Path $db.FullName) -Leaf)"
            $fixed++
        }
    }
}

# 3. Check for ghost installs
Write-Step "Checking for ghost installs..."
$packagesDir = Join-Path $CacheDir "cache\uno_packages"
if (Test-Path $packagesDir) {
    Get-ChildItem $packagesDir -Directory -Filter "*.tmp_" -ErrorAction SilentlyContinue | ForEach-Object {
        $oxtDir = Join-Path $_.FullName $ExtensionOxt
        if (Test-Path $oxtDir) {
            $hasVersion = Test-Path (Join-Path $oxtDir "plugin\version.py")
            $hasReg     = Test-Path (Join-Path $oxtDir "registration.py")
            if ((-not $hasVersion) -and (-not $hasReg)) {
                Write-Info "Ghost install found: $($_.Name)"
                Write-Info "Run -Nuke to clean up, or reinstall with: install-plugin.ps1 -Force"
                $fixed++
            }
        }
    }
}

# 4. Check for bundled junction conflict
Write-Step "Checking for bundled junction conflict..."
$loExtDir = Find-LOExtDir
if ($loExtDir) {
    $junctionPath = Join-Path $loExtDir $BundleName
    if (Test-Path $junctionPath) {
        $item = Get-Item $junctionPath -Force
        if ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) {
            Write-Warn "Bundled junction found: $junctionPath -> $($item.Target)"
            Write-Info "This conflicts with the user-installed extension."
            Write-Info "Run: .\clean-cache.ps1 -Unbundle  (needs Admin)"
            $fixed++
        }
    }
}

# Report
Write-Host ""
if ($fixed -eq 0) {
    Write-OK "Cache looks clean, nothing to fix."
} else {
    Write-OK "Fixed $fixed issue(s). Restart LibreOffice to apply."
}
Write-Host ""
