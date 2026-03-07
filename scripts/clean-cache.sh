#!/bin/bash
# Clean / repair the LibreOffice extension cache for localwriter.
#
# Adapted from mcp-libre/scripts/clean-cache.sh.
#
# Usage:
#   ./scripts/clean-cache.sh              # Fix revoked flags + remove stale locks
#   ./scripts/clean-cache.sh --nuke       # Wipe the entire user extension cache
#   ./scripts/clean-cache.sh --unbundle   # Remove bundled symlink (needs sudo)

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"

EXTENSION_ID="org.extension.writeragent"
EXTENSION_OXT="WriterAgent.oxt"
BUNDLE_NAME="WriterAgent"

# Parse args
NUKE=false
UNBUNDLE=false
for arg in "$@"; do
    case "$arg" in
        --nuke)     NUKE=true ;;
        --unbundle) UNBUNDLE=true ;;
        -h|--help)
            echo "Usage: $0 [--nuke] [--unbundle]"
            echo ""
            echo "  (default)    Fix revoked flags in backenddb, remove stale locks"
            echo "  --nuke       Wipe the entire user extension cache (will need unopkg reinstall)"
            echo "  --unbundle   Remove bundled dev-deploy symlink from LO share/extensions (needs sudo)"
            exit 0
            ;;
    esac
done

# ── Cache probing ────────────────────────────────────────────────────────────

find_cache_dir() {
    local candidates=(
        "$HOME/.config/libreoffice/4/user/uno_packages"
        "$HOME/Library/Application Support/LibreOffice/4/user/uno_packages"
    )
    for profile_dir in "$HOME/.config/libreoffice" "$HOME/Library/Application Support/LibreOffice"; do
        if [ -d "$profile_dir" ]; then
            while IFS= read -r -d '' d; do
                candidates+=("$d")
            done < <(find "$profile_dir" -type d -name "uno_packages" -print0 2>/dev/null)
        fi
    done
    # Snap
    local snap_data="$HOME/snap/libreoffice/current/.config/libreoffice"
    if [ -d "$snap_data" ]; then
        while IFS= read -r -d '' d; do
            candidates+=("$d")
        done < <(find "$snap_data" -type d -name "uno_packages" -print0 2>/dev/null)
    fi
    # Flatpak
    local flatpak_data="$HOME/.var/app/org.libreoffice.LibreOffice/config/libreoffice"
    if [ -d "$flatpak_data" ]; then
        while IFS= read -r -d '' d; do
            candidates+=("$d")
        done < <(find "$flatpak_data" -type d -name "uno_packages" -print0 2>/dev/null)
    fi

    for c in "${candidates[@]}"; do
        if [ -d "$c" ]; then
            echo "$c"
            return
        fi
    done
}

find_lo_ext_dir() {
    for p in \
        /usr/lib/libreoffice/share/extensions \
        /usr/lib64/libreoffice/share/extensions \
        /opt/libreoffice*/share/extensions \
        /snap/libreoffice/current/lib/libreoffice/share/extensions \
        /usr/local/lib/libreoffice/share/extensions; do
        for d in $p; do
            if [ -d "$d" ]; then
                echo "$d"
                return
            fi
        done
    done
}

# ── Unbundle mode ────────────────────────────────────────────────────────────

if $UNBUNDLE; then
    lo_ext_dir=$(find_lo_ext_dir)
    if [ -z "$lo_ext_dir" ]; then
        echo "[X] LibreOffice share/extensions not found"
        exit 1
    fi
    symlink="$lo_ext_dir/$BUNDLE_NAME"
    if [ -L "$symlink" ]; then
        echo "[*] Removing bundled symlink: $symlink -> $(readlink "$symlink")"
        sudo rm "$symlink"
        echo "[OK] Bundled symlink removed"
    elif [ -d "$symlink" ]; then
        echo "[!!] $symlink is a directory (not a symlink), skipping"
        echo "    Remove manually if needed: sudo rm -rf $symlink"
    else
        echo "[OK] No bundled symlink to remove"
    fi
    exit 0
fi

# ── Find cache ───────────────────────────────────────────────────────────────

CACHE_DIR=$(find_cache_dir)
if [ -z "$CACHE_DIR" ]; then
    echo "[X] Could not find uno_packages cache directory"
    exit 1
fi
echo "[OK] Cache dir: $CACHE_DIR"

# ── Nuke mode ────────────────────────────────────────────────────────────────

if $NUKE; then
    echo ""
    echo "[!!] This will wipe the entire user extension cache."
    echo "     You will need to reinstall extensions with unopkg afterwards."
    read -rp "Continue? (y/N) " response
    if [[ ! "$response" =~ ^[Yy] ]]; then
        echo "Aborted."
        exit 0
    fi

    # Remove lock first
    LO_USER_DIR="$(dirname "$CACHE_DIR")"
    rm -f "$LO_USER_DIR/../.lock" 2>/dev/null

    rm -rf "$CACHE_DIR/cache"
    rm -f "$CACHE_DIR/uno_packages.pmap" 2>/dev/null
    echo "[OK] Cache wiped: $CACHE_DIR/cache"
    echo "    Restart LibreOffice to regenerate, then reinstall extensions."
    exit 0
fi

# ── Repair mode (default) ─────────────────────────────────────────────────

echo ""
echo "=== Repairing extension cache ==="
echo ""

fixed=0

# 1. Remove stale lock files
echo "[*] Checking for stale locks..."
while IFS= read -r -d '' lockfile; do
    rm -f "$lockfile"
    echo "    Removed: $lockfile"
    fixed=$((fixed + 1))
done < <(find "$CACHE_DIR" -name "*.lock" -print0 2>/dev/null)

# Also check the LO user profile lock
LO_USER_DIR="$(dirname "$CACHE_DIR")"
LO_LOCK="$LO_USER_DIR/../.lock"
if [ -f "$LO_LOCK" ] && ! pgrep -x "soffice.bin" >/dev/null 2>&1; then
    rm -f "$LO_LOCK"
    echo "    Removed stale LO lock: $LO_LOCK"
    fixed=$((fixed + 1))
fi

# 2. Fix revoked flags in backenddb files
echo "[*] Checking for revoked extensions..."
REGISTRY_DIR="$CACHE_DIR/cache/registry"
if [ -d "$REGISTRY_DIR" ]; then
    while IFS= read -r -d '' dbfile; do
        if grep -q 'revoked="true"' "$dbfile" 2>/dev/null; then
            sed -i 's/ revoked="true"//g' "$dbfile"
            echo "    Fixed revoked flags: $(basename "$(dirname "$dbfile")")"
            fixed=$((fixed + 1))
        fi
    done < <(find "$REGISTRY_DIR" -name "backenddb.xml" -print0)
fi

# 3. Check for ghost installs (orphaned .tmp_ dirs)
echo "[*] Checking for ghost installs..."
PACKAGES_DIR="$CACHE_DIR/cache/uno_packages"
if [ -d "$PACKAGES_DIR" ]; then
    for tmpdir in "$PACKAGES_DIR"/*.tmp_; do
        [ -d "$tmpdir" ] || continue
        oxt_dir="$tmpdir/$EXTENSION_OXT"
        if [ -d "$oxt_dir" ]; then
            if [ ! -f "$oxt_dir/plugin/version.py" ] && [ ! -f "$oxt_dir/registration.py" ]; then
                echo "    Ghost install found: $(basename "$tmpdir")"
                echo "    Run --nuke to clean up, or reinstall with: install-plugin.sh --force"
                fixed=$((fixed + 1))
            fi
        fi
    done
fi

# 4. Check for bundled symlink conflict
echo "[*] Checking for bundled symlink conflict..."
lo_ext_dir=$(find_lo_ext_dir)
if [ -n "$lo_ext_dir" ] && [ -L "$lo_ext_dir/$BUNDLE_NAME" ]; then
    echo "    [!!] Bundled symlink found: $lo_ext_dir/$BUNDLE_NAME -> $(readlink "$lo_ext_dir/$BUNDLE_NAME")"
    echo "    This conflicts with the user-installed extension."
    echo "    Run: $0 --unbundle  (needs sudo)"
    fixed=$((fixed + 1))
fi

# Report
echo ""
if [ "$fixed" -eq 0 ]; then
    echo "[OK] Cache looks clean, nothing to fix."
else
    echo "[OK] Fixed $fixed issue(s). Restart LibreOffice to apply."
fi
echo ""
