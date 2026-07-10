#!/usr/bin/env bash
# Rebuild LibrePy Calc add-in typelibrary from extension-core IDL.
# Requires LibreOffice SDK (libreoffice-fresh-sdk): unoidl-write
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
IDL_PYTHON="$ROOT/extension-core/idl/XPythonFunction.idl"
RDB_PYTHON="$ROOT/extension-core/XPythonFunction.rdb"
SDK_HOME="${OO_SDK_HOME:-/usr/lib/libreoffice/sdk}"
UNOIDLWRITE="${SDK_HOME}/bin/unoidl-write"

if [[ ! -x "$UNOIDLWRITE" ]]; then
  echo "error: unoidl-write not found at $UNOIDLWRITE (install libreoffice-fresh-sdk)." >&2
  exit 1
fi

URE_TYPES="/usr/lib/libreoffice/program/types.rdb"
OFFICE_TYPES="/usr/lib/libreoffice/program/types/offapi.rdb"
for f in "$URE_TYPES" "$OFFICE_TYPES"; do
  if [[ ! -f "$f" ]]; then
    echo "error: missing type library $f" >&2
    exit 1
  fi
done

rm -f "$RDB_PYTHON"
"$UNOIDLWRITE" "$URE_TYPES" "$OFFICE_TYPES" "$IDL_PYTHON" "$RDB_PYTHON"
echo "Wrote $RDB_PYTHON ($(wc -c <"$RDB_PYTHON") bytes) from XPythonFunction.idl"
