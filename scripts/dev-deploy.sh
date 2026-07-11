#!/bin/bash
# Dev-mode deploy: hot-sync project files into the unopkg cache.
#
# ``make deploy`` runs ``make build`` first, then this script. Registration via
# unopkg happens only when the extension is not yet registered (or cache is
# missing); subsequent deploys sync source files into the cache only.
#
# Usage:
#   ./scripts/dev-deploy.sh              # Regenerate + deploy to cache
#   ./scripts/dev-deploy.sh --no-gen     # Deploy only (skip generate_manifest)

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"

NO_GEN=false
if [ "${1:-}" = "--no-gen" ]; then
    NO_GEN=true
fi

echo ""
echo "=== Dev Deploy ==="
echo ""

# ── Regenerate manifests ────────────────────────────────────────────────────

if ! $NO_GEN; then
    echo "[*] Regenerating manifests..."
    python3 "$SCRIPT_DIR/generate_manifest.py"
    echo ""
fi

# ── Deploy to cache ─────────────────────────────────────────────────────────

exec bash "$SCRIPT_DIR/install-plugin.sh" --cache
