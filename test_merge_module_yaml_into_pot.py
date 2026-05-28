# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Sanity checks for ``scripts/merge_module_yaml_into_pot.py`` path discovery.

Wrong root (e.g. ``plugin/modules/``) yields zero YAML files and drops ~50 settings
strings from the POT; see ``_walk_module_yamls`` implementation.
"""

from __future__ import annotations

import os

from scripts.merge_module_yaml_into_pot import _repo_root, _walk_module_yamls


def test_walk_module_yamls_finds_packaged_modules() -> None:
    root = _repo_root()
    plugin_root = os.path.join(root, "plugin")
    paths = _walk_module_yamls(plugin_root)
    basenames = {os.path.basename(os.path.dirname(p)) for p in paths}
    assert "chatbot" in basenames and "framework" in basenames
    assert len(paths) >= 8
