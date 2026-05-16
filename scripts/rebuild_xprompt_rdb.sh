#!/usr/bin/env bash
# Rebuild extension/XPromptFunction.rdb from extension/idl/XPromptFunction.idl
# Requires LibreOffice SDK (libreoffice-fresh-sdk): unoidl-write
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
IDL="$ROOT/extension/idl/XPromptFunction.idl"
RDB="$ROOT/extension/XPromptFunction.rdb"
SDK_HOME="${OO_SDK_HOME:-/usr/lib/libreoffice/sdk}"
UNOIDLWRITE="${SDK_HOME}/bin/unoidl-write"

if [[ ! -x "$UNOIDLWRITE" ]]; then
  echo "error: unoidl-write not found at $UNOIDLWRITE (install libreoffice-fresh-sdk)." >&2
  exit 1
fi

# Paths from sdk/settings/std.mk (Linux)
URE_TYPES="/usr/lib/libreoffice/program/types.rdb"
OFFICE_TYPES="/usr/lib/libreoffice/program/types/offapi.rdb"
for f in "$URE_TYPES" "$OFFICE_TYPES"; do
  if [[ ! -f "$f" ]]; then
    echo "error: missing type library $f" >&2
    exit 1
  fi
done

rm -f "$RDB"
"$UNOIDLWRITE" "$URE_TYPES" "$OFFICE_TYPES" "$IDL" "$RDB"
echo "Wrote $RDB ($(wc -c <"$RDB") bytes)"
