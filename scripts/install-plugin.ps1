# Build and install the LocalWriter extension (.oxt).
#
# Adapted from mcp-libre/scripts/install-plugin.sh.
#
# Usage:
#   .\scripts\install-plugin.ps1                          # Build + install (interactive)
#   .\scripts\install-plugin.ps1 -Force                   # Build + install (no prompts, kills LO)
#   .\scripts\install-plugin.ps1 -BuildOnly               # Only create the .oxt
#   .\scripts\install-plugin.ps1 -Uninstall               # Remove the extension
#   .\scripts\install-plugin.ps1 -Cache                   # Hot-deploy to LO cache (dev iteration)
#   .\scripts\install-plugin.ps1 -Modules "core mcp"      # Build specific modules

param(
    [switch]$Force,
    [switch]$BuildOnly,
    [switch]$Uninstall,
    [switch]$Cache,
    [string]$Modules = "core writer calc draw ai_openai ai_ollama ai_horde chatbot mcp",
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
    Write-Host ""
    Write-Host "=== Building WriterAgent.oxt (modules: $Modules) ==="
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
    $moduleArgs = $Modules -split "\s+"
    & python $buildScript --modules @moduleArgs --output $OxtFile

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

function Install-ToCache {
    Write-Host ""
    Write-Host "=== Cache Install (hot-deploy) ==="
    Write-Host ""

    $cacheDir = Find-UnopkgCacheDir
    if (-not $cacheDir) {
        Write-Host "[X] Could not find uno_packages cache directory"
        Write-Host "    Run a normal install first: .\scripts\install-plugin.ps1 -Force"
        exit 1
    }

    $packagesDir = Join-Path $cacheDir "cache\uno_packages"
    if (-not (Test-Path $packagesDir)) {
        Write-Host "[X] Cache packages dir not found: $packagesDir"
        Write-Host "    Run a normal install first: .\scripts\install-plugin.ps1 -Force"
        exit 1
    }

    # Find the *.tmp_ directory containing our extension
    $extDir = $null
    $tmpDirs = Get-ChildItem -Path $packagesDir -Directory -Filter "*.tmp_" -ErrorAction SilentlyContinue
    foreach ($d in $tmpDirs) {
        $oxtDir = Join-Path $d.FullName "WriterAgent.oxt"
        if (Test-Path $oxtDir) {
            $extDir = $oxtDir
            break
        }
    }
    if (-not $extDir) {
        Write-Host "[X] Extension not found in cache. Run a normal install first."
        exit 1
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
    foreach ($f in @("Addons.xcu", "Accelerators.xcu", "description.xml", "XPromptFunction.rdb")) {
        $src = Join-Path $ProjectRoot "extension\$f"
        if (Test-Path $src) {
            Copy-Item -Path $src -Destination (Join-Path $extDir $f) -Force
            Write-Host "    $f"
            $deployed++
        }
    }
    foreach ($dir in @("META-INF", "assets", "registration", "registry", "WriterAgentDialogs")) {
        $src = Join-Path $ProjectRoot "extension\$dir"
        if (Test-Path $src) {
            $dst = Join-Path $extDir $dir
            if (Test-Path $dst) { Remove-Item -Path $dst -Recurse -Force }
            Copy-Item -Path $src -Destination $dst -Recurse -Force
            Write-Host "    $dir/ synced"
            $deployed++
        }
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
Write-Host "  LocalWriter Plugin Installer"
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
