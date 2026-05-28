# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for plugin.calc.calc_utils — merged cell geometry and sheet resolution."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from plugin.calc.calc_utils import get_cell_geometry, get_cell_geometry_target, resolve_sheet


class TestGetCellGeometry:
    """get_cell_geometry should collapse merged areas before reading Position/Size."""

    def test_unmerged_cell_returns_own_geometry(self):
        cell = SimpleNamespace(IsMerged=False, Position=(0, 0), Size=(100, 50))
        sheet = MagicMock()
        pos, size = get_cell_geometry(sheet, cell)
        assert pos == (0, 0)
        assert size == (100, 50)
        sheet.createCursorByRange.assert_not_called()

    def test_merged_cell_returns_collapsed_geometry(self):
        cell = SimpleNamespace(IsMerged=True, Position=(0, 0), Size=(100, 50))
        cursor = MagicMock()
        cursor.Position = (0, 0)
        cursor.Size = (300, 100)
        sheet = MagicMock()
        sheet.createCursorByRange.return_value = cursor

        pos, size = get_cell_geometry(sheet, cell)

        sheet.createCursorByRange.assert_called_once_with(cell)
        cursor.collapseToMergedArea.assert_called_once()
        assert pos == (0, 0)
        assert size == (300, 100)

    def test_merged_cell_geometry_target_is_collapsed_cursor(self):
        cell = SimpleNamespace(IsMerged=True, Position=(0, 0), Size=(100, 50))
        cursor = MagicMock()
        sheet = MagicMock()
        sheet.createCursorByRange.return_value = cursor

        target = get_cell_geometry_target(sheet, cell)

        assert target is cursor
        sheet.createCursorByRange.assert_called_once_with(cell)
        cursor.collapseToMergedArea.assert_called_once()

    def test_merged_cell_falls_back_on_exception(self):
        cell = SimpleNamespace(IsMerged=True, Position=(5, 10), Size=(80, 40))
        sheet = MagicMock()
        sheet.createCursorByRange.side_effect = RuntimeError("UNO error")

        pos, size = get_cell_geometry(sheet, cell)
        assert pos == (5, 10)
        assert size == (80, 40)

    def test_cell_without_ismerged_attribute(self):
        cell = SimpleNamespace(Position=(1, 2), Size=(10, 20))
        sheet = MagicMock()
        pos, size = get_cell_geometry(sheet, cell)
        assert pos == (1, 2)
        assert size == (10, 20)

    def test_geometry_target_falls_back_to_cell_when_cursor_fails(self):
        cell = SimpleNamespace(IsMerged=True, Position=(5, 6), Size=(70, 30))
        sheet = MagicMock()
        sheet.createCursorByRange.side_effect = RuntimeError("UNO error")

        target = get_cell_geometry_target(sheet, cell)

        assert target is cell
