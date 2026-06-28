#!/usr/bin/env bash
# sync-opengrep-rules.sh — Refresh vendored Opengrep rules under tests/semgrep/third_party/
#
# Copies a curated subset from upstream repos at pinned commits (see SOURCES.json).
# Re-run after intentionally bumping pin SHAs in this script.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"
DEST="$PROJECT_ROOT/tests/semgrep/third_party"

SEMGREP_RULES_REPO="https://github.com/semgrep/semgrep-rules"
SEMGREP_RULES_COMMIT="d41fb34cf74466e2878af5f268ebf54466a04541"

TRAILOFBITS_REPO="https://github.com/trailofbits/semgrep-rules"
TRAILOFBITS_COMMIT="31390b3a99c04c81522d1b37c8d1900aa2dd4094"

SEMGREP_RULES=(
    python/lang/security/use-defused-xml.yaml
    python/lang/security/use-defused-xml-parse.yaml
    python/lang/security/audit/insecure-file-permissions.yaml
    python/lang/security/audit/subprocess-shell-true.yaml
    python/lang/security/audit/exec-detected.yaml
    python/lang/security/audit/eval-detected.yaml
    python/lang/security/audit/hardcoded-password-default-argument.yaml
    python/lang/security/deserialization/avoid-pyyaml-load.yaml
)

TRAILOFBITS_RULES=(
    python/tarfile-extractall-traversal.yaml
)

fetch_rule() {
    local repo="$1" commit="$2" relpath="$3" out="$4"
    local url="${repo}/raw/${commit}/${relpath}"
    mkdir -p "$(dirname "$out")"
    curl -fsSL "$url" -o "$out"
}

echo "Syncing vendored Opengrep rules into $DEST ..."

rm -rf "$DEST/semgrep-rules" "$DEST/trailofbits"
mkdir -p "$DEST"

for relpath in "${SEMGREP_RULES[@]}"; do
    fetch_rule "$SEMGREP_RULES_REPO" "$SEMGREP_RULES_COMMIT" "$relpath" "$DEST/semgrep-rules/$relpath"
done

for relpath in "${TRAILOFBITS_RULES[@]}"; do
    fetch_rule "$TRAILOFBITS_REPO" "$TRAILOFBITS_COMMIT" "$relpath" "$DEST/trailofbits/$relpath"
done

# WriterAgent path excludes for rules that fire on vetted venv IPC / dynamic tooling.
append_paths_exclude() {
    local file="$1"
    cat >>"$file" <<'YAML'

  paths:
    exclude:
      - "**/plugin/contrib/**"
      - "**/plugin/lib/**"
      - "**/plugin/scripting/venv/**"
      - "**/plugin/scripting/venv_worker.py"
      - "**/plugin/scripting/editor_host.py"
      - "**/plugin/scripting/editor_ipc.py"
      - "**/plugin/scripting/writeragent_api.py"
YAML
}

append_paths_exclude "$DEST/semgrep-rules/python/lang/security/audit/exec-detected.yaml"
append_paths_exclude "$DEST/semgrep-rules/python/lang/security/audit/eval-detected.yaml"

cat >"$DEST/SOURCES.json" <<EOF
{
  "description": "Pinned third-party Opengrep rules vendored for offline make test. Refresh: make opengrep-rules-sync",
  "excluded_from_bundle": {
    "avoid-pickle": "Intentional venv worker IPC (# nosec B301)",
    "dynamic-urllib-use-detected": "Bandit B310 skipped intentionally for HTTP client",
    "logger-credential-leak": "False positives on grammar debug log strings",
    "sqlalchemy-execute-raw-query": "Controlled SQL in embeddings venv SQLite helpers",
    "non-literal-import": "Core dynamic module loading (module_base, tool registry)"
  },
  "sources": [
    {
      "repo": "$SEMGREP_RULES_REPO",
      "commit": "$SEMGREP_RULES_COMMIT",
      "license": "Semgrep Rules License v1.0",
      "rules": $(printf '%s\n' "${SEMGREP_RULES[@]}" | python3 -c 'import json,sys; print(json.dumps([l.strip() for l in sys.stdin if l.strip()]))')
    },
    {
      "repo": "$TRAILOFBITS_REPO",
      "commit": "$TRAILOFBITS_COMMIT",
      "license": "AGPL-3.0",
      "rules": $(printf '%s\n' "${TRAILOFBITS_RULES[@]}" | python3 -c 'import json,sys; print(json.dumps([l.strip() for l in sys.stdin if l.strip()]))')
    }
  ]
}
EOF

echo "Done. $((${#SEMGREP_RULES[@]} + ${#TRAILOFBITS_RULES[@]})) rule files synced."
