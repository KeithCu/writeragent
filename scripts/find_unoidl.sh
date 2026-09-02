#!/usr/bin/env bash
# Shared LibreOffice SDK finder for unoidl-write and types.rdb (source, do not execute).
#
# Usage:
#   source "$(dirname "${BASH_SOURCE[0]}")/find_unoidl.sh"
#
# Search order for unoidl-write: PATH, $OO_SDK_HOME/bin, then OS-typical SDK
# trees. Linux CI still hits /usr/lib/libreoffice/sdk when that binary exists.

[[ -n "${_FIND_UNOIDL_LOADED:-}" ]] && return 0
_FIND_UNOIDL_LOADED=1

_find_unoidl_write() {
  local name cand pf
  for name in unoidl-write unoidl-write.exe; do
    if command -v "$name" >/dev/null 2>&1; then
      command -v "$name"
      return 0
    fi
  done
  if [[ -n "${OO_SDK_HOME:-}" ]]; then
    for cand in "$OO_SDK_HOME/bin/unoidl-write" "$OO_SDK_HOME/bin/unoidl-write.exe"; do
      if [[ -x "$cand" ]]; then
        printf '%s\n' "$cand"
        return 0
      fi
    done
  fi
  for cand in \
    /usr/lib/libreoffice/sdk/bin/unoidl-write \
    /Applications/LibreOffice.app/Contents/sdk/bin/unoidl-write
  do
    if [[ -x "$cand" ]]; then
      printf '%s\n' "$cand"
      return 0
    fi
  done
  for pf in \
    "${PROGRAMFILES:-}" \
    "$(printenv 'PROGRAMFILES(X86)' 2>/dev/null || true)" \
    "/c/Program Files" \
    "/c/Program Files (x86)"
  do
    [[ -n "$pf" ]] || continue
    for cand in \
      "$pf/LibreOffice/sdk/bin/unoidl-write.exe" \
      "$pf/LibreOffice/sdk/bin/unoidl-write"
    do
      if [[ -x "$cand" ]]; then
        printf '%s\n' "$cand"
        return 0
      fi
    done
  done
  return 1
}

_resolve_existing() {
  local p="$1"
  if command -v realpath >/dev/null 2>&1; then
    realpath "$p" 2>/dev/null && return 0
  fi
  readlink -f "$p" 2>/dev/null && return 0
  printf '%s\n' "$p"
}

# types.rdb / types/offapi.rdb sit next to the install that owns unoidl-write
# (…/program on Linux/Windows, …/Resources on macOS.app), not Linux-only paths.
_find_type_rdbs() {
  local resolved dir ure office i
  resolved="$(_resolve_existing "$1")"
  dir="$(cd "$(dirname "$resolved")" && pwd)"
  for i in 1 2 3 4 5 6; do
    for ure in "$dir/program/types.rdb" "$dir/Resources/types.rdb"; do
      office="$(dirname "$ure")/types/offapi.rdb"
      if [[ -f "$ure" && -f "$office" ]]; then
        printf '%s\n%s\n' "$ure" "$office"
        return 0
      fi
    done
    dir="$(cd "$dir/.." && pwd)" || break
  done
  return 1
}
