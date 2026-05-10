#!/usr/bin/env python3
"""Merge translatable strings from plugin/**/module.yaml into writeragent.pot.

Run after xgettext so the POT already contains Python (and generated XDL stub) strings.
Uses polib; skips msgids already present. Idempotent.

Usage:
  python scripts/merge_module_yaml_into_pot.py [path/to/writeragent.pot]

Default path: locales/writeragent.pot (relative to repo root).
"""
from __future__ import annotations

import os
import sys

import polib
import yaml


def _repo_root() -> str:
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# Skip trees that are not application modules (no gettext from vendored code).
_SKIP_PLUGIN_SUBDIRS = frozenset({"contrib", "tests", "lib", "__pycache__"})


def _walk_module_yamls(plugin_root: str) -> list[str]:
    """Paths to ``module.yaml`` under ``plugin/<pkg>/`` (not ``plugin/modules/`` — that layout is unused)."""
    out: list[str] = []
    if not os.path.isdir(plugin_root):
        return out
    for dirpath, dirnames, filenames in os.walk(plugin_root):
        dirnames[:] = [d for d in dirnames if d not in _SKIP_PLUGIN_SUBDIRS and not d.startswith(".")]
        if "module.yaml" in filenames:
            out.append(os.path.join(dirpath, "module.yaml"))
    return sorted(out)


def _collect_strings_from_module_yaml(path: str) -> list[str]:
    """Return msgids to add from one module.yaml."""
    with open(path, encoding="utf-8") as f:
        data = yaml.safe_load(f)
    if not isinstance(data, dict):
        return []

    results: list[str] = []

    title = data.get("title")
    if isinstance(title, str) and title.strip():
        results.append(title.strip())

    config = data.get("config")
    if not isinstance(config, dict):
        return results

    for _, schema in config.items():
        if not isinstance(schema, dict):
            continue
        if schema.get("internal"):
            continue
        for key in ("label", "helper"):
            val = schema.get(key)
            if isinstance(val, str) and val.strip():
                results.append(val.strip())
        opts = schema.get("options")
        if isinstance(opts, list):
            for i, opt in enumerate(opts):
                if isinstance(opt, dict):
                    lab = opt.get("label")
                    if isinstance(lab, str) and lab.strip():
                        results.append(lab.strip())

    return results


def _entry_exists(pot: polib.POFile, msgid: str) -> bool:
    for e in pot:
        if e.msgid == msgid:
            return True
    return False


def merge_yaml_into_pot(pot_path: str) -> int:
    if not os.path.isfile(pot_path):
        print(f"error: POT file not found: {pot_path}", file=sys.stderr)
        print("Run xgettext first (e.g. make extract-strings).", file=sys.stderr)
        return 1

    root = _repo_root()
    plugin_root = os.path.join(root, "plugin")
    raw: list[str] = []
    for ypath in _walk_module_yamls(plugin_root):
        raw.extend(_collect_strings_from_module_yaml(ypath))

    seen: set[str] = set()
    unique_msgids: list[str] = []
    for msgid in raw:
        if msgid in seen:
            continue
        seen.add(msgid)
        unique_msgids.append(msgid)

    pot = polib.pofile(pot_path)
    added = 0
    for msgid in unique_msgids:
        if not msgid:
            continue
        if _entry_exists(pot, msgid):
            continue
        pot.append(polib.POEntry(msgid=msgid, msgstr=""))
        added += 1

    pot.save(pot_path)
    print(f"merge_module_yaml_into_pot: {pot_path} (+{added} entries, {len(raw)} strings from YAML)")
    return 0


def main() -> int:
    root = _repo_root()
    default_pot = os.path.join(root, "locales", "writeragent.pot")
    pot_path = os.path.abspath(sys.argv[1]) if len(sys.argv) > 1 else default_pot
    return merge_yaml_into_pot(pot_path)


if __name__ == "__main__":
    sys.exit(main())
