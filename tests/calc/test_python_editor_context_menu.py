# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for Calc cell context menu detection helpers."""

from __future__ import annotations

from unittest.mock import MagicMock

from plugin.calc.python_editor_context_menu import _looks_like_cell_context_menu


def test_looks_like_cell_context_menu_matches_cut():
    first = MagicMock()
    first.getPropertyValue.return_value = ".uno:Cut"
    container = MagicMock()
    container.getCount.return_value = 1
    container.getByIndex.return_value = first
    assert _looks_like_cell_context_menu(container) is True


def test_looks_like_cell_context_menu_rejects_other_menus():
    first = MagicMock()
    first.getPropertyValue.return_value = ".uno:Insert"
    container = MagicMock()
    container.getCount.return_value = 1
    container.getByIndex.return_value = first
    assert _looks_like_cell_context_menu(container) is False
