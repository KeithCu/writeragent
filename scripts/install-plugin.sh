#!/bin/bash
# Build and install the WriterAgent extension (.oxt).
#
# Adapted from mcp-libre/scripts/install-plugin.sh.
#
# Usage:
#   ./scripts/install-plugin.sh                # Build + install (interactive)
#   ./scripts/install-plugin.sh --force        # Build + install (no prompts, kills LO)
#   ./scripts/install-plugin.sh --build-only   # Only create the .oxt
#   ./scripts/install-plugin.sh --uninstall    # Remove the extension
#   ./scripts/install-plugin.sh --cache        # Hot-deploy to LO cache (dev iteration)
#   ./scripts/install-plugin.sh --modules "core mcp"  # Build specific modules

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"
# shellcheck source=lo_paths.sh
source "$SCRIPT_DIR/lo_paths.sh"
BUILD_DIR="$PROJECT_ROOT/build"
OXT_FILE="$BUILD_DIR/WriterAgent.oxt"

EXTENSION_ID="org.extension.writeragent"

# Parse args
FORCE=false
BUILD_ONLY=false
UNINSTALL=false
CACHE=false
MODULES=""
while [ $# -gt 0 ]; do
    case "$1" in
        --force)      FORCE=true ;;
        --build-only) BUILD_ONLY=true ;;
        --uninstall)  UNINSTALL=true ;;
        --cache)      CACHE=true ;;
        --modules)    shift; MODULES="$1" ;;
        -h|--help)
            echo "Usage: $0 [--force] [--build-only] [--uninstall] [--cache] [--modules \"core mcp\"]"
            exit 0
            ;;
    esac
    shift
done

# ── Helpers ──────────────────────────────────────────────────────────────────

confirm_or_force() {
    local prompt="$1"
    if $FORCE; then return 0; fi
    read -rp "$prompt (Y/n) " response
    [[ -z "$response" || "$response" =~ ^[Yy] ]]
}

is_lo_running() {
    pgrep -x "soffice.bin" >/dev/null 2>&1
}

stop_libreoffice() {
    echo "[*] Closing LibreOffice..."
    for attempt in 1 2 3; do
        pkill -f soffice 2>/dev/null || true
        sleep 2
        if ! is_lo_running; then
            echo "[OK] LibreOffice closed"
            return 0
        fi
        echo "    Attempt $attempt/3 - processes still running, retrying..."
        sleep 2
    done
    if is_lo_running; then
        echo "[X] Could not close LibreOffice after 3 attempts"
        return 1
    fi
    echo "[OK] LibreOffice closed"
}

ensure_lo_stopped() {
    if ! is_lo_running; then return 0; fi
    echo "[!!] LibreOffice is running. It must be closed for unopkg."
    if ! confirm_or_force "Close LibreOffice now?"; then
        echo "[X] Cannot proceed while LibreOffice is running."
        return 1
    fi
    stop_libreoffice
}

# ── Build .oxt ───────────────────────────────────────────────────────────────

build_oxt() {
    local module_label="auto-discover all"
    if [ -n "$MODULES" ]; then
        module_label="$MODULES"
    fi
    echo ""
    echo "=== Building WriterAgent.oxt (modules: $module_label) ==="
    echo ""

    mkdir -p "$BUILD_DIR"
    rm -f "$OXT_FILE"

    # Generate manifests from module.yaml files
    python3 "$SCRIPT_DIR/generate_manifest.py"

    # Build the .oxt
    if [ -n "$MODULES" ]; then
        python3 "$SCRIPT_DIR/build_oxt.py" \
            --modules $MODULES \
            --output "$OXT_FILE"
    else
        python3 "$SCRIPT_DIR/build_oxt.py" \
            --output "$OXT_FILE"
    fi

    if [ -f "$OXT_FILE" ]; then
        local size
        size=$(stat -c%s "$OXT_FILE" 2>/dev/null || stat -f%z "$OXT_FILE" 2>/dev/null)
        echo "[OK] Built: $OXT_FILE ($size bytes)"
    else
        echo "[X] Failed to create .oxt file"
        return 1
    fi
}

# ── Install / Uninstall ─────────────────────────────────────────────────────

install_extension() {
    local unopkg="$1"

    echo ""
    echo "=== Installing Extension ==="
    echo ""

    ensure_lo_stopped || return 1

    clear_lo_profile_locks

    # Remove previous version
    echo "[*] Removing previous version (if any)..."
    $unopkg remove "$EXTENSION_ID" 2>&1 || true
    sleep 2

    # Install new version
    echo "[*] Installing $OXT_FILE ..."
    if ! $unopkg add "$OXT_FILE" 2>&1; then
        echo "[X] unopkg add failed"
        echo "    Troubleshooting:"
        echo "    1. Make sure LibreOffice is fully closed"
        echo "    2. Try: $0 --uninstall --force"
        echo "    3. Then: $0 --force"
        return 1
    fi

    echo "[OK] Extension installed successfully!"

    sleep 2
    echo "[*] Verifying installation..."
    if $unopkg list 2>&1 | grep -q "$EXTENSION_ID"; then
        echo "[OK] Extension verified: $EXTENSION_ID is registered"
    else
        echo "[!!] Could not verify via unopkg list (often OK, LO will load it on start)"
    fi
}

uninstall_extension() {
    local unopkg="$1"

    echo ""
    echo "=== Uninstalling Extension ==="
    echo ""

    ensure_lo_stopped || return 1

    echo "[*] Removing extension $EXTENSION_ID ..."
    if $unopkg remove "$EXTENSION_ID" 2>&1 | grep -qiE "not deployed|no such|aucune"; then
        echo "    Extension was not installed"
    else
        echo "[OK] Extension removed"
    fi
}

# ── Cache install (hot-deploy) ───────────────────────────────────────────────

extension_registered() {
    local unopkg="$1"
    $unopkg list 2>&1 | grep -q "$EXTENSION_ID"
}

install_to_cache() {
    echo ""
    echo "=== Cache Install (hot-deploy) ==="
    echo ""

    UNOPKG=$(find_unopkg)
    if [ -z "$UNOPKG" ]; then
        echo "[X] unopkg not found. Install LibreOffice first."
        exit 1
    fi

    local cache_dir
    cache_dir=$(find_unopkg_cache_dir)
    local needs_full_install=false
    local ext_dir=""

    if ! extension_registered "$UNOPKG"; then
        needs_full_install=true
    elif [ -z "$cache_dir" ]; then
        needs_full_install=true
    else
        local packages_dir="$cache_dir/cache/uno_packages"
        if [ ! -d "$packages_dir" ]; then
            needs_full_install=true
        else
            for d in "$packages_dir"/*.tmp_; do
                if [ -d "$d/WriterAgent.oxt" ]; then
                    ext_dir="$d/WriterAgent.oxt"
                    break
                fi
            done
            if [ -z "$ext_dir" ]; then
                needs_full_install=true
            fi
        fi
    fi

    if $needs_full_install; then
        if ! extension_registered "$UNOPKG"; then
            echo "[!] Extension not registered. Performing one-time unopkg install..."
        else
            echo "[!] Extension cache not found. Re-registering via unopkg..."
        fi
        FORCE=true
        if [ -f "$OXT_FILE" ]; then
            echo "[OK] Using existing $OXT_FILE (from make build)"
        else
            build_oxt || exit 1
        fi
        install_extension "$UNOPKG" || exit 1

        # Re-resolve cache dir and ext_dir after full install
        cache_dir=$(find_unopkg_cache_dir)
        if [ -n "$cache_dir" ]; then
            local packages_dir="$cache_dir/cache/uno_packages"
            for d in "$packages_dir"/*.tmp_; do
                if [ -d "$d/WriterAgent.oxt" ]; then
                    ext_dir="$d/WriterAgent.oxt"
                    break
                fi
            done
        fi
        if [ -z "$ext_dir" ]; then
            echo "[X] Extension still not found in cache after installation."
            exit 1
        fi
    fi

    echo "[OK] Cache dir: $ext_dir"

    # Sync project files into the cache
    local deployed=0

    # plugin/
    rsync -av --delete \
        --exclude '__pycache__' --exclude '*.pyc' \
        --exclude 'module.yaml' \
        "$PROJECT_ROOT/plugin/" "$ext_dir/plugin/"
    echo "    plugin/ synced"
    deployed=$((deployed + 1))

    # extension/ resources -> .oxt root
    for item in Addons.xcu Accelerators.xcu description.xml XPythonFunction.rdb XPromptFunction.rdb; do
        if [ -f "$PROJECT_ROOT/extension/$item" ]; then
            rsync -av "$PROJECT_ROOT/extension/$item" "$ext_dir/$item"
            echo "    $item synced"
            deployed=$((deployed + 1))
        fi
    done
    for dir in META-INF assets registration registry; do
        if [ -d "$PROJECT_ROOT/extension/$dir" ]; then
            rsync -av --delete "$PROJECT_ROOT/extension/$dir/" "$ext_dir/$dir/"
            echo "    $dir/ synced"
            deployed=$((deployed + 1))
        fi
    done

    # Sync dialogs (static + dynamically generated)
    if [ -d "$PROJECT_ROOT/extension/WriterAgentDialogs" ]; then
        local excludes=()
        if [ -d "$PROJECT_ROOT/build/generated/WriterAgentDialogs" ]; then
            for f in "$PROJECT_ROOT/build/generated/WriterAgentDialogs"/*; do
                if [ -f "$f" ]; then
                    excludes+=(--exclude "$(basename "$f")")
                fi
            done
        fi
        rsync -av --delete "${excludes[@]}" "$PROJECT_ROOT/extension/WriterAgentDialogs/" "$ext_dir/WriterAgentDialogs/"
        echo "    WriterAgentDialogs/ (static) synced"
        deployed=$((deployed + 1))
    fi
    if [ -d "$PROJECT_ROOT/build/generated/WriterAgentDialogs" ]; then
        rsync -av "$PROJECT_ROOT/build/generated/WriterAgentDialogs/" "$ext_dir/WriterAgentDialogs/"
        echo "    WriterAgentDialogs/ (generated) synced"
    fi
    if [ -d "$PROJECT_ROOT/build/generated/dialogs" ]; then
        rsync -av --delete "$PROJECT_ROOT/build/generated/dialogs/" "$ext_dir/dialogs/"
        echo "    dialogs/ synced"
        deployed=$((deployed + 1))
    fi


    # Clean __pycache__
    find "$ext_dir" -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true

    echo ""
    echo "[OK] Deployed $deployed items to cache"
    echo "    Restart LibreOffice to pick up changes."
    echo ""
}

# ── Main ─────────────────────────────────────────────────────────────────────

echo ""
echo "========================================"
echo "  WriterAgent Plugin Installer"
echo "========================================"
echo ""

# Cache mode
if $CACHE; then
    install_to_cache
    exit 0
fi

# Find unopkg
UNOPKG=$(find_unopkg)
if [ -z "$UNOPKG" ]; then
    echo "[X] unopkg not found. Install LibreOffice first."
    exit 1
fi
echo "[OK] unopkg: $UNOPKG"

# Uninstall mode
if $UNINSTALL; then
    uninstall_extension "$UNOPKG"
    exit $?
fi

# Build
build_oxt || exit 1

if $BUILD_ONLY; then
    echo ""
    echo "[OK] Build complete. Install manually with:"
    echo "    $UNOPKG add $OXT_FILE"
    exit 0
fi

# Install
install_extension "$UNOPKG" || exit 1

# Restart LibreOffice?
if confirm_or_force "Start LibreOffice now?"; then
    echo "[*] Starting LibreOffice..."
    SOFFICE=$(find_soffice)
    if [ -z "$SOFFICE" ]; then
        echo "[X] soffice not found. Install LibreOffice first."
        exit 1
    fi
    "$SOFFICE" &
    echo "[OK] LibreOffice started"
fi

echo ""
echo "========================================"
echo "  Done!"
echo "========================================"
echo ""
