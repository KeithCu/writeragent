#!/bin/bash
# Launch LibreOffice with debug logging.
#
# Adapted from mcp-libre/scripts/launch-lo-debug.sh.
#
# Usage:
#   ./scripts/launch-lo-debug.sh           # WARN+ERROR only
#   ./scripts/launch-lo-debug.sh --full    # +INFO (slow startup)
#   ./scripts/launch-lo-debug.sh --restore # Enable document recovery

FULL=false
NORESTORE=true

for arg in "$@"; do
    case "$arg" in
        --full)    FULL=true ;;
        --restore) NORESTORE=false ;;
        -h|--help)
            echo "Usage: $0 [--full] [--restore]"
            echo "  --full    : verbose SAL_LOG (+INFO, slow startup)"
            echo "  --restore : enable document recovery on startup"
            exit 0
            ;;
    esac
done

LOG_FILE="$HOME/soffice-debug.log"
PLUGIN_LOG="$HOME/writeragent.log"

if $FULL; then
    export SAL_LOG="+INFO+WARN+ERROR"
    echo "[!] Full SAL_LOG - expect slow startup"
else
    export SAL_LOG="+WARN+ERROR"
fi

echo "SAL_LOG    = $SAL_LOG"
echo "LO stderr  -> $LOG_FILE"
echo "Plugin log -> $PLUGIN_LOG"

# Kill existing instances
pkill -f soffice 2>/dev/null
sleep 2

LO_ARGS=""
if $NORESTORE; then
    LO_ARGS="--norestore"
    echo "Recovery disabled (--norestore, use --restore to enable)"
fi

# Find LibreOffice binary
SOFFICE=""
for candidate in \
    /usr/bin/soffice \
    /usr/lib/libreoffice/program/soffice \
    /opt/libreoffice*/program/soffice \
    /snap/bin/libreoffice.soffice \
    /usr/local/bin/soffice; do
    if [ -x "$candidate" ]; then
        SOFFICE="$candidate"
        break
    fi
done

if [ -z "$SOFFICE" ]; then
    SOFFICE=$(command -v soffice 2>/dev/null || true)
fi

if [ -z "$SOFFICE" ]; then
    echo "[X] soffice not found. Install LibreOffice first."
    exit 1
fi

if [ -n "$WRITERAGENT_SET_CONFIG" ]; then
    echo "Config overrides: $WRITERAGENT_SET_CONFIG"
fi

echo "Launching LibreOffice ($SOFFICE)..."
WRITERAGENT_SET_CONFIG="${WRITERAGENT_SET_CONFIG:-}" $SOFFICE $LO_ARGS --writer 2>"$LOG_FILE" &
echo "LibreOffice launched. Tail log: tail -f $LOG_FILE"
