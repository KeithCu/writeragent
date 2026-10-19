#!/usr/bin/env bash
# Windows CI: install what choco could not (community.chocolatey.org 503/504 outages).
# Tip: choco reports a failed fetch but the step carried on with no LibreOffice.
set -euo pipefail

tmp="${RUNNER_TEMP:-/tmp}/wa-tools"
mkdir -p "$tmp"

if [ ! -x "/c/Program Files/LibreOffice/program/soffice.exe" ]; then
  base=https://download.documentfoundation.org/libreoffice/stable
  # Oldest listed stable release, the same branch choco's libreoffice-fresh tracked.
  ver=$(curl -fsSL "$base/" | grep -oE 'href="[0-9]+\.[0-9]+\.[0-9]+/"' | grep -oE '[0-9.]+[0-9]' | sort -V | head -1)
  msi="$tmp/LibreOffice_${ver}_Win_x86-64.msi"
  echo "choco fallback: LibreOffice $ver"
  curl -fsSL --retry 3 -o "$msi" "$base/$ver/win/x86_64/LibreOffice_${ver}_Win_x86-64.msi"
  powershell -NoProfile -Command "Start-Process msiexec -ArgumentList '/i','$(cygpath -w "$msi")','/qn','/norestart' -Wait"
  test -x "/c/Program Files/LibreOffice/program/soffice.exe"
fi

if ! command -v make >/dev/null 2>&1; then
  # The runner image ships GNU make as mingw32-make.
  mm=$(command -v mingw32-make || ls /c/mingw64/bin/mingw32-make.exe /c/ProgramData/mingw64/mingw64/bin/mingw32-make.exe 2>/dev/null | head -1)
  echo "choco fallback: make from $mm"
  mkdir -p "$HOME/.local/bin"
  cp "$mm" "$HOME/.local/bin/make.exe"
fi

if [ ! -x "/c/Program Files/gettext-iconv/bin/msgfmt.exe" ]; then
  url=https://github.com/mlocati/gettext-iconv-windows/releases/download/v1.0-v1.19/gettext1.0-iconv1.19-static-64.zip
  echo "choco fallback: gettext from $url"
  curl -fsSL --retry 3 -o "$tmp/gettext.zip" "$url"
  powershell -NoProfile -Command "Expand-Archive -Force '$(cygpath -w "$tmp/gettext.zip")' 'C:\Program Files\gettext-iconv'"
  test -x "/c/Program Files/gettext-iconv/bin/msgfmt.exe"
fi
