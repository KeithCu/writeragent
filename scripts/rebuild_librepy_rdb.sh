#!/usr/bin/env bash
# Rebuild LibrePy Calc add-in typelibrary from extension-core IDL.
# IDL uses org.extension.writeragent.PythonFunction (shared namespace with WriterAgent
# for formula portability); extension id remains org.extension.librepy.
# Requires LibreOffice SDK: unoidl-write (Ubuntu/Debian: libreoffice-dev;
# Arch: libreoffice-fresh-sdk). macOS/Windows CI install LibreOffice without the
# SDK; if unoidl-write is missing, reuse the committed XPythonFunction.rdb.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
# shellcheck source=find_unoidl.sh
source "$(cd "$(dirname "$0")" && pwd)/find_unoidl.sh"
IDL_PYTHON="$ROOT/extension-core/idl/XPythonFunction.idl"
RDB_PYTHON="$ROOT/extension-core/XPythonFunction.rdb"

UNOIDLWRITE="$(_find_unoidl_write || true)"
if [[ -z "$UNOIDLWRITE" ]]; then
  if [[ -f "$RDB_PYTHON" ]]; then
    echo "skip: unoidl-write not found; using committed $RDB_PYTHON" >&2
    exit 0
  fi
  echo "error: unoidl-write not found (install libreoffice-dev on Ubuntu/Debian, or libreoffice-fresh-sdk on Arch)." >&2
  exit 1
fi

_type_pair="$(_find_type_rdbs "$UNOIDLWRITE" || true)"
if [[ -z "$_type_pair" ]]; then
  echo "error: missing type library types.rdb / types/offapi.rdb next to $UNOIDLWRITE" >&2
  exit 1
fi
URE_TYPES="${_type_pair%%$'\n'*}"
OFFICE_TYPES="${_type_pair#*$'\n'}"

rm -f "$RDB_PYTHON"
"$UNOIDLWRITE" "$URE_TYPES" "$OFFICE_TYPES" "$IDL_PYTHON" "$RDB_PYTHON"
echo "Wrote $RDB_PYTHON ($(wc -c <"$RDB_PYTHON") bytes) from XPythonFunction.idl"
