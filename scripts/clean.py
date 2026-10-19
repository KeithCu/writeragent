#!/usr/bin/env python3
# WriterAgent — remove build/, __pycache__ and .pyc files
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Remove build artifacts, __pycache__ directories, and loose .pyc files."""

from __future__ import annotations

import os
from pathlib import Path
import shutil
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def clean(root: Path | str = PROJECT_ROOT) -> None:
    """Remove __pycache__ dirs, .pyc files, and build/ under root."""
    root_path = Path(root)
    pycache_dirs: list[str] = []
    pyc_files: list[str] = []

    # Walk without following symlinks to avoid modifying external trees like .venv or vendor.
    for dirpath, dirnames, filenames in os.walk(root_path, followlinks=False):
        for name in list(dirnames):
            if name == "__pycache__":
                pycache_dirs.append(os.path.join(dirpath, name))
                dirnames.remove(name)
        for name in filenames:
            if name.endswith(".pyc"):
                pyc_files.append(os.path.join(dirpath, name))

    for cache_dir in pycache_dirs:
        shutil.rmtree(cache_dir, ignore_errors=True)

    for pyc_path in pyc_files:
        try:
            os.unlink(pyc_path)
        except OSError:
            pass

    build_dir = root_path / "build"
    shutil.rmtree(build_dir, ignore_errors=True)


def main() -> int:
    clean(PROJECT_ROOT)
    return 0


if __name__ == "__main__":
    sys.exit(main())
