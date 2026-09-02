#!/usr/bin/env bash
# Rebuild Calc add-in typelibraries from IDL (one .rdb per interface; unoidl-write
# only retains the last IDL when several are passed to a single output file).
# Requires LibreOffice SDK (libreoffice-fresh-sdk): unoidl-write.
# When unoidl-write is missing, reuse the committed .rdb files.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
IDL_PYTHON="$ROOT/extension/idl/XPythonFunction.idl"
IDL_PROMPT="$ROOT/extension/idl/XPromptFunction.idl"
RDB_PYTHON="$ROOT/extension/XPythonFunction.rdb"
RDB_PROMPT="$ROOT/extension/XPromptFunction.rdb"

# PATH, $OO_SDK_HOME/bin, then OS-typical SDK trees. Linux CI still hits
# /usr/lib/libreoffice/sdk when that binary exists.
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

UNOIDLWRITE="$(_find_unoidl_write || true)"
if [[ -z "$UNOIDLWRITE" ]]; then
  if [[ -f "$RDB_PYTHON" && -f "$RDB_PROMPT" ]]; then
    echo "skip: unoidl-write not found; using committed $RDB_PYTHON and $RDB_PROMPT" >&2
    exit 0
  fi
  echo "error: unoidl-write not found (install libreoffice-fresh-sdk)." >&2
  exit 1
fi

_type_pair="$(_find_type_rdbs "$UNOIDLWRITE" || true)"
if [[ -z "$_type_pair" ]]; then
  echo "error: missing type library types.rdb / types/offapi.rdb next to $UNOIDLWRITE" >&2
  exit 1
fi
URE_TYPES="${_type_pair%%$'\n'*}"
OFFICE_TYPES="${_type_pair#*$'\n'}"

rm -f "$RDB_PYTHON" "$RDB_PROMPT"
"$UNOIDLWRITE" "$URE_TYPES" "$OFFICE_TYPES" "$IDL_PYTHON" "$RDB_PYTHON"
"$UNOIDLWRITE" "$URE_TYPES" "$OFFICE_TYPES" "$IDL_PROMPT" "$RDB_PROMPT"
echo "Wrote $RDB_PYTHON ($(wc -c <"$RDB_PYTHON") bytes) from XPythonFunction.idl"
echo "Wrote $RDB_PROMPT ($(wc -c <"$RDB_PROMPT") bytes) from XPromptFunction.idl"
