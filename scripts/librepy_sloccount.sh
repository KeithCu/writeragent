#!/usr/bin/env bash
# librepy_sloccount.sh — sloccount report for LibrePy.oxt first-party Python.
#
# Reports both totals:
#   - Total plugin/ Python SLOC (includes vendored lib + contrib)
#   - First-party SLOC (excludes plugin/lib and plugin/contrib)
#
# External trees:
#   plugin/lib/     — vendored json_repair, latex2mathml
#   plugin/contrib/ — slim smolagents subset for the venv AST sandbox
#
# Usage:
#   bash scripts/librepy_sloccount.sh
#   bash scripts/librepy_sloccount.sh --build
#   bash scripts/librepy_sloccount.sh --extract
#   bash scripts/librepy_sloccount.sh --details
#
# By default uses build/bundle-librepy/ (same tree as build/LibrePy.oxt).
# --extract unpacks the .oxt to /tmp/librepy-oxt-sloc instead.

set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
BUNDLE_DIR="$ROOT/build/bundle-librepy"
OXT="$ROOT/build/LibrePy.oxt"
EXTRACT_DIR="${TMPDIR:-/tmp}/librepy-oxt-sloc"

DO_BUILD=0
DO_EXTRACT=0
SHOW_DETAILS=0

usage() {
    sed -n '2,18p' "$0" | sed 's/^# \?//'
    exit "${1:-0}"
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --build) DO_BUILD=1 ;;
        --extract) DO_EXTRACT=1 ;;
        --details) SHOW_DETAILS=1 ;;
        -h|--help) usage 0 ;;
        *)
            echo "error: unknown option: $1" >&2
            usage 1
            ;;
    esac
    shift
done

if ! command -v sloccount >/dev/null 2>&1; then
    echo "error: sloccount not found (install sloccount package)." >&2
    exit 1
fi

if [[ "$DO_BUILD" -eq 1 ]]; then
    echo "Building LibrePy.oxt (make build-core)..."
    make -C "$ROOT" build-core
fi

WORK_DIR=""
CLEANUP=0

if [[ "$DO_EXTRACT" -eq 1 ]]; then
    if [[ ! -f "$OXT" ]]; then
        echo "error: $OXT not found. Run: make build-core  (or pass --build)" >&2
        exit 1
    fi
    rm -rf "$EXTRACT_DIR"
    mkdir -p "$EXTRACT_DIR"
    unzip -q "$OXT" -d "$EXTRACT_DIR"
    WORK_DIR="$EXTRACT_DIR"
    CLEANUP=1
elif [[ -d "$BUNDLE_DIR/plugin" ]]; then
    WORK_DIR="$BUNDLE_DIR"
else
    echo "error: $BUNDLE_DIR not found. Run: make build-core  (or pass --build)" >&2
    exit 1
fi

cleanup() {
    if [[ "$CLEANUP" -eq 1 && -d "$EXTRACT_DIR" ]]; then
        rm -rf "$EXTRACT_DIR"
    fi
}
trap cleanup EXIT

FIRST_PARTY=(
    plugin/calc
    plugin/chatbot
    plugin/doc
    plugin/draw
    plugin/framework
    plugin/librepy
    plugin/scripting
    plugin/vision
    plugin/writer
    plugin/__init__.py
    plugin/main_core.py
    plugin/_manifest.py
    plugin/version.py
)

# Run sloccount and print total SLOC (last field on the Total Physical line).
sloc_total() {
    local dir="$1"
    shift
    (
        cd "$dir"
        sloccount --duplicates --wide "$@" 2>/dev/null \
            | awk '/^Total Physical Source Lines of Code/{gsub(/,/,"",$NF); print $NF; exit}'
    )
}

# Print per-directory rows from sloccount --wide (SLOC, directory name).
sloc_dirs() {
    local dir="$1"
    shift
    (
        cd "$dir"
        sloccount --duplicates --wide "$@" 2>/dev/null \
            | awk '/^[0-9]+[[:space:]]+/{gsub(/,/,"",$1); printf "%s\t%s\n", $1, $2}'
    )
}

sloc_report() {
    local title="$1"
    shift
    echo ""
    echo "=== $title ==="
    (
        cd "$WORK_DIR"
        if [[ "$SHOW_DETAILS" -eq 1 ]]; then
            sloccount --duplicates --wide --details "$@"
        else
            sloccount --duplicates --wide "$@"
        fi
    )
}

pct_of() {
    local part="$1"
    local whole="$2"
    if [[ "$whole" -eq 0 ]]; then
        echo "0.0"
    else
        awk -v p="$part" -v w="$whole" 'BEGIN { printf "%.1f", (p / w) * 100 }'
    fi
}

echo "LibrePy sloccount"
echo "================="
echo "Source: $WORK_DIR"
if [[ -f "$OXT" ]]; then
    echo "OXT:    $OXT ($(wc -c <"$OXT" | tr -d ' ') bytes)"
fi
if command -v git >/dev/null 2>&1 && git -C "$ROOT" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
    echo "Git:    $(git -C "$ROOT" rev-parse --short HEAD 2>/dev/null || echo unknown)"
fi

FIRST_TOTAL="$(sloc_total "$WORK_DIR" "${FIRST_PARTY[@]}")"
LIB_TOTAL="$(sloc_total "$WORK_DIR" plugin/lib)"
CONTRIB_TOTAL="$(sloc_total "$WORK_DIR" plugin/contrib)"
FULL_TOTAL="$(sloc_total "$WORK_DIR" plugin)"
SUM_PARTS=$((FIRST_TOTAL + LIB_TOTAL + CONTRIB_TOTAL))

echo ""
echo "=== SLOC report (Python, sloccount) ==="
printf "%-42s %8s %8s\n" "Category" "SLOC" "% total"
printf "%-42s %8s %8s\n" "--------" "----" "-------"
printf "%-42s %8s %7s%%\n" "First-party (excl. lib + contrib)" "$FIRST_TOTAL" "$(pct_of "$FIRST_TOTAL" "$FULL_TOTAL")"
printf "%-42s %8s %7s%%\n" "  plugin/lib (vendored packages)" "$LIB_TOTAL" "$(pct_of "$LIB_TOTAL" "$FULL_TOTAL")"
printf "%-42s %8s %7s%%\n" "  plugin/contrib (smolagents)" "$CONTRIB_TOTAL" "$(pct_of "$CONTRIB_TOTAL" "$FULL_TOTAL")"
printf "%-42s %8s %8s\n" "--------" "----" "-------"
printf "%-42s %8s %7s%%\n" "Total plugin/ (all Python)" "$FULL_TOTAL" "100.0"

if [[ "$SUM_PARTS" -ne "$FULL_TOTAL" ]]; then
    echo ""
    echo "Note: first-party + lib + contrib = $SUM_PARTS (sloccount total = $FULL_TOTAL;"
    echo "      small mismatch can happen when sloccount splits top-level files differently)."
fi

echo ""
echo "=== First-party breakdown ==="
printf "%-24s %8s\n" "Directory" "SLOC"
printf "%-24s %8s\n" "---------" "----"
while IFS=$'\t' read -r sloc name; do
    [[ -n "$sloc" ]] || continue
    printf "%-24s %8s\n" "$name" "$sloc"
done < <(sloc_dirs "$WORK_DIR" "${FIRST_PARTY[@]}")
printf "%-24s %8s\n" "---------" "----"
printf "%-24s %8s\n" "Subtotal (first-party)" "$FIRST_TOTAL"

if [[ "$SHOW_DETAILS" -eq 1 ]]; then
    sloc_report "First-party detail" "${FIRST_PARTY[@]}"
    sloc_report "External: plugin/lib" plugin/lib
    sloc_report "External: plugin/contrib" plugin/contrib
    sloc_report "Full plugin/ tree" plugin
fi

echo ""
echo "Quick totals: first-party=$FIRST_TOTAL  total=$FULL_TOTAL  (external=$((FULL_TOTAL - FIRST_TOTAL)))"
