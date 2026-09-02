#!/usr/bin/env bash
# Rebuild Calc add-in typelibraries from IDL (one .rdb per interface; unoidl-write
# only retains the last IDL when several are passed to a single output file).
# Requires LibreOffice SDK (libreoffice-fresh-sdk): unoidl-write.
# When unoidl-write is missing, reuse the committed .rdb files.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
# shellcheck source=find_unoidl.sh
source "$(cd "$(dirname "$0")" && pwd)/find_unoidl.sh"
IDL_PYTHON="$ROOT/extension/idl/XPythonFunction.idl"
IDL_PROMPT="$ROOT/extension/idl/XPromptFunction.idl"
RDB_PYTHON="$ROOT/extension/XPythonFunction.rdb"
RDB_PROMPT="$ROOT/extension/XPromptFunction.rdb"

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
