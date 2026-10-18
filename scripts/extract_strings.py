#!/usr/bin/env python3
# WriterAgent — extract translatable strings into the .pot and merge the .po files
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Extract translatable strings and update localization catalogs."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

SCRIPTS_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPTS_DIR.parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import extract_xdl_strings  # noqa: E402
from merge_module_yaml_into_pot import merge_yaml_into_pot  # noqa: E402


def _find_py_files(dir_path: str | Path, root_path: str | Path = PROJECT_ROOT) -> list[str]:
    """Find .py files using depth-first os.scandir order without sorting."""
    results: list[str] = []
    with os.scandir(dir_path) as it:
        for entry in it:
            if entry.is_dir(follow_symlinks=False):
                results.extend(_find_py_files(entry.path, root_path))
            elif entry.is_file(follow_symlinks=False) and entry.name.endswith(".py"):
                rel_path = os.path.relpath(entry.path, root_path).replace("\\", "/")
                results.append(rel_path)
    return results


def generate_pot(pot_only: bool = False, root_path: Path = PROJECT_ROOT) -> int:
    """Generate writeragent.pot from Python files, XDL dialogs, and module.yaml."""
    if shutil.which("xgettext") is None:
        if pot_only:
            print("Skipping .pot regeneration (xgettext not found; install gettext: choco install gettext.install)")
        else:
            print("Skipping string extraction (xgettext not found; install gettext: choco install gettext.install)")
        return 0

    if pot_only:
        print("Regenerating translation templates (.pot) without updating .po...")

    stub_path = root_path / "plugin" / "xdl_strings.py"
    list_path: str | None = None
    try:
        extract_xdl_strings.main()
        py_files = _find_py_files(root_path / "plugin", root_path)
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False) as tf:
            for p in py_files:
                tf.write(f"{p}\n")
            list_path = tf.name

        cmd = ["xgettext", "--add-location=file"]
        if not pot_only:
            cmd.append("--keyword=N_")
        cmd.extend([
            "-d",
            "writeragent",
            "-o",
            "locales/writeragent.pot",
            "-f",
            list_path,
        ])
        proc = subprocess.run(cmd, cwd=str(root_path))
        if proc.returncode != 0:
            return proc.returncode

        pot_file = str(root_path / "locales" / "writeragent.pot")
        yaml_ret = merge_yaml_into_pot(pot_file)
        if yaml_ret != 0:
            return yaml_ret
        return 0
    finally:
        if list_path and os.path.exists(list_path):
            try:
                os.unlink(list_path)
            except OSError:
                pass
        if stub_path.exists():
            try:
                stub_path.unlink()
            except OSError:
                pass


def merge_translations(root_path: Path = PROJECT_ROOT) -> int:
    """Merge writeragent.pot into each locale's writeragent.po."""
    if shutil.which("msgmerge") is None or shutil.which("msgattrib") is None:
        print("Skipping .po merge (msgmerge not found; install gettext: choco install gettext.install)")
        return 0

    po_files = sorted(root_path.glob("locales/**/writeragent.po"))
    any_failed = False

    for po_path in po_files:
        merge_cmd = [
            "msgmerge",
            "--add-location=file",
            "--update",
            "--backup=none",
            str(po_path),
            "locales/writeragent.pot",
        ]
        merge_proc = subprocess.run(merge_cmd, cwd=str(root_path))
        if merge_proc.returncode != 0:
            any_failed = True
            continue

        # Drop obsolete (#~) entries so removed strings do not pile up; msgattrib works where msgmerge lacks --no-obsolete.
        tmp_po = f"{po_path}.tmp"
        attrib_cmd = [
            "msgattrib",
            "--no-obsolete",
            "-o",
            tmp_po,
            str(po_path),
        ]
        attrib_proc = subprocess.run(attrib_cmd, cwd=str(root_path))
        if attrib_proc.returncode != 0:
            any_failed = True
            if os.path.exists(tmp_po):
                try:
                    os.unlink(tmp_po)
                except OSError:
                    pass
            continue

        os.replace(tmp_po, str(po_path))

    return 1 if any_failed else 0


def auto_translate(root_path: Path = PROJECT_ROOT) -> int:
    """Regenerate templates, run preview, and fill missing translations via AI if enabled."""
    print("Regenerating translation templates (.pot)...")
    run_default_pipeline(root_path)

    preview_proc = subprocess.run(
        [sys.executable, str(SCRIPTS_DIR / "translate_missing.py"), "--preview"],
        cwd=str(root_path),
    )

    if os.environ.get("OPENROUTER_API_KEY"):
        print("Auto-translating missing strings with AI...")
        exec_proc = subprocess.run(
            [
                sys.executable,
                str(SCRIPTS_DIR / "translate_missing.py"),
                "--execute",
                "--skip-initial-status",
            ],
            cwd=str(root_path),
        )
        return exec_proc.returncode

    return preview_proc.returncode


def run_default_pipeline(root_path: Path = PROJECT_ROOT) -> int:
    """Run full string extraction pipeline: generate POT then merge PO catalogs."""
    if shutil.which("xgettext") is None:
        print("Skipping string extraction (xgettext not found; install gettext: choco install gettext.install)")
        return 0

    pot_ret = generate_pot(pot_only=False, root_path=root_path)
    if pot_ret != 0:
        return pot_ret

    return merge_translations(root_path=root_path)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        "--pot-only",
        action="store_true",
        help="Regenerate .pot without updating .po catalogs",
    )
    group.add_argument(
        "--merge-only",
        action="store_true",
        help="Merge existing .pot into .po catalogs without regenerating .pot",
    )
    group.add_argument(
        "--auto-translate",
        action="store_true",
        help="Regenerate templates, preview, and auto-translate missing strings",
    )

    args = parser.parse_args(argv)

    if args.pot_only:
        return generate_pot(pot_only=True)
    if args.merge_only:
        return merge_translations()
    if args.auto_translate:
        return auto_translate()
    return run_default_pipeline()


if __name__ == "__main__":
    sys.exit(main())
