# Dev-mode deploy: hot-sync project files into the unopkg cache.
#
# ``make deploy`` runs ``make build`` first, then this script. Registration via
# unopkg happens only when the extension is not yet registered (or cache is
# missing); subsequent deploys sync source files into the cache only.
#
# Usage:
#   .\scripts\dev-deploy.ps1           # Regenerate + deploy to cache
#   .\scripts\dev-deploy.ps1 -NoGen    # Deploy only (skip generate_manifest)
#   .\scripts\dev-deploy.ps1 -Remove   # Remove legacy share\extensions symlink (migration)

param(
    [switch]$Remove,
    [switch]$NoGen,
    [switch]$Help
)

if ($Help) {
    Write-Host "Usage: .\scripts\dev-deploy.ps1 [-NoGen] [-Remove]"
    Write-Host "  -NoGen  : skip generate_manifest.py"
    Write-Host "  -Remove : remove legacy share\extensions\writeragent junction if present"
    exit 0
}

$ErrorActionPreference = "Stop"

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$ProjectRoot = Split-Path -Parent $ScriptDir
$ExtName = "writeragent"

function Remove-LegacySymlink {
    $loExtDir = $null
    $candidates = @(
        "${env:ProgramFiles}\LibreOffice\share\extensions",
        "${env:ProgramFiles(x86)}\LibreOffice\share\extensions",
        "C:\Program Files\LibreOffice\share\extensions",
        "C:\Program Files (x86)\LibreOffice\share\extensions"
    )
    foreach ($candidate in $candidates) {
        if (Test-Path $candidate) {
            $loExtDir = $candidate
            break
        }
    }
    if (-not $loExtDir) {
        Write-Host "[OK] LibreOffice share\extensions not found; nothing to remove"
        return
    }

    $symlinkPath = Join-Path $loExtDir $ExtName
    if (-not (Test-Path $symlinkPath)) {
        Write-Host "[OK] No legacy symlink to remove"
        return
    }

    cmd /c rmdir "$symlinkPath" 2>$null
    if (-not (Test-Path $symlinkPath)) {
        Write-Host "[OK] Legacy symlink removed: $symlinkPath"
    } else {
        Remove-Item -Path $symlinkPath -Force -Recurse
        Write-Host "[OK] Legacy symlink removed: $symlinkPath"
    }
}

if ($Remove) {
    Remove-LegacySymlink
    exit 0
}

Write-Host ""
Write-Host "=== Dev Deploy ==="
Write-Host ""

if (-not $NoGen) {
    Write-Host "[*] Regenerating manifests..."
    $generateScript = Join-Path $ScriptDir "generate_manifest.py"
    & python $generateScript
    Write-Host ""
}

$installScript = Join-Path $ScriptDir "install-plugin.ps1"
& $installScript -Cache
exit $LASTEXITCODE
