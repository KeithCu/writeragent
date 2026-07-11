# Build and install the WriterAgent extension (.oxt).
#
# Adapted from mcp-libre/scripts/install-plugin.sh.
#
# Usage:
#   .\scripts\install-plugin.ps1                          # Build + install (interactive)
#   .\scripts\install-plugin.ps1 -Force                   # Build + install (no prompts, kills LO)
#   .\scripts\install-plugin.ps1 -BuildOnly               # Only create the .oxt
#   .\scripts\install-plugin.ps1 -Uninstall               # Remove the extension
#   .\scripts\install-plugin.ps1 -Cache                   # Hot-deploy to LO cache (dev iteration)
#   .\scripts\install-plugin.ps1 -Modules "core mcp"      # Build specific modules (default: auto-discover all)

param(
    [switch]$Force,
    [switch]$BuildOnly,
    [switch]$Uninstall,
    [switch]$Cache,
    [string]$Modules = "",
    [switch]$Help
)

if ($Help) {
    Write-Host "Usage: .\scripts\install-plugin.ps1 [-Force] [-BuildOnly] [-Uninstall] [-Cache] [-Modules `"core mcp`"]"
    exit 0
}

$ErrorActionPreference = "Stop"

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$ProjectRoot = Split-Path -Parent $ScriptDir
$BuildDir = Join-Path $ProjectRoot "build"
$OxtFile = Join-Path $BuildDir "WriterAgent.oxt"
$ExtensionId = "org.extension.writeragent"

# ── Helpers ──────────────────────────────────────────────────────────────────

function Confirm-OrForce {
    param([string]$Prompt)
    if ($Force) { return $true }
    $response = Read-Host "$Prompt (Y/n)"
    return ($response -eq "" -or $response -match "^[Yy]")
}

function Find-Unopkg {
    $candidates = @(
        "${env:ProgramFiles}\LibreOffice\program\unopkg.exe",
        "${env:ProgramFiles(x86)}\LibreOffice\program\unopkg.exe",
        "C:\Program Files\LibreOffice\program\unopkg.exe",
        "C:\Program Files (x86)\LibreOffice\program\unopkg.exe"
    )
    foreach ($c in $candidates) {
        if (Test-Path $c) { return $c }
    }
    $cmd = Get-Command unopkg -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }
    return $null
}

function Test-LORunning {
    return $null -ne (Get-Process -Name "soffice.bin" -ErrorAction SilentlyContinue)
}

function Stop-LibreOffice {
    Write-Host "[*] Closing LibreOffice..."
    for ($attempt = 1; $attempt -le 3; $attempt++) {
        Get-Process -Name "soffice", "soffice.bin", "oosplash" -ErrorAction SilentlyContinue |
            Stop-Process -Force -ErrorAction SilentlyContinue
        Start-Sleep -Seconds 2
        if (-not (Test-LORunning)) {
            Write-Host "[OK] LibreOffice closed"
            return
        }
        Write-Host "    Attempt $attempt/3 - processes still running, retrying..."
        Start-Sleep -Seconds 2
    }
    if (Test-LORunning) {
        throw "[X] Could not close LibreOffice after 3 attempts"
    }
    Write-Host "[OK] LibreOffice closed"
}

function Confirm-LOStopped {
    if (-not (Test-LORunning)) { return }
    Write-Host "[!!] LibreOffice is running. It must be closed for unopkg."
    if (-not (Confirm-OrForce "Close LibreOffice now?")) {
        throw "[X] Cannot proceed while LibreOffice is running."
    }
    Stop-LibreOffice
}

# ── Build .oxt ───────────────────────────────────────────────────────────────

function Build-Oxt {
    $moduleLabel = if ($Modules) { $Modules } else { "auto-discover all" }
    Write-Host ""
    Write-Host "=== Building WriterAgent.oxt (modules: $moduleLabel) ==="
    Write-Host ""

    if (-not (Test-Path $BuildDir)) {
        New-Item -ItemType Directory -Path $BuildDir -Force | Out-Null
    }
    if (Test-Path $OxtFile) {
        Remove-Item -Path $OxtFile -Force
    }

    # Generate manifests from module.yaml files
    $generateScript = Join-Path $ScriptDir "generate_manifest.py"
    & python $generateScript

    # Build the .oxt
    $buildScript = Join-Path $ScriptDir "build_oxt.py"
    if ($Modules) {
        $moduleArgs = $Modules -split "\s+"
        & python $buildScript --modules @moduleArgs --output $OxtFile
    } else {
        & python $buildScript --output $OxtFile
    }

    if (Test-Path $OxtFile) {
        $size = (Get-Item $OxtFile).Length
        Write-Host "[OK] Built: $OxtFile ($size bytes)"
    } else {
        throw "[X] Failed to create .oxt file"
    }
}

# ── Install / Uninstall ─────────────────────────────────────────────────────

function Install-Extension {
    param([string]$Unopkg)

    Write-Host ""
    Write-Host "=== Installing Extension ==="
    Write-Host ""

    Confirm-LOStopped

    # Clear stale profile locks (same as make register-built-oxt)
    $loConf = Join-Path $env:APPDATA "LibreOffice\4"
    foreach ($lock in @(
            (Join-Path $loConf ".lock"),
            (Join-Path $loConf "user\.lock")
        )) {
        if (Test-Path $lock) { Remove-Item -Path $lock -Force -ErrorAction SilentlyContinue }
    }
    $extTmp = Join-Path $loConf "user\extensions\tmp"
    $pmap = Join-Path $extTmp "extensions.pmap"
    if (Test-Path $pmap) { Remove-Item -Path $pmap -Force -ErrorAction SilentlyContinue }
    Get-ChildItem -Path $extTmp -Filter "*.tmp_" -ErrorAction SilentlyContinue |
        Remove-Item -Recurse -Force -ErrorAction SilentlyContinue

    # Remove previous version (ignore error if not installed)
    Write-Host "[*] Removing previous version (if any)..."
    try { & $Unopkg remove $ExtensionId 2>&1 | Out-Null } catch { }

    Start-Sleep -Seconds 2

    # Install new version
    Write-Host "[*] Installing $OxtFile ..."
    $result = & $Unopkg add $OxtFile 2>&1
    if ($LASTEXITCODE -ne 0) {
        Write-Host "[X] unopkg add failed"
        Write-Host "    Troubleshooting:"
        Write-Host "    1. Make sure LibreOffice is fully closed"
        Write-Host "    2. Try: .\scripts\install-plugin.ps1 -Uninstall -Force"
        Write-Host "    3. Then: .\scripts\install-plugin.ps1 -Force"
        throw "Installation failed"
    }

    Write-Host "[OK] Extension installed successfully!"

    Start-Sleep -Seconds 2
    Write-Host "[*] Verifying installation..."
    $list = & $Unopkg list 2>&1
    if ($list -match [regex]::Escape($ExtensionId)) {
        Write-Host "[OK] Extension verified: $ExtensionId is registered"
    } else {
        Write-Host "[!!] Could not verify via unopkg list (often OK, LO will load it on start)"
    }
}

function Uninstall-Extension {
    param([string]$Unopkg)

    Write-Host ""
    Write-Host "=== Uninstalling Extension ==="
    Write-Host ""

    Confirm-LOStopped

    Write-Host "[*] Removing extension $ExtensionId ..."
    $result = & $Unopkg remove $ExtensionId 2>&1 | Out-String
    if ($result -match "not deployed|no such|aucune") {
        Write-Host "    Extension was not installed"
    } else {
        Write-Host "[OK] Extension removed"
    }
}

# ── Cache install (hot-deploy) ───────────────────────────────────────────────

function Find-UnopkgCacheDir {
    $profileDir = Join-Path $env:APPDATA "LibreOffice"
    if (-not (Test-Path $profileDir)) { return $null }

    # Search for uno_packages directories
    $found = Get-ChildItem -Path $profileDir -Directory -Recurse -Filter "uno_packages" -ErrorAction SilentlyContinue |
        Select-Object -First 1
    if ($found) { return $found.FullName }
    return $null
}

function Test-ExtensionRegistered {
    param([string]$Unopkg)
    $list = & $Unopkg list 2>&1 | Out-String
    return ($list -match [regex]::Escape($ExtensionId))
}

function Install-ToCache {
    Write-Host ""
    Write-Host "=== Cache Install (hot-deploy) ==="
    Write-Host ""

    $unopkg = Find-Unopkg
    if (-not $unopkg) {
        Write-Host "[X] unopkg not found. Install LibreOffice first."
        exit 1
    }

    $cacheDir = Find-UnopkgCacheDir
    $extDir = $null
    $needsFullInstall = -not (Test-ExtensionRegistered -Unopkg $unopkg)

    if (-not $needsFullInstall) {
        if (-not $cacheDir) {
            $needsFullInstall = $true
        } else {
            $packagesDir = Join-Path $cacheDir "cache\uno_packages"
            if (-not (Test-Path $packagesDir)) {
                $needsFullInstall = $true
            } else {
                $tmpDirs = Get-ChildItem -Path $packagesDir -Directory -Filter "*.tmp_" -ErrorAction SilentlyContinue
                foreach ($d in $tmpDirs) {
                    $oxtDir = Join-Path $d.FullName "WriterAgent.oxt"
                    if (Test-Path $oxtDir) {
                        $extDir = $oxtDir
                        break
                    }
                }
                if (-not $extDir) {
                    $needsFullInstall = $true
                }
            }
        }
    }

    if ($needsFullInstall) {
        if (-not (Test-ExtensionRegistered -Unopkg $unopkg)) {
            Write-Host "[!] Extension not registered. Performing one-time unopkg install..."
        } else {
            Write-Host "[!] Extension cache not found. Re-registering via unopkg..."
        }
        $script:Force = $true
        if (Test-Path $OxtFile) {
            Write-Host "[OK] Using existing $OxtFile (from make build)"
        } else {
            Build-Oxt
        }
        Install-Extension -Unopkg $unopkg

        $cacheDir = Find-UnopkgCacheDir
        if ($cacheDir) {
            $packagesDir = Join-Path $cacheDir "cache\uno_packages"
            $tmpDirs = Get-ChildItem -Path $packagesDir -Directory -Filter "*.tmp_" -ErrorAction SilentlyContinue
            foreach ($d in $tmpDirs) {
                $oxtDir = Join-Path $d.FullName "WriterAgent.oxt"
                if (Test-Path $oxtDir) {
                    $extDir = $oxtDir
                    break
                }
            }
        }
        if (-not $extDir) {
            Write-Host "[X] Extension still not found in cache after installation."
            exit 1
        }
    }

    Write-Host "[OK] Cache dir: $extDir"

    $deployed = 0

    # plugin/
    $pluginSrc = Join-Path $ProjectRoot "plugin"
    $pluginDst = Join-Path $extDir "plugin"
    if (Test-Path $pluginDst) { Remove-Item -Path $pluginDst -Recurse -Force }
    Copy-Item -Path $pluginSrc -Destination $pluginDst -Recurse -Force
    Get-ChildItem -Path $pluginDst -Recurse -Include "__pycache__", "*.pyc", "module.yaml" -ErrorAction SilentlyContinue |
        Remove-Item -Recurse -Force -ErrorAction SilentlyContinue
    Write-Host "    plugin/ synced"
    $deployed++

    # extension/ resources -> .oxt root
    foreach ($f in @("Addons.xcu", "Accelerators.xcu", "description.xml", "XPythonFunction.rdb", "XPromptFunction.rdb")) {
        $src = Join-Path $ProjectRoot "extension\$f"
        if (Test-Path $src) {
            Copy-Item -Path $src -Destination (Join-Path $extDir $f) -Force
            Write-Host "    $f"
            $deployed++
        }
    }
    foreach ($dir in @("META-INF", "assets", "registration", "registry")) {
        $src = Join-Path $ProjectRoot "extension\$dir"
        if (Test-Path $src) {
            $dst = Join-Path $extDir $dir
            if (Test-Path $dst) { Remove-Item -Path $dst -Recurse -Force }
            Copy-Item -Path $src -Destination $dst -Recurse -Force
            Write-Host "    $dir/ synced"
            $deployed++
        }
    }

    # Sync dialogs (static + dynamically generated)
    $staticDialogs = Join-Path $ProjectRoot "extension\WriterAgentDialogs"
    $generatedDialogs = Join-Path $ProjectRoot "build\generated\WriterAgentDialogs"
    if (Test-Path $staticDialogs) {
        $dst = Join-Path $extDir "WriterAgentDialogs"
        if (Test-Path $dst) { Remove-Item -Path $dst -Recurse -Force }
        Copy-Item -Path $staticDialogs -Destination $dst -Recurse -Force
        if (Test-Path $generatedDialogs) {
            Copy-Item -Path (Join-Path $generatedDialogs "*") -Destination $dst -Recurse -Force
        }
        Write-Host "    WriterAgentDialogs/ synced"
        $deployed++
    } elseif (Test-Path $generatedDialogs) {
        $dst = Join-Path $extDir "WriterAgentDialogs"
        if (Test-Path $dst) { Remove-Item -Path $dst -Recurse -Force }
        Copy-Item -Path $generatedDialogs -Destination $dst -Recurse -Force
        Write-Host "    WriterAgentDialogs/ (generated) synced"
        $deployed++
    }

    $generatedDialogPages = Join-Path $ProjectRoot "build\generated\dialogs"
    if (Test-Path $generatedDialogPages) {
        $dst = Join-Path $extDir "dialogs"
        if (Test-Path $dst) { Remove-Item -Path $dst -Recurse -Force }
        Copy-Item -Path $generatedDialogPages -Destination $dst -Recurse -Force
        Write-Host "    dialogs/ synced"
        $deployed++
    }

    # Clean __pycache__
    Get-ChildItem -Path $extDir -Directory -Recurse -Filter "__pycache__" -ErrorAction SilentlyContinue |
        Remove-Item -Recurse -Force -ErrorAction SilentlyContinue

    Write-Host ""
    Write-Host "[OK] Deployed $deployed items to cache"
    Write-Host "    Restart LibreOffice to pick up changes."
    Write-Host ""
}

# ── Main ─────────────────────────────────────────────────────────────────────

Write-Host ""
Write-Host "========================================"
Write-Host "  WriterAgent Plugin Installer"
Write-Host "========================================"
Write-Host ""

# Cache mode
if ($Cache) {
    Install-ToCache
    exit 0
}

# Find unopkg
$unopkg = Find-Unopkg
if (-not $unopkg) {
    Write-Host "[X] unopkg not found. Install LibreOffice first."
    exit 1
}
Write-Host "[OK] unopkg: $unopkg"

# Uninstall mode
if ($Uninstall) {
    Uninstall-Extension -Unopkg $unopkg
    exit 0
}

# Build
Build-Oxt

if ($BuildOnly) {
    Write-Host ""
    Write-Host "[OK] Build complete. Install manually with:"
    Write-Host "    & `"$unopkg`" add `"$OxtFile`""
    exit 0
}

# Install
Install-Extension -Unopkg $unopkg

# Restart LibreOffice?
if (Confirm-OrForce "Start LibreOffice now?") {
    Write-Host "[*] Starting LibreOffice..."
    Start-Process soffice
    Write-Host "[OK] LibreOffice started"
}

Write-Host ""
Write-Host "========================================"
Write-Host "  Done!"
Write-Host "========================================"
Write-Host ""
