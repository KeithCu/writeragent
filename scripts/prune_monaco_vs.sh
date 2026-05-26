#!/usr/bin/env bash
# Drop Monaco assets not needed for the Python-only Calc editor (AMD min tree).
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
VS="$ROOT/plugin/contrib/scripting/assets/editor/vs"

if [[ ! -d "$VS" ]]; then
  echo "prune_monaco_vs: missing $VS (run fetch_monaco_editor.sh first)" >&2
  exit 1
fi

rm -rf "$VS/language"

for lang_dir in "$VS/basic-languages"/*/; do
  [[ -d "$lang_dir" ]] || continue
  base="$(basename "$lang_dir")"
  if [[ "$base" != "python" && "$base" != "latex" ]]; then
    rm -rf "$lang_dir"
  fi
done

for locale in de es fr it ja ko ru zh-cn zh-tw; do
  rm -f "$VS/nls.messages.${locale}.js"
done

echo "prune_monaco_vs: python-only tree under $VS"
