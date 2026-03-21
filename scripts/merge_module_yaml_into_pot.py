#!/usr/bin/env python3
"""Merge translatable strings from plugin/modules/**/module.yaml into writeragent.pot.

Run after xgettext so the POT already contains Python (and generated XDL stub) strings.
Uses polib; skips msgid+msgctxt pairs already present. Idempotent.

Usage:
  python scripts/merge_module_yaml_into_pot.py [path/to/writeragent.pot]

Default path: plugin/locales/writeragent.pot (relative to repo root).
"""
from __future__ import annotations

import os
import sys

import polib
import yaml


def _repo_root() -> str:
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _walk_module_yamls(modules_root: str) -> list[str]:
    out = []
    if not os.path.isdir(modules_root):
        return out
    for dirpath, _dirnames, filenames in os.walk(modules_root):
        if "module.yaml" in filenames:
            out.append(os.path.join(dirpath, "module.yaml"))
    return sorted(out)


def _collect_strings_from_module_yaml(path: str) -> list[tuple[str, str]]:
    """Return list of (msgid, msgctxt) for strings to add."""
    module_name = os.path.basename(os.path.dirname(path))
    with open(path, encoding="utf-8") as f:
        data = yaml.safe_load(f)
    if not isinstance(data, dict):
        return []

    results: list[tuple[str, str]] = []

    title = data.get("title")
    if isinstance(title, str) and title.strip():
        t = title.strip()
        results.append((t, None))

    config = data.get("config")
    if not isinstance(config, dict):
        return results

    for field_name, schema in config.items():
        if not isinstance(schema, dict):
            continue
        if schema.get("internal"):
            continue
        for key in ("label", "helper"):
            val = schema.get(key)
            if isinstance(val, str) and val.strip():
                results.append((val.strip(), None))
        opts = schema.get("options")
        if isinstance(opts, list):
            for i, opt in enumerate(opts):
                if isinstance(opt, dict):
                    lab = opt.get("label")
                    if isinstance(lab, str) and lab.strip():
                        results.append((lab.strip(), None))

    return results


def _entry_exists(pot: polib.POFile, msgid: str, msgctxt: str | None) -> bool:
    for e in pot:
        if e.msgid != msgid:
            continue
        ec = getattr(e, "msgctxt", None)
        if ec == msgctxt:
            return True
    return False


def merge_yaml_into_pot(pot_path: str) -> int:
    if not os.path.isfile(pot_path):
        print(f"error: POT file not found: {pot_path}", file=sys.stderr)
        print("Run xgettext first (e.g. make extract-strings).", file=sys.stderr)
        return 1

    root = _repo_root()
    modules_root = os.path.join(root, "plugin", "modules")
    pairs: list[tuple[str, str]] = []
    for ypath in _walk_module_yamls(modules_root):
        pairs.extend(_collect_strings_from_module_yaml(ypath))

    # Dedupe (msgid, msgctxt) from overlapping YAML paths
    seen_keys: set[tuple[str, str | None]] = set()
    unique_pairs: list[tuple[str, str]] = []
    for msgid, msgctxt in pairs:
        key = (msgid, msgctxt)
        if key in seen_keys:
            continue
        seen_keys.add(key)
        unique_pairs.append((msgid, msgctxt))

    pot = polib.pofile(pot_path)
    added = 0
    for msgid, msgctxt in unique_pairs:
        if not msgid:
            continue
        if _entry_exists(pot, msgid, msgctxt):
            continue
        entry = polib.POEntry(msgid=msgid, msgstr="", msgctxt=msgctxt)
        pot.append(entry)
        added += 1

    pot.save(pot_path)
    print(f"merge_module_yaml_into_pot: {pot_path} (+{added} entries, {len(pairs)} strings from YAML)")
    return 0


def main() -> int:
    root = _repo_root()
    default_pot = os.path.join(root, "plugin", "locales", "writeragent.pot")
    pot_path = os.path.abspath(sys.argv[1]) if len(sys.argv) > 1 else default_pot
    return merge_yaml_into_pot(pot_path)


if __name__ == "__main__":
    sys.exit(main())
