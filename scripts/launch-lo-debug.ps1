# Launch LibreOffice with debug logging.
#
# Adapted from mcp-libre/scripts/launch-lo-debug.sh.
#
# Usage:
#   .\scripts\launch-lo-debug.ps1             # WARN+ERROR only
#   .\scripts\launch-lo-debug.ps1 -Full       # +INFO (slow startup)
#   .\scripts\launch-lo-debug.ps1 -Restore    # Enable document recovery

param(
    [switch]$Full,
    [switch]$Restore,
    [switch]$Writer,
    [switch]$Calc,
    [switch]$Draw,
    [switch]$Impress,
    [switch]$Help
)

if ($Help) {
    Write-Host "Usage: .\scripts\launch-lo-debug.ps1 [-Full] [-Restore] [-Writer|-Calc|-Draw|-Impress]"
    Write-Host "  -Full    : verbose SAL_LOG (+INFO, slow startup)"
    Write-Host "  -Restore : enable document recovery on startup"
    Write-Host "  -Writer  : start LibreOffice Writer (default)"
    Write-Host "  -Calc    : start LibreOffice Calc"
    Write-Host "  -Draw    : start LibreOffice Draw"
    Write-Host "  -Impress : start LibreOffice Impress"
    exit 0
}

$LogFile = Join-Path $env:USERPROFILE "soffice-debug.log"
$PluginLog = Join-Path $env:APPDATA "LibreOffice\4\user\writeragent_debug.log"

if ($Full) {
    $env:SAL_LOG = "+INFO+WARN+ERROR"
    Write-Host "[!] Full SAL_LOG - expect slow startup"
} else {
    $env:SAL_LOG = "+WARN+ERROR"
}

Write-Host "SAL_LOG    = $env:SAL_LOG"
Write-Host "LO stderr  -> $LogFile"
Write-Host "Plugin log -> $PluginLog"

# Kill existing instances
Get-Process -Name "soffice", "soffice.bin" -ErrorAction SilentlyContinue |
    Stop-Process -Force -ErrorAction SilentlyContinue
Start-Sleep -Seconds 2

$loArgs = @()
if (-not $Restore) {
    $loArgs += "--norestore"
    Write-Host "Recovery disabled (--norestore, use -Restore to enable)"
}

if ($Writer) { $loArgs += "--writer" }
elseif ($Calc) { $loArgs += "--calc" }
elseif ($Draw) { $loArgs += "--draw" }
elseif ($Impress) { $loArgs += "--impress" }
else {
    $loArgs += "--writer"
}

# ── Find LibreOffice binary ────────────────────────────────────────────────

$soffice = $null
$candidates = @(
    "${env:ProgramFiles}\LibreOffice\program\soffice.exe",
    "${env:ProgramFiles(x86)}\LibreOffice\program\soffice.exe",
    "C:\Program Files\LibreOffice\program\soffice.exe",
    "C:\Program Files (x86)\LibreOffice\program\soffice.exe"
)

foreach ($candidate in $candidates) {
    if (Test-Path $candidate) {
        $soffice = $candidate
        break
    }
}

if (-not $soffice) {
    $soffice = (Get-Command soffice -ErrorAction SilentlyContinue).Source
}

if (-not $soffice) {
    Write-Host "[X] soffice not found. Install LibreOffice first."
    exit 1
}

Write-Host "Launching LibreOffice ($soffice)..."
Start-Process -FilePath $soffice -ArgumentList $loArgs `
    -RedirectStandardError $LogFile -WindowStyle Normal
Write-Host "LibreOffice launched. Log: $LogFile"
