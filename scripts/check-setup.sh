#!/usr/bin/env bash
# check-setup.sh — Verify the LocalWriter development stack.
#
# Usage:
#   ./scripts/check-setup.sh          Check everything
#   bash scripts/check-setup.sh       Same (no +x needed)

set -uo pipefail

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BOLD='\033[1m'
NC='\033[0m'

ERRORS=0
WARNINGS=0
BRIEF=""

ok()   { echo -e "  ${GREEN}OK${NC}   $1"; BRIEF+="OK   $1"$'\n'; }
warn() { echo -e "  ${YELLOW}WARN${NC} $1"; WARNINGS=$((WARNINGS+1)); BRIEF+="WARN $1"$'\n'; }
fail() { echo -e "  ${RED}FAIL${NC} $1"; ERRORS=$((ERRORS+1)); BRIEF+="FAIL $1"$'\n'; }

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"

echo ""
echo -e "${BOLD}LocalWriter — Development Stack Check${NC}"
echo "======================================"
echo ""

# ── OS ─────────────────────────────────────────────────────────────────

OS_INFO="unknown"
if [[ -f /etc/os-release ]]; then
    OS_INFO=$(. /etc/os-release && echo "$PRETTY_NAME")
elif [[ "$(uname)" == "Darwin" ]]; then
    OS_INFO="macOS $(sw_vers -productVersion 2>/dev/null || echo 'unknown')"
fi
ok "OS: $OS_INFO"

# ── Python ─────────────────────────────────────────────────────────────

PYTHON=""
for cmd in python3 python; do
    if command -v "$cmd" &>/dev/null; then
        PYTHON="$cmd"
        break
    fi
done

if [[ -n "$PYTHON" ]]; then
    PY_VER=$("$PYTHON" --version 2>&1 | head -1)
    PY_PATH=$(command -v "$PYTHON")
    ok "$PY_VER ($PY_PATH)"

    # Check it's not inside a venv (unopkg issue)
    if [[ -n "${VIRTUAL_ENV:-}" ]]; then
        warn "Python is inside a venv ($VIRTUAL_ENV) — unopkg may fail with std::bad_alloc"
    fi
else
    fail "Python 3.8+ not found"
fi

# ── pip or uv ──────────────────────────────────────────────────────────

UV=""
if command -v uv &>/dev/null; then
    UV_VER=$(uv --version 2>&1 | head -1)
    ok "uv: $UV_VER"
    UV="uv"
fi

if [[ -n "$PYTHON" ]] && "$PYTHON" -m pip --version &>/dev/null; then
    PIP_VER=$("$PYTHON" -m pip --version 2>&1 | head -1 | awk '{print $1, $2}')
    ok "pip: $PIP_VER"
elif [[ -z "$UV" ]]; then
    fail "Neither pip nor uv found — cannot install dependencies"
fi

# ── PyYAML ─────────────────────────────────────────────────────────────

if [[ -n "$PYTHON" ]] && "$PYTHON" -c "import yaml" 2>/dev/null; then
    YAML_VER=$("$PYTHON" -c "import yaml; print(yaml.__version__)" 2>/dev/null || echo "?")
    ok "PyYAML: $YAML_VER"
else
    fail "PyYAML not installed — run: ./install.sh"
fi

# ── LibreOffice ────────────────────────────────────────────────────────

LO=""
for cmd in soffice libreoffice; do
    if command -v "$cmd" &>/dev/null; then
        LO="$cmd"
        break
    fi
done

if [[ -n "$LO" ]]; then
    LO_VER=$("$LO" --version 2>&1 | head -1 || echo "?")
    ok "LibreOffice: $LO_VER"
else
    fail "LibreOffice (soffice) not found"
fi

# ── unopkg ─────────────────────────────────────────────────────────────

UNOPKG=""
for candidate in \
    /usr/bin/unopkg \
    /usr/lib/libreoffice/program/unopkg \
    /usr/lib64/libreoffice/program/unopkg \
    /opt/libreoffice*/program/unopkg \
    /snap/bin/libreoffice.unopkg; do
    for c in $candidate; do
        if [[ -x "$c" ]]; then
            UNOPKG="$c"
            break 2
        fi
    done
done
[[ -z "$UNOPKG" ]] && UNOPKG=$(command -v unopkg 2>/dev/null || true)

if [[ -n "$UNOPKG" ]]; then
    ok "unopkg: $UNOPKG"
else
    fail "unopkg not found — check LibreOffice installation"
fi

# ── make ───────────────────────────────────────────────────────────────

if command -v make &>/dev/null; then
    MAKE_VER=$(make --version 2>&1 | head -1)
    ok "make: $MAKE_VER"
else
    fail "make not found — install: sudo dnf install make / sudo apt install make"
fi

# ── git ────────────────────────────────────────────────────────────────

if command -v git &>/dev/null; then
    GIT_VER=$(git --version 2>&1)
    ok "git: $GIT_VER"
else
    fail "git not found"
fi

# ── openssl (optional) ────────────────────────────────────────────────

if command -v openssl &>/dev/null; then
    SSL_VER=$(openssl version 2>&1)
    ok "openssl: $SSL_VER (optional, for MCP HTTPS)"
else
    warn "openssl not found (optional, for MCP HTTPS)"
fi

# ── Project files ──────────────────────────────────────────────────────

echo ""
echo -e "${BOLD}Project${NC}"
echo "-------"

if [[ -f "$PROJECT_ROOT/plugin/version.py" ]]; then
    EXT_VER=$("$PYTHON" -c "
import sys; sys.path.insert(0, '$PROJECT_ROOT')
from plugin.version import EXTENSION_VERSION
print(EXTENSION_VERSION)
" 2>/dev/null || echo "?")
    ok "Extension version: $EXT_VER"
else
    warn "plugin/version.py not found"
fi

if [[ -d "$PROJECT_ROOT/vendor" ]] && [[ "$(ls -A "$PROJECT_ROOT/vendor" 2>/dev/null)" ]]; then
    ok "vendor/ populated"
else
    warn "vendor/ empty — run: make vendor"
fi

if [[ -f "$PROJECT_ROOT/build/WriterAgent.oxt" ]]; then
    OXT_SIZE=$(stat -c%s "$PROJECT_ROOT/build/WriterAgent.oxt" 2>/dev/null || stat -f%z "$PROJECT_ROOT/build/WriterAgent.oxt" 2>/dev/null || echo "?")
    ok "build/WriterAgent.oxt exists ($OXT_SIZE bytes)"
else
    warn "No .oxt built yet — run: make build"
fi

# ── Extension installed? ──────────────────────────────────────────────

if [[ -n "$UNOPKG" ]]; then
    if $UNOPKG list 2>&1 | grep -q "org.extension.writeragent"; then
        ok "Extension registered in LibreOffice"
    else
        warn "Extension not registered — run: make deploy"
    fi
fi

# ── Log symlinks ─────────────────────────────────────────────────────

LOG_FILES="writeragent.log soffice-debug.log"
for f in $LOG_FILES; do
    target="$HOME/$f"
    link="$PROJECT_ROOT/$f"
    if [[ -L "$link" ]]; then
        ok "Symlink $f already exists"
    elif [[ -e "$link" ]]; then
        warn "$f exists but is not a symlink — skipping"
    else
        # Create the target if it doesn't exist yet
        touch "$target" 2>/dev/null || true
        if ln -s "$target" "$link" 2>/dev/null; then
            ok "Symlink created: $f -> $target"
        else
            warn "Could not create symlink $f"
        fi
    fi
done

# ── Summary ───────────────────────────────────────────────────────────

echo ""
echo "======================================"
if [[ $ERRORS -gt 0 ]]; then
    echo -e "${RED}${BOLD}$ERRORS error(s)${NC}, $WARNINGS warning(s)"
    echo ""
    echo "Fix the errors above before building. See DEVEL.md for instructions."
elif [[ $WARNINGS -gt 0 ]]; then
    echo -e "${GREEN}${BOLD}All required tools found${NC}, $WARNINGS warning(s)"
else
    echo -e "${GREEN}${BOLD}Everything looks good!${NC}"
fi

echo ""
echo -e "${BOLD}--- Copy-paste brief ---${NC}"
echo ""
echo "$BRIEF"
echo "OS:   $OS_INFO"
echo "Errors: $ERRORS / Warnings: $WARNINGS"

exit $ERRORS
