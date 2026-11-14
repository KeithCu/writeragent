# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Unit tests for Calc =PY() deferred spill formatting and neighbour classification."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from plugin.calc.python.spill import perform_deferred_spill
from plugin.calc.python.workbook_state import WorkbookState
from plugin.framework.queue_executor import main
from tests.doc_stubs import CalcDocStub


def test_perform_deferred_spill_applies_standard_keys_to_temporal_neighbours(monkeypatch: pytest.MonkeyPatch) -> None:
    formats = MagicMock()
    formats.getStandardFormat.side_effect = lambda fmt_type, _loc: {2: 37, 4: 63, 6: 70}[fmt_type]
    formats.getFormatIndex.return_value = 64
    formats.getByKey.side_effect = lambda key: SimpleNamespace(
        getPropertyValue=lambda prop: 2 if key == 40 and prop == "Type" else 16
    )

    doc = CalcDocStub(
        url="file:///spill_fmt.ods",
        props={"RuntimeUID": "uid-spill-fmt", "CharLocale": SimpleNamespace(Language="en", Country="US", Variant="")},
        number_formats=formats,
    )
    sheet = doc.getSheets().getByName("Sheet1")
    code = "grid"
    sheet.getCellByPosition(0, 0).setFormula(f'=PY("{code}")')
    # A3 (col 0, row 2) already carries a date NumberFormat key (40), which should be preserved.
    sheet.getCellByPosition(0, 2).setPropertyValue("NumberFormat", 40)

    monkeypatch.setattr(main, "is_current", lambda: True)
    state = WorkbookState.for_doc(doc)
    assert state is not None
    try:
        grid = [
            ["2026-08-08", "PT30H"],
            ["x", "2026-08-08T12:00:00"],
            ["2026-08-08", 5],
        ]
        perform_deferred_spill(
            "Sheet1",
            0,
            0,
            grid,
            doc=doc,
            sheet=sheet,
            code=code,
            key=state.key,
        )

        # B1 (col 1, row 0): duration 1.25 with elapsed key 64
        b1 = sheet.getCellByPosition(1, 0)
        assert b1.getValue() == pytest.approx(1.25)
        assert b1.getPropertyValue("NumberFormat") == 64

        # A2 (col 0, row 1): text "x" with NumberFormat 0
        a2 = sheet.getCellByPosition(0, 1)
        assert a2.getString() == "x"
        assert a2.getPropertyValue("NumberFormat") == 0

        # B2 (col 1, row 1): datetime 46242.5 with datetime key 70
        b2 = sheet.getCellByPosition(1, 1)
        assert b2.getValue() == pytest.approx(46242.5)
        assert b2.getPropertyValue("NumberFormat") == 70

        # A3 (col 0, row 2): date 46242.0 preserves existing date key 40
        a3 = sheet.getCellByPosition(0, 2)
        assert a3.getValue() == pytest.approx(46242.0)
        assert a3.getPropertyValue("NumberFormat") == 40

        # B3 (col 1, row 2): plain number 5.0 with NumberFormat 0
        b3 = sheet.getCellByPosition(1, 2)
        assert b3.getValue() == pytest.approx(5.0)
        assert b3.getPropertyValue("NumberFormat") == 0

        rec = state.spills[("Sheet1", 0, 0)]
        assert [(c.kind, c.value) for c in rec.cells] == [
            ("n", 1.25),
            ("s", "x"),
            ("n", 46242.5),
            ("n", 46242.0),
            ("n", 5.0),
        ]
    finally:
        WorkbookState.forget_key(state.key)


def test_perform_deferred_spill_error_and_empty_neighbours_record_kinds(monkeypatch: pytest.MonkeyPatch) -> None:
    doc = CalcDocStub(
        url="file:///spill_err.ods",
        props={"RuntimeUID": "uid-spill-err"},
    )
    sheet = doc.getSheets().getByName("Sheet1")
    code = "errs"
    sheet.getCellByPosition(0, 0).setFormula(f'=PY("{code}")')

    monkeypatch.setattr(main, "is_current", lambda: True)
    state = WorkbookState.for_doc(doc)
    assert state is not None
    try:
        grid = [
            [1, "#N/A"],
            ["#VALUE!", ""],
        ]
        perform_deferred_spill(
            "Sheet1",
            0,
            0,
            grid,
            doc=doc,
            sheet=sheet,
            code=code,
            key=state.key,
        )

        # B1 (#N/A): no =#ERR formula written
        assert sheet.getCellByPosition(1, 0).getFormula() == ""
        # A2 (#VALUE!): =#ERR519! formula written
        assert sheet.getCellByPosition(0, 1).getFormula() == "=#ERR519!"

        rec = state.spills[("Sheet1", 0, 0)]
        assert [(c.kind, c.value) for c in rec.cells] == [
            ("err", 32767),
            ("err", 519),
            ("e", None),
        ]
    finally:
        WorkbookState.forget_key(state.key)
