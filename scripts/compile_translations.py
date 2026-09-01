#!/usr/bin/env python3
"""Compile ``locales/*/LC_MESSAGES/*.po`` to ``.mo`` without GNU ``msgfmt``.

Windows PR CI (33453184665) ran ``make compile-translations`` after
``choco install gettext || true`` but never put ``msgfmt`` on PATH, so the
Makefile ``command -v msgfmt`` branch skipped silently and gettext tests
fell back to English. ``polib.save_as_mofile`` is already a project
dependency (LibrePy locale filter) and works on every runner OS.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import polib

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_LOCALES = PROJECT_ROOT / "locales"


def compile_po(po_path: Path, mo_path: Path | None = None) -> Path:
    """Write the GNU ``.mo`` next to *po_path* (or to *mo_path*)."""
    dest = mo_path if mo_path is not None else po_path.with_suffix(".mo")
    dest.parent.mkdir(parents=True, exist_ok=True)
    catalog = polib.pofile(str(po_path))
    catalog.save_as_mofile(str(dest))
    return dest


def compile_locales_tree(locales_dir: Path) -> list[Path]:
    """Compile every ``*/LC_MESSAGES/*.po`` under *locales_dir*."""
    written: list[Path] = []
    for po_path in sorted(locales_dir.glob("*/LC_MESSAGES/*.po")):
        written.append(compile_po(po_path))
    return written


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "locales_dir",
        nargs="?",
        type=Path,
        default=DEFAULT_LOCALES,
        help="Tree containing <lang>/LC_MESSAGES/*.po (default: repo locales/)",
    )
    args = parser.parse_args(argv)
    locales_dir = args.locales_dir
    if not locales_dir.is_dir():
        print("error: locales dir not found: %s" % locales_dir, file=sys.stderr)
        return 1
    written = compile_locales_tree(locales_dir)
    if not written:
        print("error: no .po files under %s" % locales_dir, file=sys.stderr)
        return 1
    print("compiled %s catalog(s)" % len(written))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
