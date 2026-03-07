# check-setup.ps1 — Verify the LocalWriter development stack (Windows).
#
# Usage:
#   .\scripts\check-setup.ps1
#   powershell -ExecutionPolicy Bypass -File scripts\check-setup.ps1

$ErrorActionPreference = "Continue"

$script:Errors = 0
$script:Warnings = 0
$script:Brief = @()

function Write-Ok($msg) {
    Write-Host "  OK   " -NoNewline -ForegroundColor Green; Write-Host $msg
    $script:Brief += "OK   $msg"
}
function Write-Warn($msg) {
    Write-Host "  WARN " -NoNewline -ForegroundColor Yellow; Write-Host $msg
    $script:Warnings++
    $script:Brief += "WARN $msg"
}
function Write-Fail($msg) {
    Write-Host "  FAIL " -NoNewline -ForegroundColor Red; Write-Host $msg
    $script:Errors++
    $script:Brief += "FAIL $msg"
}

$ProjectRoot = Split-Path -Parent (Split-Path -Parent $PSCommandPath)

Write-Host ""
Write-Host "LocalWriter - Development Stack Check" -ForegroundColor White
Write-Host "======================================"
Write-Host ""

# -- OS -----------------------------------------------------------------------

$osInfo = "Windows $([Environment]::OSVersion.Version)"
Write-Ok "OS: $osInfo"

# -- Python -------------------------------------------------------------------

$python = $null
foreach ($cmd in @("python", "python3", "py")) {
    try {
        $ver = & $cmd --version 2>&1 | Select-Object -First 1
        $python = $cmd
        $pyPath = (Get-Command $cmd).Source
        Write-Ok "$ver ($pyPath)"

        # Check venv
        if ($env:VIRTUAL_ENV) {
            Write-Warn "Python is inside a venv ($env:VIRTUAL_ENV) - unopkg may fail with std::bad_alloc"
        }
        break
    } catch { }
}

if (-not $python) {
    Write-Fail "Python 3.8+ not found"
}

# -- pip or uv ----------------------------------------------------------------

$hasUv = $false
try {
    $uvVer = & uv --version 2>&1 | Select-Object -First 1
    Write-Ok "uv: $uvVer"
    $hasUv = $true
} catch { }

$hasPip = $false
if ($python) {
    try {
        $pipVer = & $python -m pip --version 2>&1 | Select-Object -First 1
        Write-Ok "pip: $pipVer"
        $hasPip = $true
    } catch { }
}

if (-not $hasPip -and -not $hasUv) {
    Write-Fail "Neither pip nor uv found - cannot install dependencies"
}

# -- PyYAML -------------------------------------------------------------------

if ($python) {
    try {
        $yamlVer = & $python -c "import yaml; print(yaml.__version__)" 2>&1
        if ($LASTEXITCODE -eq 0) {
            Write-Ok "PyYAML: $yamlVer"
        } else {
            Write-Fail "PyYAML not installed - run: .\install.ps1"
        }
    } catch {
        Write-Fail "PyYAML not installed - run: .\install.ps1"
    }
}

# -- LibreOffice --------------------------------------------------------------

$lo = $null
$loSearchPaths = @(
    "$env:ProgramFiles\LibreOffice\program\soffice.exe",
    "${env:ProgramFiles(x86)}\LibreOffice\program\soffice.exe"
)

foreach ($path in $loSearchPaths) {
    if (Test-Path $path) {
        $lo = $path
        break
    }
}

if (-not $lo) {
    $loCmd = Get-Command soffice -ErrorAction SilentlyContinue
    if ($loCmd) { $lo = $loCmd.Source }
}

if ($lo) {
    try {
        $loVer = & $lo --version 2>&1 | Select-Object -First 1
        Write-Ok "LibreOffice: $loVer"
    } catch {
        Write-Ok "LibreOffice: $lo"
    }
} else {
    Write-Fail "LibreOffice (soffice) not found"
}

# -- unopkg -------------------------------------------------------------------

$unopkg = Get-Command unopkg -ErrorAction SilentlyContinue
if (-not $unopkg) {
    # Try LO program dir
    foreach ($base in @("$env:ProgramFiles\LibreOffice", "${env:ProgramFiles(x86)}\LibreOffice")) {
        $candidate = Join-Path $base "program\unopkg.exe"
        if (Test-Path $candidate) {
            $unopkg = $candidate
            break
        }
    }
}

if ($unopkg) {
    $unopkgPath = if ($unopkg -is [System.Management.Automation.CommandInfo]) { $unopkg.Source } else { $unopkg }
    Write-Ok "unopkg: $unopkgPath"
} else {
    Write-Fail "unopkg not found - check LibreOffice installation"
}

# -- bash (Git for Windows) ---------------------------------------------------

$bash = Get-Command bash -ErrorAction SilentlyContinue
if ($bash) {
    Write-Ok "bash: $($bash.Source) (needed by Makefile)"
} else {
    Write-Fail "bash not found - install Git for Windows: winget install Git.Git"
}

# -- make ---------------------------------------------------------------------

$makeCmd = Get-Command make -ErrorAction SilentlyContinue
if ($makeCmd) {
    try {
        $makeVer = & make --version 2>&1 | Select-Object -First 1
        Write-Ok "make: $makeVer"
    } catch {
        Write-Ok "make: $($makeCmd.Source)"
    }
} else {
    Write-Fail "make not found - install: winget install GnuWin32.Make"
}

# -- git ----------------------------------------------------------------------

$gitCmd = Get-Command git -ErrorAction SilentlyContinue
if ($gitCmd) {
    $gitVer = & git --version 2>&1
    Write-Ok "git: $gitVer"
} else {
    Write-Fail "git not found"
}

# -- openssl (optional) -------------------------------------------------------

$sslCmd = Get-Command openssl -ErrorAction SilentlyContinue
if ($sslCmd) {
    $sslVer = & openssl version 2>&1
    Write-Ok "openssl: $sslVer (optional, for MCP HTTPS)"
} else {
    Write-Warn "openssl not found (optional, for MCP HTTPS)"
}

# -- Project files ------------------------------------------------------------

Write-Host ""
Write-Host "Project" -ForegroundColor White
Write-Host "-------"

if ($python -and (Test-Path "$ProjectRoot\plugin\version.py")) {
    try {
        $extVer = & $python -c "import sys; sys.path.insert(0, r'$ProjectRoot'); from plugin.version import EXTENSION_VERSION; print(EXTENSION_VERSION)" 2>&1
        if ($LASTEXITCODE -eq 0) {
            Write-Ok "Extension version: $extVer"
        } else {
            Write-Warn "Could not read extension version"
        }
    } catch {
        Write-Warn "Could not read extension version"
    }
} else {
    Write-Warn "plugin/version.py not found"
}

if (Test-Path "$ProjectRoot\vendor") {
    $vendorFiles = Get-ChildItem "$ProjectRoot\vendor" -ErrorAction SilentlyContinue
    if ($vendorFiles) {
        Write-Ok "vendor/ populated"
    } else {
        Write-Warn "vendor/ empty - run: make vendor"
    }
} else {
    Write-Warn "vendor/ missing - run: make vendor"
}

$oxtPath = Join-Path $ProjectRoot "build\localwriter.oxt"
if (Test-Path $oxtPath) {
    $oxtSize = (Get-Item $oxtPath).Length
    Write-Ok "build/localwriter.oxt exists ($oxtSize bytes)"
} else {
    Write-Warn "No .oxt built yet - run: make build"
}

# -- Extension installed? -----------------------------------------------------

if ($unopkg) {
    $unopkgExe = if ($unopkg -is [System.Management.Automation.CommandInfo]) { $unopkg.Source } else { $unopkg }
    $extList = & $unopkgExe list 2>&1 | Out-String
    if ($extList -match "org.extension.writeragent") {
        Write-Ok "Extension registered in LibreOffice"
    } else {
        Write-Warn "Extension not registered - run: make deploy"
    }
}

# -- Summary ------------------------------------------------------------------

Write-Host ""
Write-Host "======================================"
if ($script:Errors -gt 0) {
    Write-Host "$($script:Errors) error(s), $($script:Warnings) warning(s)" -ForegroundColor Red
    Write-Host ""
    Write-Host "Fix the errors above before building. See DEVEL.md for instructions."
} elseif ($script:Warnings -gt 0) {
    Write-Host "All required tools found, $($script:Warnings) warning(s)" -ForegroundColor Green
} else {
    Write-Host "Everything looks good!" -ForegroundColor Green
}

Write-Host ""
Write-Host "--- Copy-paste brief ---" -ForegroundColor White
Write-Host ""
foreach ($line in $script:Brief) {
    Write-Host $line
}
Write-Host "OS:   $osInfo"
Write-Host "Errors: $($script:Errors) / Warnings: $($script:Warnings)"

exit $script:Errors
