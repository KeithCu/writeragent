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


def test_collect_strings_includes_button_text(tmp_path) -> None:
    """Settings button captions live in button_text, not label."""
    from scripts.merge_module_yaml_into_pot import _collect_strings_from_module_yaml

    yaml_path = tmp_path / "module.yaml"
    yaml_path.write_text(
        "name: demo\n"
        "config:\n"
        "  go:\n"
        "    widget: button\n"
        "    label: Do the thing\n"
        "    button_text: Recheck\n",
        encoding="utf-8",
    )
    got = _collect_strings_from_module_yaml(str(yaml_path))
    assert "Recheck" in got
    assert "Do the thing" in got


def test_walk_module_yamls_finds_packaged_modules() -> None:
    root = _repo_root()
    plugin_root = os.path.join(root, "plugin")
    paths = _walk_module_yamls(plugin_root)
    basenames = {os.path.basename(os.path.dirname(p)) for p in paths}
    assert "chatbot" in basenames and "framework" in basenames
    assert len(paths) >= 8
