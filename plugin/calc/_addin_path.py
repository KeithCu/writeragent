# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Ensure extension paths are on sys.path before plugin imports (Calc UNO components)."""

from __future__ import annotations

import os
import sys


def ensure_addin_paths() -> None:
    """Insert extension root and plugin/ on sys.path (idempotent)."""
    calc_dir = os.path.dirname(os.path.abspath(__file__))
    plugin_dir = os.path.dirname(calc_dir)
    ext_root = os.path.dirname(plugin_dir)
    for path in (ext_root, plugin_dir, calc_dir):
        if path not in sys.path:
            sys.path.insert(0, path)
