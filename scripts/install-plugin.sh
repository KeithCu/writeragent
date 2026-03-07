#!/bin/bash
# Build and install the LocalWriter extension (.oxt).
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
BUILD_DIR="$PROJECT_ROOT/build"
OXT_FILE="$BUILD_DIR/WriterAgent.oxt"

EXTENSION_ID="org.extension.writeragent"

# Parse args
FORCE=false
BUILD_ONLY=false
UNINSTALL=false
CACHE=false
MODULES="core writer calc draw ai_openai ai_ollama ai_horde chatbot mcp"
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

find_unopkg() {
    for candidate in \
        /usr/bin/unopkg \
        /usr/lib/libreoffice/program/unopkg \
        /usr/lib64/libreoffice/program/unopkg \
        /opt/libreoffice*/program/unopkg \
        /snap/bin/libreoffice.unopkg; do
        for c in $candidate; do
            if [ -x "$c" ]; then
                echo "$c"
                return
            fi
        done
    done
    command -v unopkg 2>/dev/null || true
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
    echo ""
    echo "=== Building WriterAgent.oxt (modules: $MODULES) ==="
    echo ""

    mkdir -p "$BUILD_DIR"
    rm -f "$OXT_FILE"

    # Generate manifests from module.yaml files
    python3 "$SCRIPT_DIR/generate_manifest.py"

    # Build the .oxt
    python3 "$SCRIPT_DIR/build_oxt.py" \
        --modules $MODULES \
        --output "$OXT_FILE"

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

find_unopkg_cache_dir() {
    local candidates=(
        "$HOME/.config/libreoffice/4/user/uno_packages"
    )
    for profile_dir in "$HOME/.config/libreoffice"; do
        if [ -d "$profile_dir" ]; then
            while IFS= read -r -d '' d; do
                candidates+=("$d")
            done < <(find "$profile_dir" -type d -name "uno_packages" -print0 2>/dev/null)
        fi
    done
    for c in "${candidates[@]}"; do
        if [ -d "$c" ]; then
            echo "$c"
            return
        fi
    done
}

install_to_cache() {
    echo ""
    echo "=== Cache Install (hot-deploy) ==="
    echo ""

    local cache_dir
    cache_dir=$(find_unopkg_cache_dir)
    if [ -z "$cache_dir" ]; then
        echo "[X] Could not find uno_packages cache directory"
        echo "    Run a normal install first: $0 --force"
        exit 1
    fi

    local packages_dir="$cache_dir/cache/uno_packages"
    if [ ! -d "$packages_dir" ]; then
        echo "[X] Cache packages dir not found: $packages_dir"
        echo "    Run a normal install first: $0 --force"
        exit 1
    fi

    # Find the *.tmp_ directory containing our extension
    local ext_dir=""
    for d in "$packages_dir"/*.tmp_; do
        if [ -d "$d/WriterAgent.oxt" ]; then
            ext_dir="$d/WriterAgent.oxt"
            break
        fi
    done
    if [ -z "$ext_dir" ]; then
        echo "[X] Extension not found in cache. Run a normal install first."
        exit 1
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
    for item in Addons.xcu Accelerators.xcu Jobs.xcu description.xml XPromptFunction.rdb; do
        if [ -f "$PROJECT_ROOT/extension/$item" ]; then
            cp "$PROJECT_ROOT/extension/$item" "$ext_dir/$item"
            echo "    $item"
            deployed=$((deployed + 1))
        fi
    done
    for dir in META-INF assets registration registry WriterAgentDialogs; do
        if [ -d "$PROJECT_ROOT/extension/$dir" ]; then
            rsync -av --delete "$PROJECT_ROOT/extension/$dir/" "$ext_dir/$dir/"
            echo "    $dir/ synced"
            deployed=$((deployed + 1))
        fi
    done


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
echo "  LocalWriter Plugin Installer"
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
    soffice &
    echo "[OK] LibreOffice started"
fi

echo ""
echo "========================================"
echo "  Done!"
echo "========================================"
echo ""
