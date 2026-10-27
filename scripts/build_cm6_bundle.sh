#!/usr/bin/env bash
# Rebuild the vendored CodeMirror 6 bundle for the pywebview Python editor.
#
# Output (committed): plugin/contrib/scripting/assets/editor/cm6/cm6.bundle.js
#                     plugin/contrib/scripting/assets/editor/cm6/LICENSE-codemirror.txt
#
# Not part of make build / make release: run by hand after bumping the pinned
# versions in scripts/cm6/package.json, then commit package-lock.json and the
# output. Needs node + npm. node_modules stays in scripts/cm6 (gitignored).
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SRC="$ROOT/scripts/cm6"
OUT="$ROOT/plugin/contrib/scripting/assets/editor/cm6"

cd "$SRC"
npm ci --no-audit --no-fund
mkdir -p "$OUT"
npx --no-install esbuild entry.js \
  --bundle --minify --format=iife --global-name=CM6 \
  --target=es2020 --legal-comments=none --charset=utf8 \
  --outfile="$OUT/cm6.bundle.js"

# Every bundled @codemirror/* and @lezer/* package is MIT (same copyright
# holder); ship one license plus the list of bundled package versions.
{
  cat node_modules/@codemirror/view/LICENSE
  echo
  echo "Bundled packages (all MIT):"
  for pkg in node_modules/codemirror node_modules/@codemirror/* node_modules/@lezer/* node_modules/@marijn/* node_modules/crelt node_modules/style-mod node_modules/w3c-keyname; do
    [ -f "$pkg/package.json" ] || continue
    node -e 'const p=require(process.argv[1]);console.log(`  ${p.name} ${p.version} (${p.license})`)' "$PWD/$pkg/package.json"
  done
} > "$OUT/LICENSE-codemirror.txt"

ls -l "$OUT"
