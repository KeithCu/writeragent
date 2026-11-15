# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for scripts/crosshair_stream.py target discovery."""

from __future__ import annotations

from pathlib import Path

from scripts.crosshair_stream import cover_fqns_for_module, discover_deal_plugin_files


def test_discover_skips_modules_that_mention_deal_only_in_comments(tmp_path: Path) -> None:
    root = tmp_path / "plugin"
    root.mkdir()
    (root / "mentions.py").write_text(
        '"""Docstring names @deal.pre."""\n\n\ndef f() -> int:\n    # fights the @deal.ensure rule\n    return 1\n',
        encoding="utf-8",
    )
    (root / "decorated.py").write_text(
        "from plugin.framework.deal_shim import deal\n\n\n"
        "@deal.post(lambda result: result == 1)\ndef f() -> int:\n    return 1\n",
        encoding="utf-8",
    )
    assert discover_deal_plugin_files(root) == [root / "decorated.py"]


def test_require_deal_keeps_only_deal_rooted_decorators(tmp_path: Path) -> None:
    root = tmp_path / "plugin"
    root.mkdir()
    path = root / "mod.py"
    path.write_text(
        "import functools\nfrom plugin.framework.deal_shim import deal\n\n\n"
        "def _deal_ok(x):\n    return True\n\n\n"
        "@functools.wraps(_deal_ok)\ndef wrapped(x):\n    return x\n\n\n"
        "@deal.pre(_deal_ok)\ndef checked(x):\n    return x\n\n\n"
        "def off(x):\n    # crosshair: off\n    return x\n",
        encoding="utf-8",
    )
    assert cover_fqns_for_module(path, require_deal=True) == ["plugin.mod.checked"]
    assert cover_fqns_for_module(path) == ["plugin.mod.wrapped", "plugin.mod.checked"]
