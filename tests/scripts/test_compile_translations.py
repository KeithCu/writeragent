# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for scripts/compile_translations.py (polib .mo writer, no msgfmt)."""

from __future__ import annotations

from pathlib import Path

from scripts.compile_translations import compile_locales_tree, compile_po


_MINI_PO = """msgid ""
msgstr ""
"Content-Type: text/plain; charset=UTF-8\\n"

msgid "Built-in"
msgstr "Integriert"
"""


def test_compile_po_writes_mo_next_to_po(tmp_path: Path) -> None:
    po = tmp_path / "writeragent.po"
    po.write_text(_MINI_PO, encoding="utf-8")
    mo = compile_po(po)
    assert mo == tmp_path / "writeragent.mo"
    assert mo.is_file()
    assert mo.stat().st_size > 0


def test_compile_locales_tree_walks_lc_messages(tmp_path: Path) -> None:
    po = tmp_path / "de" / "LC_MESSAGES" / "writeragent.po"
    po.parent.mkdir(parents=True)
    po.write_text(_MINI_PO, encoding="utf-8")
    written = compile_locales_tree(tmp_path)
    assert [p.name for p in written] == ["writeragent.mo"]
    assert written[0].is_file()
