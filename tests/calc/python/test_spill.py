# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Unit tests for Calc =PY() deferred spill formatting and neighbour classification."""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest

import plugin.calc.python.spill as _spill
from plugin.calc.python.spill import perform_deferred_spill
from plugin.calc.python.workbook_state import WorkbookState
from plugin.framework.queue_executor import main
from plugin.tests.testing_utils import CalcDocStub
from tests.calc.python.calc_workbook_test_access import (
    SPILL_REGISTRY,
    ctx_with_doc as _ctx_with_doc,
    reset_calc_workbook_states,
)


def _seen_record(sheet: Any, row: int, col: int, code: str, rows: int, cols: int) -> _spill.SpillRecord:
    return _spill.SpillRecord(
        code=code,
        rows=rows,
        cols=cols,
        values=_spill._rect_values(sheet, row, col, rows, cols),
    )


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
        assert (rec.rows, rec.cols) == (3, 2)
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
        assert (rec.rows, rec.cols) == (2, 2)
        assert state.used_ends["Sheet1"] == (1, 0)
    finally:
        WorkbookState.forget_key(state.key)


def _fire_sheet_modify(ctx, doc, sheet, sheet_name="Sheet1"):
    from plugin.calc.python.sheet_modify import SheetModifyDispatcher, flush_sheet_modify_pass_for_tests

    st = WorkbookState.for_doc(doc)
    assert st is not None
    got = sheet.getName() if callable(getattr(sheet, "getName", None)) else None
    if not isinstance(got, str):
        sheet.getName = lambda: sheet_name  # MagicMock.getName() would rename spills
    SheetModifyDispatcher(ctx, doc, sheet_name).modified(SimpleNamespace(Source=sheet))
    flush_sheet_modify_pass_for_tests(ctx, doc, sheet, sheet_name=sheet_name)
    return st


def test_load_and_save_spill_registry(monkeypatch: pytest.MonkeyPatch) -> None:
    """Test that spill registry loads from old/new formats and saves rectangles to document properties."""
    from plugin.calc.python.spill import SpillRecord

    saved_payload = None

    def mock_get_prop(model, name, default=None):
        if name == "WriterAgentSpillRegistry":
            return saved_payload if saved_payload is not None else json.dumps({
                "Sheet1:1,1": {
                    "code": "orig_code",
                    "cells": [
                        {"r": 2, "c": 1, "t": "n", "v": 20.0},
                        {"r": 3, "c": 1, "t": "n", "v": 30.0},
                    ],
                },
                "Sheet1:0,0": {
                    "code": "nan_code",
                    "cells": [{"r": 1, "c": 0, "t": "n", "v": "nan"}],
                },
                "Sheet1:5,5": {
                    "code": "empty_code",
                    "cells": [],
                },
            })
        return default

    def mock_set_prop(model, name, value):
        nonlocal saved_payload
        if name == "WriterAgentSpillRegistry":
            saved_payload = value

    monkeypatch.setattr("plugin.calc.python.spill.read_document_property", mock_get_prop)
    monkeypatch.setattr("plugin.calc.python.spill.set_document_property", mock_set_prop)

    doc = CalcDocStub(url="file:///fake_doc.ods")

    reset_calc_workbook_states()

    st = WorkbookState.for_doc(doc)
    assert st is not None
    _spill.load_spill_registry_for_doc(doc, st)
    key = ("file:///fake_doc.ods", "Sheet1", 1, 1)
    assert key in SPILL_REGISTRY
    rec = SPILL_REGISTRY[key]
    assert (rec.rows, rec.cols) == (3, 1)
    assert rec.code == "orig_code"
    assert rec.values is None
    assert (SPILL_REGISTRY[("file:///fake_doc.ods", "Sheet1", 0, 0)].rows, SPILL_REGISTRY[("file:///fake_doc.ods", "Sheet1", 0, 0)].cols) == (2, 1)
    assert (SPILL_REGISTRY[("file:///fake_doc.ods", "Sheet1", 5, 5)].rows, SPILL_REGISTRY[("file:///fake_doc.ods", "Sheet1", 5, 5)].cols) == (1, 1)

    SPILL_REGISTRY[key] = SpillRecord(code="some_code", rows=3, cols=1)
    SPILL_REGISTRY.pop(("file:///fake_doc.ods", "Sheet1", 0, 0), None)
    SPILL_REGISTRY.pop(("file:///fake_doc.ods", "Sheet1", 5, 5), None)
    _spill.save_spill_registry_for_doc(doc, st)

    assert saved_payload is not None
    data = json.loads(saved_payload)
    assert data["Sheet1:1,1"] == {
        "code": "some_code",
        "rows": 3,
        "cols": 1,
    }

    # Reload the new rectangle format
    reset_calc_workbook_states()
    st = WorkbookState.for_doc(doc)
    assert st is not None
    _spill.load_spill_registry_for_doc(doc, st)
    rec2 = SPILL_REGISTRY[key]
    assert (rec2.rows, rec2.cols) == (3, 1)
    assert rec2.code == "some_code"


def test_untitled_docs_do_not_share_spill_registry(monkeypatch: pytest.MonkeyPatch) -> None:
    """Two unsaved books must not share spill rows (each has its own WorkbookState)."""
    from plugin.calc.python.spill import PreparedSpill, SpillRecord

    stored: dict[int, str] = {}

    def mock_get_prop(model, name, default=None):
        if name == "WriterAgentSpillRegistry":
            return stored.get(id(model), default)
        return default

    def mock_set_prop(model, name, value):
        if name == "WriterAgentSpillRegistry":
            stored[id(model)] = value

    monkeypatch.setattr("plugin.calc.python.spill.read_document_property", mock_get_prop)
    monkeypatch.setattr("plugin.calc.python.spill.set_document_property", mock_set_prop)

    doc1 = CalcDocStub(url="", props={"RuntimeUID": "uid-spill-1"}, selection="B2")
    doc2 = CalcDocStub(url="", props={"RuntimeUID": "uid-spill-2"}, selection="B2")
    sheet1 = doc1.getSheets().getByName("Sheet1")
    sheet2 = doc2.getSheets().getByName("Sheet1")
    sheet1.getCellByPosition(1, 1).setFormula('=PYTHON("untitled_spill_a")')
    sheet2.getCellByPosition(1, 1).setFormula('=PYTHON("untitled_spill_b")')
    stored[id(doc1)] = json.dumps({"Sheet1:1,1": {"code": "untitled_spill_a", "rows": 2, "cols": 1}})
    stored[id(doc2)] = json.dumps({"Sheet1:1,1": {"code": "untitled_spill_b", "rows": 9, "cols": 9}})

    reset_calc_workbook_states()
    reset_calc_workbook_states()
    from plugin.calc.python.formula_locator_cache import FormulaOrigin, locate_formula_cell_in_doc

    ctx1 = _ctx_with_doc(doc1)
    ctx2 = _ctx_with_doc(doc2)
    key1 = ("uid-spill-1", "Sheet1", 1, 1)
    key2 = ("uid-spill-2", "Sheet1", 1, 1)
    try:
        loc1 = locate_formula_cell_in_doc(doc1, "untitled_spill_a")
        loc2 = locate_formula_cell_in_doc(doc2, "untitled_spill_b")
        assert loc1 is not None and loc2 is not None
        st1 = WorkbookState.for_doc(doc1)
        st2 = WorkbookState.for_doc(doc2)
        assert st1 is not None and st2 is not None
        prepared1 = _spill.prepare_auto_spill(ctx1, doc1, st1, FormulaOrigin.from_located(loc1), "untitled_spill_a", [[1], [2]])
        prepared2 = _spill.prepare_auto_spill(ctx2, doc2, st2, FormulaOrigin.from_located(loc2), "untitled_spill_b", [[3], [4]])
        assert isinstance(prepared1, PreparedSpill)
        assert isinstance(prepared2, PreparedSpill)
        assert prepared1.key == "uid-spill-1"
        assert prepared1.sheet_name == "Sheet1"
        assert prepared1.formula_row == 1
        assert prepared1.formula_col == 1

        assert prepared2.key == "uid-spill-2"
        assert prepared2.sheet_name == "Sheet1"
        assert prepared2.formula_row == 1
        assert prepared2.formula_col == 1

        assert (SPILL_REGISTRY[key1].rows, SPILL_REGISTRY[key1].cols) == (2, 1)
        assert (SPILL_REGISTRY[key2].rows, SPILL_REGISTRY[key2].cols) == (9, 9)
        assert ("", "Sheet1", 1, 1) not in SPILL_REGISTRY

        assert WorkbookState.for_doc(doc1) is not None and WorkbookState.for_doc(doc1).loaded
        assert WorkbookState.for_doc(doc2) is not None and WorkbookState.for_doc(doc2).loaded

        SPILL_REGISTRY[key1] = SpillRecord(code="untitled_spill_a", rows=3, cols=1)
        _spill.save_spill_registry_for_doc(doc1, WorkbookState.for_doc(doc1))
        assert json.loads(stored[id(doc1)]) == {
            "Sheet1:1,1": {
                "code": "untitled_spill_a",
                "rows": 3,
                "cols": 1,
            }
        }
        assert json.loads(stored[id(doc2)]) == {
            "Sheet1:1,1": {
                "code": "untitled_spill_b",
                "rows": 9,
                "cols": 9,
            }
        }

        # Book 1's modify clears only book 1, even though both sheets are Sheet1.
        sheet1.getCellByPosition(1, 1).setFormula("")
        st1.used_ends["Sheet1"] = _spill._used_end(sheet1)
        _fire_sheet_modify(ctx1, doc1, sheet1)
        assert key1 not in SPILL_REGISTRY
        assert (SPILL_REGISTRY[key2].rows, SPILL_REGISTRY[key2].cols) == (9, 9)

        SPILL_REGISTRY[key1] = SpillRecord(code="untitled_spill_a", rows=2, cols=1)
        WorkbookState.forget_key("uid-spill-1")
        assert key1 not in SPILL_REGISTRY
        assert (SPILL_REGISTRY[key2].rows, SPILL_REGISTRY[key2].cols) == (9, 9)

        # A scheduled identity from book 1 must not write book 2.
        _spill.perform_deferred_spill(
            "Sheet1", 1, 1, [[8], [9]], doc=doc2, sheet=sheet2, code="untitled_spill_b", key="uid-spill-1"
        )
        assert sheet2.getCellByPosition(1, 2).getValue() in (0, 0.0)
        assert ("", "Sheet1", 1, 1) not in SPILL_REGISTRY

        # Matching lifecycle key spills this book.
        _spill.perform_deferred_spill(
            "Sheet1", 1, 1, [[3], [4]], doc=doc2, sheet=sheet2, code="untitled_spill_b", key="uid-spill-2"
        )
        assert sheet2.getCellByPosition(1, 2).getValue() == 4.0
        assert ("", "Sheet1", 1, 1) not in SPILL_REGISTRY
        assert ("uid-spill-2", "Sheet1", 1, 1) in SPILL_REGISTRY
    finally:
        reset_calc_workbook_states()
        reset_calc_workbook_states()


def test_calc_spill_modify_listener_cleanup(monkeypatch: pytest.MonkeyPatch) -> None:
    """Sheet modify pass cleans up spilled cells when formula is removed."""
    monkeypatch.setattr("plugin.framework.queue_executor.main.is_current", lambda: True)
    from plugin.tests.testing_utils import CalcSheetStub

    sheet = CalcSheetStub("Sheet1")
    doc = CalcDocStub(sheets=[sheet], url="file:///fake_cleanup.ods")

    saved = []
    monkeypatch.setattr(_spill, "save_spill_registry_for_doc", lambda d, st: saved.append(d))

    st = WorkbookState.for_doc(doc)
    assert st is not None
    key = (st.key, "Sheet1", 1, 1)
    SPILL_REGISTRY[key] = _spill.SpillRecord(code="some_code", rows=2, cols=1)

    cell_B2 = sheet.getCellByPosition(1, 1)
    cell_B3 = sheet.getCellByPosition(1, 2)
    cell_B2.setFormula('=PYTHON("some_code")')
    cell_B3.setValue(20.0)
    st.used_ends["Sheet1"] = _spill._used_end(sheet)

    _fire_sheet_modify(MagicMock(), doc, sheet)

    assert key in SPILL_REGISTRY
    assert cell_B3.getValue() == 20.0

    cell_B2.setFormula("")
    _fire_sheet_modify(MagicMock(), doc, sheet)
    st.flush_spill_registry()

    assert key not in SPILL_REGISTRY
    assert cell_B3.getType() == 0
    assert len(saved) == 1


def test_spill_orphan_cleanup_saves_sheet_owner_not_active_doc(monkeypatch: pytest.MonkeyPatch) -> None:
    """Orphan cleanup must undo/save the workbook that owns the sheet."""
    monkeypatch.setattr("plugin.framework.queue_executor.main.is_current", lambda: True)
    from plugin.calc.python.sheet_modify import SheetModifyDispatcher, flush_sheet_modify_pass_for_tests

    owner = CalcDocStub(url="file:///owner.ods", props={"RuntimeUID": "uid-owner"})
    active = CalcDocStub(url="file:///active.ods", props={"RuntimeUID": "uid-active"})
    sheet = owner.getSheets().getByName("Sheet1")
    sheet.getParent = lambda: owner  # type: ignore[method-assign]
    origin = sheet.getCellByPosition(1, 1)
    spilled = sheet.getCellByPosition(1, 2)
    origin.setFormula("")
    spilled.setValue(99)

    saved: list[Any] = []
    monkeypatch.setattr(_spill, "save_spill_registry_for_doc", lambda doc, st: saved.append(doc))

    st = WorkbookState.for_doc(owner)
    assert st is not None
    st.used_ends["Sheet1"] = _spill._used_end(sheet)
    key = (st.key, "Sheet1", 1, 1)
    SPILL_REGISTRY[key] = _seen_record(sheet, 1, 1, '=PY("test")', 2, 1)
    try:
        ctx = MagicMock()
        SheetModifyDispatcher(ctx, active, "Sheet1").modified(SimpleNamespace(Source=sheet))
        flush_sheet_modify_pass_for_tests(ctx, owner, sheet, sheet_name="Sheet1")
        st.flush_spill_registry()
        assert key not in SPILL_REGISTRY
        assert spilled.getValue() == 0
        assert saved == [owner]
    finally:
        SPILL_REGISTRY.pop(key, None)


def test_calc_spill_modify_listener_drops_without_clearing_when_formula_only_contains_py(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """=PYMT and other formulas that merely contain PY/PYTHON drop the record without clearing."""
    monkeypatch.setattr("plugin.framework.queue_executor.main.is_current", lambda: True)
    from plugin.tests.testing_utils import CalcSheetStub

    sheet = CalcSheetStub("Sheet1")
    doc = CalcDocStub(sheets=[sheet], url="file:///fake_pymt.ods")
    monkeypatch.setattr(_spill, "save_spill_registry_for_doc", lambda d, st: None)

    st = WorkbookState.for_doc(doc)
    assert st is not None
    key = (st.key, "Sheet1", 1, 1)
    SPILL_REGISTRY[key] = _spill.SpillRecord(code="keep", rows=2, cols=1)

    origin = sheet.getCellByPosition(1, 1)
    spilled = sheet.getCellByPosition(1, 2)
    origin.setFormula('=PY("keep")')
    spilled.setValue(20.0)
    st.used_ends["Sheet1"] = _spill._used_end(sheet)
    try:
        _fire_sheet_modify(MagicMock(), doc, sheet)
        assert key in SPILL_REGISTRY
        assert spilled.getValue() == 20.0

        origin.setFormula("=PYMT(0.05, 12, 1000)")
        _fire_sheet_modify(MagicMock(), doc, sheet)
        assert key not in SPILL_REGISTRY
        assert spilled.getValue() == 20.0
    finally:
        SPILL_REGISTRY.pop(key, None)


def _clear_spill_timer_registry() -> None:
    WorkbookState.reset_for_tests()


def test_off_main_spill_timer_registers_cached_workbook_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """Unload cancel must see off-main timers; registering them must not call UNO."""

    _clear_spill_timer_registry()
    doc = CalcDocStub(url="file:///spill-life.ods", props={"RuntimeUID": "uid-spill"})
    calls = {"n": 0}
    original = doc.getPropertyValue

    def _count(name):
        calls["n"] += 1
        return original(name)

    doc.getPropertyValue = _count
    monkeypatch.setattr("plugin.framework.queue_executor.main.is_current", lambda: True)
    assert WorkbookState.for_doc(doc).key == "uid-spill"
    seen = calls["n"]
    assert seen >= 1

    monkeypatch.setattr("plugin.framework.queue_executor.main.is_current", lambda: False)
    try:
        _spill.queue_off_main_auto_spill(MagicMock(), "code", [[1, 2]], doc)
        assert calls["n"] == seen
        state = WorkbookState.known(doc)
        assert state is not None
        assert "code:code" in state.debounce.pending_for_tests()
        state.clear()
        assert "code:code" not in state.debounce.pending_for_tests()

        # No document object: do not guess a workbook key.
        _spill.queue_off_main_auto_spill(MagicMock(), "code", [[1]], None)
    finally:
        _clear_spill_timer_registry()


def test_off_main_spill_timer_key_follows_each_workbook(monkeypatch: pytest.MonkeyPatch) -> None:
    """Each cached model keeps its own unload key; a missing model stays blank."""

    _clear_spill_timer_registry()
    doc_a = CalcDocStub(props={"RuntimeUID": "uid-a"})
    doc_b = CalcDocStub(props={"RuntimeUID": "uid-b"})
    monkeypatch.setattr("plugin.framework.queue_executor.main.is_current", lambda: True)
    assert WorkbookState.for_doc(doc_a).key == "uid-a"
    assert WorkbookState.for_doc(doc_b).key == "uid-b"
    monkeypatch.setattr("plugin.framework.queue_executor.main.is_current", lambda: False)
    try:
        _spill.queue_off_main_auto_spill(MagicMock(), "a", [[1]], doc_a)
        _spill.queue_off_main_auto_spill(MagicMock(), "b", [[1]], doc_b)
        _spill.queue_off_main_auto_spill(MagicMock(), "none", [[1]], None)
        state_a = WorkbookState.known(doc_a)
        state_b = WorkbookState.known(doc_b)
        assert state_a is not None and "code:a" in state_a.debounce.pending_for_tests()
        assert state_b is not None and "code:b" in state_b.debounce.pending_for_tests()
        state_a.clear()
        assert "code:a" not in state_a.debounce.pending_for_tests()
        assert "code:b" in state_b.debounce.pending_for_tests()
    finally:
        _clear_spill_timer_registry()


def test_perform_deferred_spill_aborts_when_formula_replaced(monkeypatch: pytest.MonkeyPatch) -> None:
    doc = CalcDocStub(url="file:///spill.ods")
    sheet = doc.getSheets().getByIndex(0)
    sheet.getCellByPosition(0, 0).setFormula("=SUM(A2)")
    monkeypatch.setattr("plugin.framework.queue_executor.main.is_current", lambda: True)
    state = WorkbookState.for_doc(doc)
    assert state is not None
    _spill.perform_deferred_spill(
        "Sheet1", 0, 0, [["x", "y"]], doc=doc, sheet=sheet, code="result = 1", key=state.key
    )
    b1 = sheet.getCellByPosition(1, 0)
    assert b1.getString() == ""
    assert b1.getValue() == 0
    assert ("Sheet1", 0, 0) not in state.spills


def test_off_main_spill_timer_does_not_register_empty_lkey() -> None:
    """Missing doc must not arm a debounce slot."""

    _clear_spill_timer_registry()
    try:
        _spill.queue_off_main_auto_spill(None, "code", [[1, 2]], None)
        assert WorkbookState.known(None) is None
    finally:
        _clear_spill_timer_registry()


def test_prepare_auto_spill_respects_reported_sheet_size() -> None:
    """Spills past 1024 columns succeed on a 16384-column sheet; overflowing the last column is #SPILL!."""
    from plugin.calc.python.formula_locator_cache import FormulaOrigin
    from plugin.tests.testing_utils import CalcSheetStub

    sheet = CalcSheetStub(name="Sheet1")
    doc = CalcDocStub(sheets=[sheet], url="file:///wide-fallback.ods", props={"RuntimeUID": "uid-wide-fallback"})
    ctx = MagicMock()
    grid = [[1, 2]]

    doc_id = "uid-wide-fallback"
    state = WorkbookState.for_doc(doc)
    assert state is not None
    try:
        # Crosses past 1024 columns (1020..1029) and near the 16384 boundary (16300..16301).
        for formula_col, wide_grid in ((1020, [list(range(1, 11))]), (16300, grid)):
            cell = sheet.getCellByPosition(formula_col, 0)
            prepared = _spill.prepare_auto_spill(ctx, doc, state, FormulaOrigin(sheet, cell, "Sheet1", 0, formula_col), "py_code", wide_grid)
            assert prepared != "#SPILL!"
            assert isinstance(prepared, _spill.PreparedSpill)
            assert prepared.key == doc_id
            assert prepared.sheet_name == "Sheet1"
            assert prepared.formula_row == 0
            assert prepared.formula_col == formula_col

        edge_cell = sheet.getCellByPosition(16383, 0)
        prepared_spill = _spill.prepare_auto_spill(ctx, doc, state, FormulaOrigin(sheet, edge_cell, "Sheet1", 0, 16383), "py_code", grid)
        assert prepared_spill == "#SPILL!"
        from plugin.calc.python.diagnostics import diagnostics_workbook_key, get_diagnostics_store

        entry = get_diagnostics_store().latest_for_code(diagnostics_workbook_key(doc), "py_code")
        assert entry is not None and entry.status == "error"
        assert "Sheet1.XFD1" in entry.message
    finally:
        for key in [k for k in SPILL_REGISTRY if k[0] == doc_id]:
            SPILL_REGISTRY.pop(key, None)


def test_prepare_auto_spill_records_why_it_is_blocked() -> None:
    from plugin.calc.python.diagnostics import diagnostics_workbook_key, get_diagnostics_store
    from plugin.calc.python.formula_locator_cache import FormulaOrigin
    from plugin.tests.testing_utils import CalcSheetStub

    doc = CalcDocStub(url="file:///spill-blocked-diag.ods", props={"RuntimeUID": "uid-spill-blocked-diag"})
    sheet = CalcSheetStub(name="Sheet1")
    origin_cell = sheet.getCellByPosition(0, 0)
    origin_cell.setFormula('=PY("code_spill")')
    sheet.getCellByPosition(1, 0).setString("blocker")

    state = WorkbookState.for_doc(doc)
    assert state is not None
    doc_id = "uid-spill-blocked-diag"
    try:
        res = _spill.prepare_auto_spill(
            MagicMock(),
            doc,
            state,
            FormulaOrigin(sheet, origin_cell, "Sheet1", 0, 0),
            "code_spill",
            [[1, 2], [3, 4]],
        )
        assert res == "#SPILL!"
        entry = get_diagnostics_store().latest_for_code(diagnostics_workbook_key(doc), "code_spill")
        assert entry is not None
        assert entry.status == "error"
        assert "Sheet1.A1:B2" in entry.message
        assert "B1" in entry.message
    finally:
        for key in [k for k in SPILL_REGISTRY if k[0] == doc_id]:
            SPILL_REGISTRY.pop(key, None)


def test_perform_deferred_spill_fallback_to_passed_sheet_when_getbyname_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """When doc.getSheets().getByName(sheet_name) raises, the passed sheet object receives the spill."""
    monkeypatch.setattr("plugin.framework.queue_executor.main.is_current", lambda: True)
    doc = CalcDocStub(url="file:///spill_rename.ods")

    def boom_get_by_name(name: str) -> None:
        raise KeyError(f"Sheet {name} not found")

    monkeypatch.setattr(doc.getSheets(), "getByName", boom_get_by_name)

    from plugin.tests.testing_utils import CalcSheetStub

    passed_sheet = CalcSheetStub(name="SheetRenamed")
    origin_cell = passed_sheet.getCellByPosition(0, 0)
    origin_cell.setFormula('=PY("result = [[1, 2], [3, 4]]")')

    grid = [[1, 2], [3, 4]]

    _spill.perform_deferred_spill(
        "Sheet1",
        0,
        0,
        grid,
        doc=doc,
        sheet=passed_sheet,
        code="result = [[1, 2], [3, 4]]",
        key=WorkbookState.for_doc(doc).key,
    )

    assert passed_sheet.getCellByPosition(1, 0).getValue() == 2.0
    assert passed_sheet.getCellByPosition(0, 1).getValue() == 3.0
    assert passed_sheet.getCellByPosition(1, 1).getValue() == 4.0


def test_spill_remap_when_block_moves_with_formula(monkeypatch: pytest.MonkeyPatch) -> None:
    """Post-row-insert state: formula at row 5, spilled values at rows 6-8, registry at row 4. modified() remaps key without clearing."""
    from plugin.tests.testing_utils import CalcSheetStub

    monkeypatch.setattr("plugin.framework.queue_executor.main.is_current", lambda: True)
    sheet = CalcSheetStub("Sheet1")
    doc = CalcDocStub(sheets=[sheet], url="file:///remap.ods")

    st = WorkbookState.for_doc(doc)
    assert st is not None

    code = "result = [1, 2, 3]"
    formula = f'=PY("{code}")'
    # Formula is now at (col 0, row 5)
    sheet.getCellByPosition(0, 5).setFormula(formula)
    # Values 1, 2, 3 at rows 6, 7, 8
    sheet.getCellByPosition(0, 6).setValue(1.0)
    sheet.getCellByPosition(0, 7).setValue(2.0)
    sheet.getCellByPosition(0, 8).setValue(3.0)

    # Old registry entry at row 4; pre-insert used_end was (7, 0)
    st.used_ends["Sheet1"] = (7, 0)
    old_key = (st.key, "Sheet1", 4, 0)
    SPILL_REGISTRY[old_key] = _spill.SpillRecord(code=code, rows=4, cols=1)

    _fire_sheet_modify(MagicMock(), doc, sheet)

    new_key = (st.key, "Sheet1", 5, 0)
    assert old_key not in SPILL_REGISTRY
    assert new_key in SPILL_REGISTRY
    rec = SPILL_REGISTRY[new_key]
    assert (rec.rows, rec.cols) == (4, 1)
    assert st.used_ends["Sheet1"] == (8, 0)
    assert sheet.getCellByPosition(0, 6).getValue() == 1.0
    assert sheet.getCellByPosition(0, 7).getValue() == 2.0
    assert sheet.getCellByPosition(0, 8).getValue() == 3.0


def test_spill_cut_paste_drops_record_and_leaves_block(monkeypatch: pytest.MonkeyPatch) -> None:
    """Formula cut-pasted to another column with used_end delta (0, 0) drops the record and leaves old values."""
    from plugin.tests.testing_utils import CalcSheetStub

    monkeypatch.setattr("plugin.framework.queue_executor.main.is_current", lambda: True)
    sheet = CalcSheetStub("Sheet1")
    doc = CalcDocStub(sheets=[sheet], url="file:///cutpaste.ods")

    st = WorkbookState.for_doc(doc)
    assert st is not None

    code = "result = [1, 2]"
    # Formula cut-pasted to col 3 row 4
    sheet.getCellByPosition(3, 4).setFormula(f'=PY("{code}")')
    # Old formula cell at col 0 row 4 is now empty
    sheet.getCellByPosition(0, 4).setFormula("")
    # Old spill values still at col 0 row 5
    sheet.getCellByPosition(0, 5).setValue(2.0)

    st.used_ends["Sheet1"] = _spill._used_end(sheet)
    old_key = (st.key, "Sheet1", 4, 0)
    SPILL_REGISTRY[old_key] = _seen_record(sheet, 4, 0, code, 2, 1)

    _fire_sheet_modify(MagicMock(), doc, sheet)

    assert old_key not in SPILL_REGISTRY
    assert (st.key, "Sheet1", 4, 3) not in SPILL_REGISTRY
    assert sheet.getCellByPosition(0, 5).getValue() == 2.0


@pytest.mark.parametrize("user_formula", ["=SUM(1)", "=1/0"])
def test_spill_clear_keeps_block_when_user_formula_inside(
    monkeypatch: pytest.MonkeyPatch, user_formula: str
) -> None:
    """A user formula inside the rectangle prevents clearing when the origin formula is deleted."""
    from plugin.tests.testing_utils import CalcSheetStub

    monkeypatch.setattr("plugin.framework.queue_executor.main.is_current", lambda: True)
    sheet = CalcSheetStub("Sheet1")
    doc = CalcDocStub(sheets=[sheet], url="file:///skip.ods")

    st = WorkbookState.for_doc(doc)
    assert st is not None

    code = "result = [1, 2, 3]"
    sheet.getCellByPosition(0, 0).setFormula(f'=PY("{code}")')
    sheet.getCellByPosition(0, 1).setFormula(user_formula)
    sheet.getCellByPosition(0, 2).setValue(999.0)
    sheet.getCellByPosition(0, 3).setValue(3.0)

    st.used_ends["Sheet1"] = _spill._used_end(sheet)
    key = (st.key, "Sheet1", 0, 0)
    SPILL_REGISTRY[key] = _spill.SpillRecord(code=code, rows=4, cols=1)
    _fire_sheet_modify(MagicMock(), doc, sheet)
    assert key in SPILL_REGISTRY

    sheet.getCellByPosition(0, 0).setFormula("")
    st.used_ends["Sheet1"] = _spill._used_end(sheet)
    _fire_sheet_modify(MagicMock(), doc, sheet)

    assert key not in SPILL_REGISTRY
    assert sheet.getCellByPosition(0, 1).getFormula() == user_formula
    assert sheet.getCellByPosition(0, 2).getValue() == 999.0
    assert sheet.getCellByPosition(0, 3).getValue() == 3.0


def test_perform_deferred_spill_writes_nan_column_as_err_block(monkeypatch: pytest.MonkeyPatch) -> None:
    from plugin.tests.testing_utils import CalcSheetStub

    monkeypatch.setattr("plugin.framework.queue_executor.main.is_current", lambda: True)
    monkeypatch.setattr("plugin.calc.python.spill.save_spill_registry_for_doc", lambda doc, state: None)

    sheet = CalcSheetStub("Sheet1")
    doc = CalcDocStub(sheets=[sheet], url="file:///nan_block.ods")
    sheet.getCellByPosition(0, 0).setFormula('=PY("result = [[1, float(\'nan\'), float(\'inf\')], [2, float(\'nan\'), float(\'-inf\')]]")')

    reset_calc_workbook_states()
    st = WorkbookState.for_doc(doc)
    assert st is not None
    try:
        perform_deferred_spill(
            "Sheet1",
            0,
            0,
            [[1.0, float("nan"), float("inf")], [2.0, float("nan"), float("-inf")]],
            doc=doc,
            sheet=sheet,
            code="result = [[1, float('nan'), float('inf')], [2, float('nan'), float('-inf')]]",
            key=st.key,
        )
        assert sheet.getCellByPosition(1, 0).getFormula() == "=#ERR503!"
        assert sheet.getCellByPosition(1, 1).getFormula() == "=#ERR503!"
        assert sheet.getCellByPosition(2, 0).getFormula() == "=#ERR503!"
        assert sheet.getCellByPosition(2, 1).getFormula() == "=#ERR503!"
        assert sheet.getCellByPosition(0, 1).getValue() == 2.0
        rec = SPILL_REGISTRY[(st.key, "Sheet1", 0, 0)]
        assert (rec.rows, rec.cols) == (2, 3)
    finally:
        reset_calc_workbook_states()


def test_load_spill_registry_skips_malformed_entry(monkeypatch: pytest.MonkeyPatch) -> None:
    payload = json.dumps({
        "Sheet1:1,1": [[2, 1]],
        "Sheet1:x,1": [[0, 0]],
        "Sheet1:4,4": {"code": "later", "cells": [{"r": 5, "c": 4, "t": "n", "v": 1.0}]},
    })
    monkeypatch.setattr(
        "plugin.calc.python.spill.read_document_property",
        lambda model, name, default=None: payload if name == "WriterAgentSpillRegistry" else default,
    )
    doc = CalcDocStub(url="file:///fake_malformed.ods")

    reset_calc_workbook_states()

    st = WorkbookState.for_doc(doc)
    assert st is not None
    _spill.load_spill_registry_for_doc(doc, st)
    doc_key = st.key
    assert (doc_key, "Sheet1", 1, 1) not in SPILL_REGISTRY
    assert (doc_key, "Sheet1", 4, 4) in SPILL_REGISTRY
    assert len([k for k in SPILL_REGISTRY if k[0] == doc_key]) == 1


def test_spill_registry_doc_key_uses_runtime_uid_ladder() -> None:
    class MethodVsPropStub:
        def getRuntimeUID(self) -> str:
            return "from-method"

        def getPropertyValue(self, name: str) -> str:
            if name == "RuntimeUID":
                return "from-prop"
            raise KeyError(name)

        def getURL(self) -> str:
            return ""

    assert WorkbookState.for_doc(MethodVsPropStub()).key == "from-method"

    int_stub = CalcDocStub(props={"RuntimeUID": 42})
    assert WorkbookState.for_doc(int_stub).key == "42"

    class NonStrIntObj:
        pass

    non_str_stub = CalcDocStub(url="file:///x.ods", props={"RuntimeUID": NonStrIntObj()})
    assert WorkbookState.for_doc(non_str_stub).key == "calc:file:///x.ods"


def test_spill_registry_doc_key_stable_without_runtime_uid(monkeypatch: pytest.MonkeyPatch) -> None:
    doc = CalcDocStub(url="", props={})
    counter = 0

    def sid(_doc: Any) -> str:
        nonlocal counter
        counter += 1
        return f"calc:unsaved:{counter}"

    monkeypatch.setattr("plugin.calc.python.workbook_state.calc_workbook_base_session_id", sid)

    key1 = ""
    try:
        st1 = WorkbookState.for_doc(doc)
        assert st1 is not None
        key1 = st1.key
        assert key1 == "calc:unsaved:1"
        st2 = WorkbookState.for_doc(doc)
        assert st2 is not None
        assert st2.key == "calc:unsaved:1"
        assert counter == 1
    finally:
        if key1:
            WorkbookState.forget_key(key1)


def test_perform_deferred_spill_aborts_on_late_collision(monkeypatch: pytest.MonkeyPatch) -> None:
    from plugin.calc.python.diagnostics import diagnostics_workbook_key, get_diagnostics_store
    from plugin.calc.python.formula_locator_cache import FormulaOrigin

    monkeypatch.setattr("plugin.framework.queue_executor.main.is_current", lambda: True)
    doc = CalcDocStub(url="file:///spill-late-collision.ods", props={"RuntimeUID": "uid-spill-late-collision"})
    sheet = doc.getSheets().getByIndex(0)
    origin_cell = sheet.getCellByPosition(0, 0)
    origin_cell.setFormula('=PY("code_late")')

    state = WorkbookState.for_doc(doc)
    assert state is not None
    doc_id = "uid-spill-late-collision"
    try:
        prep = _spill.prepare_auto_spill(
            MagicMock(),
            doc,
            state,
            FormulaOrigin(sheet, origin_cell, "Sheet1", 0, 0),
            "code_late",
            [[1, 2], [3, 4]],
        )
        assert isinstance(prep, _spill.PreparedSpill)

        # User fills B2 before the deferred write fires.
        sheet.getCellByPosition(1, 1).setValue(99)

        _spill.perform_deferred_spill(
            prep.sheet_name,
            prep.formula_row,
            prep.formula_col,
            [[1, 2], [3, 4]],
            doc=doc,
            sheet=sheet,
            code=prep.code,
            key=prep.key,
        )

        assert sheet.getCellByPosition(1, 1).getValue() == 99
        assert sheet.getCellByPosition(1, 0).getType() == _spill.EMPTY
        assert sheet.getCellByPosition(0, 1).getType() == _spill.EMPTY
        assert ("Sheet1", 0, 0) not in state.spills
        entry = get_diagnostics_store().latest_for_code(diagnostics_workbook_key(doc), "code_late")
        assert entry is not None
        assert entry.status == "error"
        assert "B2" in entry.message
    finally:
        for key in [k for k in SPILL_REGISTRY if k[0] == doc_id]:
            SPILL_REGISTRY.pop(key, None)
        WorkbookState.forget_key(doc_id)


def test_perform_deferred_spill_allows_owned_spill_overwrite(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("plugin.framework.queue_executor.main.is_current", lambda: True)
    doc = CalcDocStub(url="file:///spill-owned.ods", props={"RuntimeUID": "uid-spill-owned"})
    sheet = doc.getSheets().getByIndex(0)
    origin_cell = sheet.getCellByPosition(0, 0)
    origin_cell.setFormula('=PY("code_owned")')
    sheet.getCellByPosition(1, 0).setValue(10)

    state = WorkbookState.for_doc(doc)
    assert state is not None
    state.spills[("Sheet1", 0, 0)] = _spill.SpillRecord(
        code="code_owned",
        rows=1,
        cols=2,
    )
    doc_id = "uid-spill-owned"
    try:
        _spill.perform_deferred_spill(
            "Sheet1",
            0,
            0,
            [[1, 20]],
            doc=doc,
            sheet=sheet,
            code="code_owned",
            key=state.key,
        )
        assert sheet.getCellByPosition(1, 0).getValue() == 20
    finally:
        for key in [k for k in SPILL_REGISTRY if k[0] == doc_id]:
            SPILL_REGISTRY.pop(key, None)
        WorkbookState.forget_key(doc_id)


def test_perform_deferred_spill_late_collision_clears_old_spill(monkeypatch: pytest.MonkeyPatch) -> None:
    from plugin.calc.python.formula_locator_cache import FormulaOrigin

    monkeypatch.setattr("plugin.framework.queue_executor.main.is_current", lambda: True)
    doc = CalcDocStub(url="file:///spill-late-clear.ods", props={"RuntimeUID": "uid-spill-late-clear"})
    sheet = doc.getSheets().getByIndex(0)
    origin_cell = sheet.getCellByPosition(0, 0)
    origin_cell.setFormula('=PY("code_grow")')
    sheet.getCellByPosition(1, 0).setValue(10)

    state = WorkbookState.for_doc(doc)
    assert state is not None
    state.spills[("Sheet1", 0, 0)] = _spill.SpillRecord(
        code="code_grow",
        rows=1,
        cols=2,
    )
    doc_id = "uid-spill-late-clear"
    try:
        prep = _spill.prepare_auto_spill(
            MagicMock(),
            doc,
            state,
            FormulaOrigin(sheet, origin_cell, "Sheet1", 0, 0),
            "code_grow",
            [[1, 2], [3, 4]],
        )
        assert isinstance(prep, _spill.PreparedSpill)

        sheet.getCellByPosition(1, 1).setValue(99)

        _spill.perform_deferred_spill(
            prep.sheet_name,
            prep.formula_row,
            prep.formula_col,
            [[1, 2], [3, 4]],
            doc=doc,
            sheet=sheet,
            code=prep.code,
            key=prep.key,
        )

        assert sheet.getCellByPosition(1, 0).getType() == _spill.EMPTY
        assert sheet.getCellByPosition(1, 1).getValue() == 99
        rec = state.spills[("Sheet1", 0, 0)]
        assert (rec.rows, rec.cols) == (1, 1)
    finally:
        for key in [k for k in SPILL_REGISTRY if k[0] == doc_id]:
            SPILL_REGISTRY.pop(key, None)
        WorkbookState.forget_key(doc_id)


def test_prepare_auto_spill_blocked_clears_owned_cells_after_settle(monkeypatch: pytest.MonkeyPatch) -> None:
    from plugin.calc.python.formula_locator_cache import FormulaOrigin

    monkeypatch.setattr("plugin.framework.queue_executor.main.is_current", lambda: True)
    saved: list[str] = []

    def mock_set_prop(_model: Any, name: str, value: Any) -> None:
        if name == "WriterAgentSpillRegistry":
            saved.append(value)

    monkeypatch.setattr("plugin.calc.python.spill.set_document_property", mock_set_prop)

    doc = CalcDocStub(url="file:///spill-prep-clear.ods", props={"RuntimeUID": "uid-spill-prep-clear"})
    sheet = doc.getSheets().getByIndex(0)
    origin_cell = sheet.getCellByPosition(0, 0)
    origin_cell.setFormula('=PY("c")')
    sheet.getCellByPosition(0, 1).setValue(10)
    sheet.getCellByPosition(0, 2).setValue(5)

    state = WorkbookState.for_doc(doc)
    assert state is not None
    state.spills[("Sheet1", 0, 0)] = _spill.SpillRecord(code="c", rows=2, cols=1)
    doc_id = "uid-spill-prep-clear"
    try:
        res = _spill.prepare_auto_spill(
            MagicMock(),
            doc,
            state,
            FormulaOrigin(sheet, origin_cell, "Sheet1", 0, 0),
            "c",
            [[1], [2], [3]],
        )
        assert res == "#SPILL!"
        # Nothing is written during formula interpretation.
        assert sheet.getCellByPosition(0, 1).getValue() == 10

        state.debounce.flush_for_tests("spill:Sheet1:0:0")
        state.flush_spill_registry()

        assert sheet.getCellByPosition(0, 1).getType() == _spill.EMPTY
        assert sheet.getCellByPosition(0, 2).getValue() == 5
        rec = state.spills[("Sheet1", 0, 0)]
        assert (rec.rows, rec.cols) == (1, 1)
        assert saved
        assert json.loads(saved[-1])["Sheet1:0,0"] == {"code": "c", "rows": 1, "cols": 1}
    finally:
        for key in [k for k in SPILL_REGISTRY if k[0] == doc_id]:
            SPILL_REGISTRY.pop(key, None)
        WorkbookState.forget_key(doc_id)


def test_blocked_spill_skips_clear_when_record_belongs_to_different_formula(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from plugin.calc.python.formula_locator_cache import FormulaOrigin

    monkeypatch.setattr("plugin.framework.queue_executor.main.is_current", lambda: True)
    doc = CalcDocStub(url="file:///spill-mismatch-rec.ods", props={"RuntimeUID": "uid-spill-mismatch"})
    sheet = doc.getSheets().getByIndex(0)
    origin_cell = sheet.getCellByPosition(0, 0)
    origin_cell.setFormula('=PY("q")')
    sheet.getCellByPosition(0, 1).setValue(10)
    sheet.getCellByPosition(0, 2).setValue(20)
    sheet.getCellByPosition(0, 3).setValue(99)

    state = WorkbookState.for_doc(doc)
    assert state is not None
    state.spills[("Sheet1", 0, 0)] = _spill.SpillRecord(code="p", rows=3, cols=1)
    doc_id = "uid-spill-mismatch"
    try:
        res = _spill.prepare_auto_spill(
            MagicMock(),
            doc,
            state,
            FormulaOrigin(sheet, origin_cell, "Sheet1", 0, 0),
            "q",
            [[1], [2], [3], [4]],
        )
        assert res == "#SPILL!"
        state.debounce.flush_for_tests("spill:Sheet1:0:0")

        assert sheet.getCellByPosition(0, 1).getValue() == 10
        assert sheet.getCellByPosition(0, 2).getValue() == 20
        rec = state.spills[("Sheet1", 0, 0)]
        assert rec.code == "p"
        assert (rec.rows, rec.cols) == (3, 1)
    finally:
        for key in [k for k in SPILL_REGISTRY if k[0] == doc_id]:
            SPILL_REGISTRY.pop(key, None)
        WorkbookState.forget_key(doc_id)


def test_off_main_fitting_spill_cancels_pending_release(monkeypatch: pytest.MonkeyPatch) -> None:
    from plugin.calc.python.formula_locator_cache import FormulaOrigin

    monkeypatch.setattr("plugin.framework.queue_executor.main.is_current", lambda: True)
    doc = CalcDocStub(url="file:///spill-offmain-cancel.ods", props={"RuntimeUID": "uid-offmain-cancel"})
    sheet = doc.getSheets().getByIndex(0)
    origin_cell = sheet.getCellByPosition(0, 0)
    origin_cell.setFormula('=PY("c_fit")')
    sheet.getCellByPosition(0, 1).setValue(10)
    sheet.getCellByPosition(0, 2).setValue(99)

    state = WorkbookState.for_doc(doc)
    assert state is not None
    state.spills[("Sheet1", 0, 0)] = _spill.SpillRecord(code="c_fit", rows=2, cols=1)
    doc_id = "uid-offmain-cancel"
    try:
        res = _spill.prepare_auto_spill(
            MagicMock(),
            doc,
            state,
            FormulaOrigin(sheet, origin_cell, "Sheet1", 0, 0),
            "c_fit",
            [[1], [2], [3]],
        )
        assert res == "#SPILL!"
        assert "spill:Sheet1:0:0" in state.debounce.pending_for_tests()

        # Blocker is removed and an off-main recalculation fits and writes.
        sheet.getCellByPosition(0, 2).clearContents()
        _spill.queue_off_main_auto_spill(MagicMock(), "c_fit", [[1], [20], [30]], doc)
        state.debounce.flush_for_tests("code:c_fit")
        assert "spill:Sheet1:0:0" not in state.debounce.pending_for_tests()

        # Flushing the token must not wipe the fresh spill.
        state.debounce.flush_for_tests("spill:Sheet1:0:0")
        assert sheet.getCellByPosition(0, 1).getValue() == 20
        assert sheet.getCellByPosition(0, 2).getValue() == 30
        rec = state.spills[("Sheet1", 0, 0)]
        assert (rec.rows, rec.cols) == (3, 1)
    finally:
        for key in [k for k in SPILL_REGISTRY if k[0] == doc_id]:
            SPILL_REGISTRY.pop(key, None)
        WorkbookState.forget_key(doc_id)


def test_perform_deferred_spill_overwrites_user_value_in_rect(monkeypatch: pytest.MonkeyPatch) -> None:
    from plugin.calc.python.formula_locator_cache import FormulaOrigin

    monkeypatch.setattr("plugin.framework.queue_executor.main.is_current", lambda: True)
    doc = CalcDocStub(url="file:///spill-user-edit.ods", props={"RuntimeUID": "uid-spill-user-edit"})
    sheet = doc.getSheets().getByIndex(0)
    origin_cell = sheet.getCellByPosition(0, 0)
    origin_cell.setFormula('=PY("code_user_edit")')
    sheet.getCellByPosition(1, 0).setValue(99)

    state = WorkbookState.for_doc(doc)
    assert state is not None
    state.spills[("Sheet1", 0, 0)] = _spill.SpillRecord(code="code_user_edit", rows=1, cols=2)
    doc_id = "uid-spill-user-edit"
    try:
        prep = _spill.prepare_auto_spill(
            MagicMock(),
            doc,
            state,
            FormulaOrigin(sheet, origin_cell, "Sheet1", 0, 0),
            "code_user_edit",
            [[1, 20]],
        )
        assert isinstance(prep, _spill.PreparedSpill)

        _spill.perform_deferred_spill(
            "Sheet1",
            0,
            0,
            [[1, 20]],
            doc=doc,
            sheet=sheet,
            code="code_user_edit",
            key=state.key,
        )
        assert sheet.getCellByPosition(1, 0).getValue() == 20
    finally:
        for key in [k for k in SPILL_REGISTRY if k[0] == doc_id]:
            SPILL_REGISTRY.pop(key, None)
        WorkbookState.forget_key(doc_id)


def test_perform_deferred_spill_shrink_clears_old_rect(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("plugin.framework.queue_executor.main.is_current", lambda: True)
    monkeypatch.setattr(_spill, "save_spill_registry_for_doc", lambda _d, _st: None)
    doc = CalcDocStub(url="file:///spill-shrink-user.ods", props={"RuntimeUID": "uid-spill-shrink-user"})
    sheet = doc.getSheets().getByIndex(0)
    sheet.getCellByPosition(0, 0).setFormula('=PY("code_shrink")')
    sheet.getCellByPosition(2, 0).setString("user")
    sheet.getCellByPosition(3, 0).setValue(30)

    state = WorkbookState.for_doc(doc)
    assert state is not None
    state.spills[("Sheet1", 0, 0)] = _spill.SpillRecord(code="code_shrink", rows=1, cols=4)
    doc_id = "uid-spill-shrink-user"
    try:
        _spill.perform_deferred_spill(
            "Sheet1",
            0,
            0,
            [[1, 99]],
            doc=doc,
            sheet=sheet,
            code="code_shrink",
            key=state.key,
        )
        assert sheet.getCellByPosition(1, 0).getValue() == 99
        assert sheet.getCellByPosition(2, 0).getType() == 0
        assert sheet.getCellByPosition(3, 0).getType() == 0
        rec = state.spills[("Sheet1", 0, 0)]
        assert (rec.rows, rec.cols) == (1, 2)
    finally:
        for key in [k for k in SPILL_REGISTRY if k[0] == doc_id]:
            SPILL_REGISTRY.pop(key, None)
        WorkbookState.forget_key(doc_id)


def test_spill_move_not_confirmed_when_used_area_delta_differs(monkeypatch: pytest.MonkeyPatch) -> None:
    from plugin.tests.testing_utils import CalcSheetStub

    monkeypatch.setattr("plugin.framework.queue_executor.main.is_current", lambda: True)
    sheet = CalcSheetStub("Sheet1")
    doc = CalcDocStub(sheets=[sheet], url="file:///delta-mismatch.ods")
    st = WorkbookState.for_doc(doc)
    assert st is not None

    code = "result = [1, 2]"
    sheet.getCellByPosition(0, 5).setFormula(f'=PY("{code}")')
    sheet.getCellByPosition(0, 6).setValue(1.0)
    sheet.getCellByPosition(0, 7).setValue(2.0)
    # Formula shifted by +1 row (4 -> 5), but used_end delta is +3 (4 -> 7).
    st.used_ends["Sheet1"] = (4, 0)
    old_key = (st.key, "Sheet1", 4, 0)
    SPILL_REGISTRY[old_key] = _seen_record(sheet, 4, 0, code, 3, 1)

    _fire_sheet_modify(MagicMock(), doc, sheet)

    assert old_key not in SPILL_REGISTRY
    assert (st.key, "Sheet1", 5, 0) not in SPILL_REGISTRY
    assert sheet.getCellByPosition(0, 6).getValue() == 1.0
    assert sheet.getCellByPosition(0, 7).getValue() == 2.0


def test_spill_row_delete_through_formula_clears_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    from plugin.tests.testing_utils import CalcSheetStub

    monkeypatch.setattr("plugin.framework.queue_executor.main.is_current", lambda: True)
    sheet = CalcSheetStub("Sheet1")
    doc = CalcDocStub(sheets=[sheet], url="file:///row-del-formula.ods")
    st = WorkbookState.for_doc(doc)
    assert st is not None

    sheet.getCellByPosition(0, 4).setValue(1.0)
    sheet.getCellByPosition(0, 5).setValue(2.0)
    # Used end shrank by 1 row (6 -> 5) and formula is gone.
    st.used_ends["Sheet1"] = (6, 0)
    key = (st.key, "Sheet1", 4, 0)
    SPILL_REGISTRY[key] = _seen_record(sheet, 4, 0, "result = [1, 2]", 3, 1)

    _fire_sheet_modify(MagicMock(), doc, sheet)

    assert key not in SPILL_REGISTRY
    assert sheet.getCellByPosition(0, 4).getValue() == 1.0
    assert sheet.getCellByPosition(0, 5).getValue() == 2.0


def test_spill_sort_moves_formula_without_clearing(monkeypatch: pytest.MonkeyPatch) -> None:
    from plugin.tests.testing_utils import CalcSheetStub

    monkeypatch.setattr("plugin.framework.queue_executor.main.is_current", lambda: True)
    sheet = CalcSheetStub("Sheet1")
    doc = CalcDocStub(sheets=[sheet], url="file:///sort-move.ods")
    st = WorkbookState.for_doc(doc)
    assert st is not None

    code = "result = [1, 2]"
    sheet.getCellByPosition(0, 2).setValue(10.0)
    sheet.getCellByPosition(0, 3).setValue(20.0)
    sheet.getCellByPosition(0, 4).setFormula(f'=PY("{code}")')
    st.used_ends["Sheet1"] = (4, 0)
    key = (st.key, "Sheet1", 1, 0)
    SPILL_REGISTRY[key] = _seen_record(sheet, 1, 0, code, 3, 1)

    _fire_sheet_modify(MagicMock(), doc, sheet)

    assert key not in SPILL_REGISTRY
    assert (st.key, "Sheet1", 4, 0) not in SPILL_REGISTRY
    assert sheet.getCellByPosition(0, 2).getValue() == 10.0
    assert sheet.getCellByPosition(0, 3).getValue() == 20.0


def test_prepare_auto_spill_blocks_on_user_formula_in_rect(monkeypatch: pytest.MonkeyPatch) -> None:
    from plugin.calc.python.diagnostics import diagnostics_workbook_key, get_diagnostics_store
    from plugin.calc.python.formula_locator_cache import FormulaOrigin

    monkeypatch.setattr("plugin.framework.queue_executor.main.is_current", lambda: True)
    doc = CalcDocStub(url="file:///user-formula-block.ods", props={"RuntimeUID": "uid-uf-block"})
    sheet = doc.getSheets().getByIndex(0)
    origin_cell = sheet.getCellByPosition(0, 0)
    origin_cell.setFormula('=PY("uf_code")')
    sheet.getCellByPosition(1, 0).setFormula("=SUM(1)")
    sheet.getCellByPosition(0, 1).setValue(42)

    st = WorkbookState.for_doc(doc)
    assert st is not None
    st.spills[("Sheet1", 0, 0)] = _spill.SpillRecord(code="uf_code", rows=2, cols=2)
    try:
        res = _spill.prepare_auto_spill(
            MagicMock(),
            doc,
            st,
            FormulaOrigin(sheet, origin_cell, "Sheet1", 0, 0),
            "uf_code",
            [[1, 2], [3, 4]],
        )
        assert res == "#SPILL!"
        entry = get_diagnostics_store().latest_for_code(diagnostics_workbook_key(doc), "uf_code")
        assert entry is not None and "B1" in entry.message and "previous spill" in entry.message

        st.debounce.flush_for_tests("spill:Sheet1:0:0")
        assert sheet.getCellByPosition(1, 0).getFormula() == "=SUM(1)"
        assert sheet.getCellByPosition(0, 1).getValue() == 42
        assert (st.spills[("Sheet1", 0, 0)].rows, st.spills[("Sheet1", 0, 0)].cols) == (2, 2)

        # User error formula =1/0 also blocks (text-only guard).
        sheet.getCellByPosition(1, 0).setFormula("=1/0")
        assert _spill.prepare_auto_spill(
            MagicMock(),
            doc,
            st,
            FormulaOrigin(sheet, origin_cell, "Sheet1", 0, 0),
            "uf_code",
            [[1, 2]],
        ) == "#SPILL!"

        # Error literal in B1 does not block.
        sheet.getCellByPosition(1, 0).setFormula("=#ERR519!")
        res2 = _spill.prepare_auto_spill(
            MagicMock(),
            doc,
            st,
            FormulaOrigin(sheet, origin_cell, "Sheet1", 0, 0),
            "uf_code",
            [[1, 2]],
        )
        assert isinstance(res2, _spill.PreparedSpill)
    finally:
        WorkbookState.forget_key("uid-uf-block")


def test_cleanup_refreshes_used_end_after_pass(monkeypatch: pytest.MonkeyPatch) -> None:
    from plugin.tests.testing_utils import CalcSheetStub

    monkeypatch.setattr("plugin.framework.queue_executor.main.is_current", lambda: True)
    sheet = CalcSheetStub("Sheet1")
    doc = CalcDocStub(sheets=[sheet], url="file:///refresh-used-end.ods")
    st = WorkbookState.for_doc(doc)
    assert st is not None

    sheet.getCellByPosition(0, 0).setFormula('=PY("keep")')
    sheet.getCellByPosition(3, 5).setValue(42.0)
    st.spills[("Sheet1", 0, 0)] = _spill.SpillRecord(code="keep", rows=1, cols=1)
    st.used_ends["Sheet1"] = (0, 0)

    _fire_sheet_modify(MagicMock(), doc, sheet)

    assert ("Sheet1", 0, 0) in st.spills
    assert st.used_ends["Sheet1"] == (5, 3)


def test_arm_spill_registry_loads_and_arms_sheets(monkeypatch: pytest.MonkeyPatch) -> None:
    from plugin.tests.testing_utils import CalcSheetStub

    s1 = CalcSheetStub("Sheet1")
    s2 = CalcSheetStub("Sheet2")
    s1.getCellByPosition(0, 0).setFormula('=PY("a")')
    s1.getCellByPosition(0, 1).setValue(10.0)
    s2.getCellByPosition(1, 2).setValue(20.0)
    doc = CalcDocStub(sheets=[s1, s2], url="file:///arm-sheets.ods", props={"RuntimeUID": "uid-arm-sheets"})

    payload = json.dumps({
        "Sheet1:0,0": {"code": "a", "rows": 2, "cols": 1},
        "Sheet2:0,0": {"code": "b", "rows": 3, "cols": 2},
        "MissingSheet:0,0": {"code": "c", "rows": 2, "cols": 1},
    })
    monkeypatch.setattr(
        "plugin.calc.python.spill.read_document_property",
        lambda _model, name, default=None: payload if name == "WriterAgentSpillRegistry" else default,
    )

    st = WorkbookState.for_doc(doc)
    assert st is not None
    try:
        _spill.arm_spill_registry(MagicMock(), doc, st)
        assert st.loaded is True
        assert st.spills[("Sheet1", 0, 0)].values == (((10.0,),),)
        assert st.spills[("Sheet2", 0, 0)].values is None
        assert "Sheet1" in st.listeners and "Sheet2" in st.listeners
        assert "MissingSheet" not in st.listeners
        assert st.used_ends["Sheet1"] == (1, 0)
        assert st.used_ends["Sheet2"] == (2, 1)
    finally:
        WorkbookState.forget_key("uid-arm-sheets")


def test_spill_constant_over_origin_drops_without_clearing(monkeypatch: pytest.MonkeyPatch) -> None:
    from plugin.tests.testing_utils import CalcSheetStub

    monkeypatch.setattr("plugin.framework.queue_executor.main.is_current", lambda: True)
    sheet = CalcSheetStub("Sheet1")
    doc = CalcDocStub(sheets=[sheet], url="file:///const-origin.ods")
    st = WorkbookState.for_doc(doc)
    assert st is not None

    sheet.getCellByPosition(0, 0).setValue(42.0)
    sheet.getCellByPosition(0, 1).setValue(2.0)
    st.used_ends["Sheet1"] = _spill._used_end(sheet)
    key = (st.key, "Sheet1", 0, 0)
    SPILL_REGISTRY[key] = _seen_record(sheet, 0, 0, "result = [1, 2]", 2, 1)

    _fire_sheet_modify(MagicMock(), doc, sheet)

    assert key not in SPILL_REGISTRY
    assert sheet.getCellByPosition(0, 1).getValue() == 2.0


@pytest.mark.parametrize("origin_formula", ["", '=PY("other")'])
def test_spill_stale_loaded_record_drops_without_clearing(
    monkeypatch: pytest.MonkeyPatch, origin_formula: str
) -> None:
    from plugin.tests.testing_utils import CalcSheetStub

    monkeypatch.setattr("plugin.framework.queue_executor.main.is_current", lambda: True)
    sheet = CalcSheetStub("Sheet1")
    doc = CalcDocStub(sheets=[sheet], url="file:///stale-open.ods")
    st = WorkbookState.for_doc(doc)
    assert st is not None

    if origin_formula:
        sheet.getCellByPosition(0, 0).setFormula(origin_formula)
    sheet.getCellByPosition(0, 1).setValue(99.0)
    st.used_ends["Sheet1"] = _spill._used_end(sheet)
    key = (st.key, "Sheet1", 0, 0)
    SPILL_REGISTRY[key] = _spill.SpillRecord(code="result = [1, 2]", rows=2, cols=1)

    _fire_sheet_modify(MagicMock(), doc, sheet)

    assert key not in SPILL_REGISTRY
    assert sheet.getCellByPosition(0, 1).getValue() == 99.0


@pytest.mark.parametrize("neighbour_changed", [False, True])
def test_spill_code_edit_with_nonzero_delta_keeps_record(
    monkeypatch: pytest.MonkeyPatch, neighbour_changed: bool
) -> None:
    from plugin.tests.testing_utils import CalcSheetStub

    monkeypatch.setattr("plugin.framework.queue_executor.main.is_current", lambda: True)
    sheet = CalcSheetStub("Sheet1")
    doc = CalcDocStub(sheets=[sheet], url="file:///code-edit-delta.ods")
    st = WorkbookState.for_doc(doc)
    assert st is not None

    sheet.getCellByPosition(0, 0).setFormula('=PY("new_code")')
    sheet.getCellByPosition(0, 1).setValue(10.0)
    key = (st.key, "Sheet1", 0, 0)
    SPILL_REGISTRY[key] = _seen_record(sheet, 0, 0, "old_code", 2, 1)
    if neighbour_changed:
        sheet.getCellByPosition(0, 1).setValue(55.0)
    sheet.getCellByPosition(0, 5).setValue(99.0)
    st.used_ends["Sheet1"] = (1, 0)

    _fire_sheet_modify(MagicMock(), doc, sheet)

    if neighbour_changed:
        assert key not in SPILL_REGISTRY
        assert sheet.getCellByPosition(0, 1).getValue() == 55.0
    else:
        assert key in SPILL_REGISTRY
        assert sheet.getCellByPosition(0, 1).getValue() == 10.0


def test_spill_duplicate_formula_sort_drops_without_clearing(monkeypatch: pytest.MonkeyPatch) -> None:
    from plugin.tests.testing_utils import CalcSheetStub

    monkeypatch.setattr("plugin.framework.queue_executor.main.is_current", lambda: True)
    sheet = CalcSheetStub("Sheet1")
    doc = CalcDocStub(sheets=[sheet], url="file:///dup-sort.ods")
    st = WorkbookState.for_doc(doc)
    assert st is not None

    code = "result = [1, 2]"
    sheet.getCellByPosition(0, 1).setValue(10.0)
    sheet.getCellByPosition(0, 2).setFormula(f'=PY("{code}")')
    sheet.getCellByPosition(0, 3).setFormula(f'=PY("{code}")')
    st.used_ends["Sheet1"] = _spill._used_end(sheet)
    key = (st.key, "Sheet1", 0, 0)
    SPILL_REGISTRY[key] = _seen_record(sheet, 0, 0, code, 2, 1)

    _fire_sheet_modify(MagicMock(), doc, sheet)

    assert key not in SPILL_REGISTRY
    assert sheet.getCellByPosition(0, 1).getValue() == 10.0


def test_perform_deferred_spill_preserves_pending_delta(monkeypatch: pytest.MonkeyPatch) -> None:
    from plugin.tests.testing_utils import CalcSheetStub

    monkeypatch.setattr("plugin.framework.queue_executor.main.is_current", lambda: True)
    monkeypatch.setattr(_spill, "save_spill_registry_for_doc", lambda _d, _st: None)
    sheet = CalcSheetStub("Sheet1")
    doc = CalcDocStub(sheets=[sheet], url="file:///pending-delta.ods")
    st = WorkbookState.for_doc(doc)
    assert st is not None

    # Block A was at row 2..3 (used_ends=(3, 0)). A pending row insert shifted A to row 3..4
    # and B to row 4 before B's 3-row spill runs.
    st.used_ends["Sheet1"] = (3, 0)
    st.spills[("Sheet1", 2, 0)] = _spill.SpillRecord(code="a", rows=2, cols=1)
    sheet.getCellByPosition(0, 3).setFormula('=PY("a")')
    sheet.getCellByPosition(0, 4).setFormula('=PY("b")')

    _spill.perform_deferred_spill(
        "Sheet1",
        4,
        0,
        [[1], [2], [3]],
        doc=doc,
        sheet=sheet,
        code="b",
        key=st.key,
    )

    # B's write extended used_end from (4, 0) to (6, 0) (+2 rows); witness advances (3+2, 0) = (5, 0).
    assert st.used_ends["Sheet1"] == (5, 0)

    # Pending modify pass sees delta = (6, 0) - (5, 0) = (1, 0), remapping A without clearing.
    _fire_sheet_modify(MagicMock(), doc, sheet)
    assert ("Sheet1", 2, 0) not in st.spills
    assert ("Sheet1", 3, 0) in st.spills
    assert sheet.getCellByPosition(0, 4).getFormula() == '=PY("b")'
    assert sheet.getCellByPosition(0, 5).getValue() == 2.0
    assert sheet.getCellByPosition(0, 6).getValue() == 3.0


def test_spill_row_delete_with_adjacent_py_drops_without_clearing(monkeypatch: pytest.MonkeyPatch) -> None:
    """Deleting row 5 shifts A6's =PY(y) into A5 with delta (-1, 0); A5's old record must drop, not keep."""
    from plugin.tests.testing_utils import CalcSheetStub

    monkeypatch.setattr("plugin.framework.queue_executor.main.is_current", lambda: True)
    sheet = CalcSheetStub("Sheet1")
    doc = CalcDocStub(sheets=[sheet], url="file:///row-del-adj-py.ods")
    st = WorkbookState.for_doc(doc)
    assert st is not None

    # Before delete: A5 =PY("x") spilled 1x4 (B5:D5), A6 =PY("y") had user data in B6:D6 (used_end=(5, 3)).
    # After deleting row 5: A5 now holds =PY("y") and user data is at B5:D5 (used_end=(4, 3)).
    st.used_ends["Sheet1"] = (5, 3)
    st.spills[("Sheet1", 4, 0)] = _spill.SpillRecord(code="x", rows=1, cols=4, values=(((2.0, 3.0, 4.0),),))
    sheet.getCellByPosition(0, 4).setFormula('=PY("y")')
    sheet.getCellByPosition(1, 4).setValue(101.0)
    sheet.getCellByPosition(2, 4).setValue(102.0)

    # Pass 1: negative delta (-1, 0) drops x's record and leaves user data untouched.
    _fire_sheet_modify(MagicMock(), doc, sheet)
    assert ("Sheet1", 4, 0) not in st.spills
    assert sheet.getCellByPosition(1, 4).getValue() == 101.0
    assert sheet.getCellByPosition(2, 4).getValue() == 102.0

    # Pass 2: subsequent unrelated edit with delta (0, 0) still leaves user data untouched.
    _fire_sheet_modify(MagicMock(), doc, sheet)
    assert sheet.getCellByPosition(1, 4).getValue() == 101.0
    assert sheet.getCellByPosition(2, 4).getValue() == 102.0


def test_spill_adjacent_records_remap_both_directions(monkeypatch: pytest.MonkeyPatch) -> None:
    """Adjacent records remapping into each other's old keys must not overwrite one another."""
    from plugin.tests.testing_utils import CalcSheetStub

    monkeypatch.setattr("plugin.framework.queue_executor.main.is_current", lambda: True)

    # Shift down (5->6, 6->7, 7->8)
    sheet_down = CalcSheetStub("Sheet1")
    doc_down = CalcDocStub(sheets=[sheet_down], url="file:///remap-down.ods")
    st_down = WorkbookState.for_doc(doc_down)
    assert st_down is not None
    for row, code, cols in ((5, "a", 2), (6, "b", 3), (7, "c", 4)):
        st_down.spills[("Sheet1", row, 0)] = _spill.SpillRecord(code=code, rows=1, cols=cols)
        sheet_down.getCellByPosition(0, row + 1).setFormula(f'=PY("{code}")')
    st_down.used_ends["Sheet1"] = (7, 0)

    _fire_sheet_modify(MagicMock(), doc_down, sheet_down)
    assert set(st_down.spills) == {("Sheet1", 6, 0), ("Sheet1", 7, 0), ("Sheet1", 8, 0)}
    assert (st_down.spills[("Sheet1", 6, 0)].code, st_down.spills[("Sheet1", 6, 0)].cols) == ("a", 2)
    assert (st_down.spills[("Sheet1", 7, 0)].code, st_down.spills[("Sheet1", 7, 0)].cols) == ("b", 3)
    assert (st_down.spills[("Sheet1", 8, 0)].code, st_down.spills[("Sheet1", 8, 0)].cols) == ("c", 4)

    # Shift up with reverse dict order (7->6, 6->5, 5->4)
    sheet_up = CalcSheetStub("Sheet1")
    doc_up = CalcDocStub(sheets=[sheet_up], url="file:///remap-up.ods")
    st_up = WorkbookState.for_doc(doc_up)
    assert st_up is not None
    for row, code, cols in ((7, "c", 4), (6, "b", 3), (5, "a", 2)):
        st_up.spills[("Sheet1", row, 0)] = _spill.SpillRecord(code=code, rows=1, cols=cols)
        sheet_up.getCellByPosition(0, row - 1).setFormula(f'=PY("{code}")')
    st_up.used_ends["Sheet1"] = (7, 0)

    _fire_sheet_modify(MagicMock(), doc_up, sheet_up)
    assert set(st_up.spills) == {("Sheet1", 4, 0), ("Sheet1", 5, 0), ("Sheet1", 6, 0)}
    assert (st_up.spills[("Sheet1", 4, 0)].code, st_up.spills[("Sheet1", 4, 0)].cols) == ("a", 2)
    assert (st_up.spills[("Sheet1", 5, 0)].code, st_up.spills[("Sheet1", 5, 0)].cols) == ("b", 3)
    assert (st_up.spills[("Sheet1", 6, 0)].code, st_up.spills[("Sheet1", 6, 0)].cols) == ("c", 4)


@pytest.mark.parametrize("used_end", [(2, 0), (3, 0), (1, 0)])
def test_moved_formula_respills_before_modify_pass(
    monkeypatch: pytest.MonkeyPatch, used_end: tuple[int, int]
) -> None:
    from plugin.calc.python.formula_locator_cache import FormulaOrigin
    from plugin.tests.testing_utils import CalcSheetStub

    monkeypatch.setattr("plugin.framework.queue_executor.main.is_current", lambda: True)
    saved: list[str] = []

    def mock_get_prop(model, name, default=None):
        if name == "WriterAgentSpillRegistry":
            return saved[-1] if saved else default
        return default

    def mock_set_prop(model, name, value):
        if name == "WriterAgentSpillRegistry":
            saved.append(value)

    monkeypatch.setattr("plugin.calc.python.spill.read_document_property", mock_get_prop)
    monkeypatch.setattr("plugin.calc.python.spill.set_document_property", mock_set_prop)

    sheet = CalcSheetStub("Sheet1")
    doc = CalcDocStub(sheets=[sheet], url="file:///moved-respill.ods", props={"RuntimeUID": "uid-moved-respill"})
    st = WorkbookState.for_doc(doc)
    assert st is not None
    try:
        code = "result = [10, 20, 30]"
        origin_cell = sheet.getCellByPosition(0, 1)
        origin_cell.setFormula(f'=PY("{code}")')
        sheet.getCellByPosition(0, 2).setValue(2.0)
        sheet.getCellByPosition(0, 3).setValue(3.0)
        st.spills[("Sheet1", 0, 0)] = _spill.SpillRecord(code=code, rows=3, cols=1)
        st.used_ends["Sheet1"] = used_end

        grid = [[10], [20], [30]]
        prepared = _spill.prepare_auto_spill(
            _ctx_with_doc(doc),
            doc,
            st,
            FormulaOrigin(sheet, origin_cell, "Sheet1", 1, 0),
            code,
            grid,
        )
        if used_end == (2, 0):
            assert isinstance(prepared, _spill.PreparedSpill)
            assert saved == []
            assert sheet.getCellByPosition(0, 2).getValue() == 2.0

            perform_deferred_spill(
                "Sheet1",
                1,
                0,
                grid,
                doc=doc,
                sheet=sheet,
                code=code,
                key=st.key,
            )
            st.flush_spill_registry()
            assert sheet.getCellByPosition(0, 3).getValue() == 30.0
            assert list(json.loads(saved[-1]).keys()) == ["Sheet1:1,0"]
        else:
            assert prepared == "#SPILL!"
    finally:
        WorkbookState.forget_key(st.key)


def test_blocked_growth_reports_first_cell_outside_owned_rect() -> None:
    from plugin.calc.python.diagnostics import diagnostics_workbook_key, get_diagnostics_store
    from plugin.calc.python.formula_locator_cache import FormulaOrigin
    from plugin.tests.testing_utils import CalcSheetStub

    sheet = CalcSheetStub("Sheet1")
    doc = CalcDocStub(sheets=[sheet], url="file:///growth-first.ods", props={"RuntimeUID": "uid-growth-first"})
    st = WorkbookState.for_doc(doc)
    assert st is not None
    try:
        code = "grid_3x3"
        origin_cell = sheet.getCellByPosition(0, 0)
        origin_cell.setFormula(f'=PY("{code}")')
        sheet.getCellByPosition(1, 0).setValue(1.0)
        sheet.getCellByPosition(0, 1).setValue(2.0)
        sheet.getCellByPosition(1, 1).setValue(3.0)
        sheet.getCellByPosition(2, 1).setValue(99.0)  # C2
        sheet.getCellByPosition(0, 2).setValue(88.0)  # A3
        st.spills[("Sheet1", 0, 0)] = _spill.SpillRecord(code=code, rows=2, cols=2)

        grid = [[1, 2, 3], [4, 5, 6], [7, 8, 9]]
        origin = FormulaOrigin(sheet, origin_cell, "Sheet1", 0, 0)
        ctx = _ctx_with_doc(doc)

        assert _spill.prepare_auto_spill(ctx, doc, st, origin, code, grid) == "#SPILL!"
        entry1 = get_diagnostics_store().latest_for_code(diagnostics_workbook_key(doc), code)
        assert entry1 is not None and "C2" in entry1.message

        sheet.getCellByPosition(2, 1).setString("")
        assert _spill.prepare_auto_spill(ctx, doc, st, origin, code, grid) == "#SPILL!"
        entry2 = get_diagnostics_store().latest_for_code(diagnostics_workbook_key(doc), code)
        assert entry2 is not None and "A3" in entry2.message

        sheet.getCellByPosition(0, 2).setString("")
        prepared = _spill.prepare_auto_spill(ctx, doc, st, origin, code, grid)
        assert isinstance(prepared, _spill.PreparedSpill)
    finally:
        WorkbookState.forget_key(st.key)


@pytest.mark.parametrize("origin_formula", ["", '=PY("other")'])
def test_spill_clear_skips_cells_replaced_with_origin(
    monkeypatch: pytest.MonkeyPatch, origin_formula: str
) -> None:
    from plugin.tests.testing_utils import CalcSheetStub

    monkeypatch.setattr("plugin.framework.queue_executor.main.is_current", lambda: True)
    sheet = CalcSheetStub("Sheet1")
    doc = CalcDocStub(sheets=[sheet], url="file:///replaced-with-origin.ods")
    st = WorkbookState.for_doc(doc)
    assert st is not None

    sheet.getCellByPosition(0, 1).setValue(2.0)
    sheet.getCellByPosition(0, 2).setValue(3.0)
    key = (st.key, "Sheet1", 0, 0)
    SPILL_REGISTRY[key] = _seen_record(sheet, 0, 0, "result = [1, 2, 3]", 3, 1)

    if origin_formula:
        sheet.getCellByPosition(0, 0).setFormula(origin_formula)
    sheet.getCellByPosition(0, 1).setValue(100.0)
    sheet.getCellByPosition(0, 2).setValue(200.0)
    st.used_ends["Sheet1"] = _spill._used_end(sheet)

    _fire_sheet_modify(MagicMock(), doc, sheet)

    assert key not in SPILL_REGISTRY
    assert sheet.getCellByPosition(0, 1).getValue() == 100.0
    assert sheet.getCellByPosition(0, 2).getValue() == 200.0


def test_spill_clear_covers_rest_when_part_of_block_is_emptied(monkeypatch: pytest.MonkeyPatch) -> None:
    from plugin.tests.testing_utils import CalcSheetStub

    monkeypatch.setattr("plugin.framework.queue_executor.main.is_current", lambda: True)
    sheet = CalcSheetStub("Sheet1")
    doc = CalcDocStub(sheets=[sheet], url="file:///partial-empty.ods")
    st = WorkbookState.for_doc(doc)
    assert st is not None

    sheet.getCellByPosition(0, 0).setFormula('=PY("x")')
    sheet.getCellByPosition(0, 1).setValue(2.0)
    sheet.getCellByPosition(0, 2).setValue(3.0)
    key = (st.key, "Sheet1", 0, 0)
    SPILL_REGISTRY[key] = _seen_record(sheet, 0, 0, "x", 3, 1)

    sheet.getCellByPosition(0, 0).setFormula("")
    sheet.getCellByPosition(0, 1).setString("")
    st.used_ends["Sheet1"] = _spill._used_end(sheet)

    _fire_sheet_modify(MagicMock(), doc, sheet)

    assert key not in SPILL_REGISTRY
    assert sheet.getCellByPosition(0, 2).getValue() == 0.0
    assert sheet.getCellByPosition(0, 2).getString() == ""


def test_spill_clear_includes_value_edited_in_earlier_pass(monkeypatch: pytest.MonkeyPatch) -> None:
    from plugin.tests.testing_utils import CalcSheetStub

    monkeypatch.setattr("plugin.framework.queue_executor.main.is_current", lambda: True)
    sheet = CalcSheetStub("Sheet1")
    doc = CalcDocStub(sheets=[sheet], url="file:///edited-earlier-pass.ods")
    st = WorkbookState.for_doc(doc)
    assert st is not None

    sheet.getCellByPosition(0, 0).setFormula('=PY("x")')
    sheet.getCellByPosition(0, 1).setValue(2.0)
    sheet.getCellByPosition(0, 2).setValue(3.0)
    key = (st.key, "Sheet1", 0, 0)
    SPILL_REGISTRY[key] = _seen_record(sheet, 0, 0, "x", 3, 1)

    sheet.getCellByPosition(0, 1).setValue(50.0)
    st.used_ends["Sheet1"] = _spill._used_end(sheet)
    _fire_sheet_modify(MagicMock(), doc, sheet)
    assert key in SPILL_REGISTRY

    sheet.getCellByPosition(0, 0).setFormula("")
    _fire_sheet_modify(MagicMock(), doc, sheet)

    assert key not in SPILL_REGISTRY
    assert sheet.getCellByPosition(0, 1).getValue() == 0.0
    assert sheet.getCellByPosition(0, 2).getValue() == 0.0


@pytest.mark.parametrize("remembered", [(((2.0,),), ((3.0,),)), None])
def test_respill_keeps_cells_of_another_formulas_record(
    monkeypatch: pytest.MonkeyPatch, remembered: tuple[Any, ...] | None
) -> None:
    from plugin.tests.testing_utils import CalcSheetStub

    monkeypatch.setattr("plugin.framework.queue_executor.main.is_current", lambda: True)
    monkeypatch.setattr(_spill, "save_spill_registry_for_doc", lambda _d, _st: None)
    sheet = CalcSheetStub("Sheet1")
    doc = CalcDocStub(sheets=[sheet], url="file:///foreign-respill.ods")
    st = WorkbookState.for_doc(doc)
    assert st is not None

    sheet.getCellByPosition(0, 0).setFormula('=PY("other")')
    sheet.getCellByPosition(1, 0).setValue(100.0)
    sheet.getCellByPosition(0, 1).setValue(200.0)
    st.used_ends["Sheet1"] = _spill._used_end(sheet)
    st.spills[("Sheet1", 0, 0)] = _spill.SpillRecord(code="x", rows=2, cols=2, values=remembered)

    _spill.perform_deferred_spill(
        "Sheet1",
        0,
        0,
        [[8, 9], [10, 11]],
        doc=doc,
        sheet=sheet,
        code="other",
        key=st.key,
    )

    assert ("Sheet1", 0, 0) not in st.spills
    assert sheet.getCellByPosition(1, 0).getValue() == 100.0
    assert sheet.getCellByPosition(0, 1).getValue() == 200.0


def test_respill_after_code_edit_takes_over_unchanged_rect(monkeypatch: pytest.MonkeyPatch) -> None:
    from plugin.tests.testing_utils import CalcSheetStub

    monkeypatch.setattr("plugin.framework.queue_executor.main.is_current", lambda: True)
    monkeypatch.setattr(_spill, "save_spill_registry_for_doc", lambda _d, _st: None)
    sheet = CalcSheetStub("Sheet1")
    doc = CalcDocStub(sheets=[sheet], url="file:///respill-code-edit.ods")
    st = WorkbookState.for_doc(doc)
    assert st is not None

    sheet.getCellByPosition(0, 0).setFormula('=PY("new")')
    sheet.getCellByPosition(0, 1).setValue(2.0)
    sheet.getCellByPosition(0, 2).setValue(3.0)
    st.used_ends["Sheet1"] = _spill._used_end(sheet)
    st.spills[("Sheet1", 0, 0)] = _seen_record(sheet, 0, 0, "old", 3, 1)

    _spill.perform_deferred_spill(
        "Sheet1",
        0,
        0,
        [[10], [20]],
        doc=doc,
        sheet=sheet,
        code="new",
        key=st.key,
    )

    rec = st.spills[("Sheet1", 0, 0)]
    assert rec.code == "new"
    assert (rec.rows, rec.cols) == (2, 1)
    assert sheet.getCellByPosition(0, 1).getValue() == 20.0
    assert sheet.getCellByPosition(0, 2).getValue() == 0.0
    assert sheet.getCellByPosition(0, 2).getString() == ""


@pytest.mark.parametrize(
    ("rows", "cols", "neighbour_formula", "delta", "values_override", "expected"),
    [
        (1, 1, "", (0, 0), None, (0, 0)),
        (2, 1, "=SUM(1)", (0, 0), None, (0, 0)),
        (2, 1, "=#ERR519!", (0, 0), None, (2, 1)),
        (3, 1, "", (-1, 0), (3.0, 99.0), (2, 1)),
        (3, 1, "", (-1, 0), (88.0, 99.0), (1, 1)),
        (3, 1, "", (-1, 0), None, (3, 1)),
        # Deletion running past the bottom still owns the matching prefix.
        (10, 1, "", (-4, 0), (2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0, 99.0, 0.0), (8, 1)),
        # Deleting a row that also held the sheet's rightmost cell shrinks only rows for a 1-col spill.
        (3, 1, "", (-1, -2), (3.0, 99.0), (2, 1)),
        # Origin row or column mismatch on a single-axis delete falls back to (1, 1).
        (1, 3, "", (-1, 0), (999.0, 3.0), (1, 1)),
        (3, 1, "", (0, -1), (999.0, 3.0), (1, 1)),
        (2, 2, "", (-1, 0), (999.0, 3.0, 4.0), (1, 1)),
        # Typed-over values at delta (0, 0) are deliberately still owned (#3940).
        (2, 1, "", (0, 0), (99.0,), (2, 1)),
    ],
)
def test_owned_rect_rules(
    rows: int,
    cols: int,
    neighbour_formula: str,
    delta: tuple[int, int],
    values_override: tuple[float, ...] | None,
    expected: tuple[int, int],
) -> None:
    from plugin.tests.testing_utils import CalcSheetStub

    sheet = CalcSheetStub("Sheet1")
    sheet.getCellByPosition(0, 0).setFormula('=PY("x")')
    val = 2.0
    for r in range(rows):
        for c in range(cols):
            if (r, c) != (0, 0):
                sheet.getCellByPosition(c, r).setValue(val)
                val += 1.0
    rec = _seen_record(sheet, 0, 0, "x", rows, cols) if rows * cols > 1 else _spill.SpillRecord(code="x", rows=1, cols=1)
    if neighbour_formula:
        sheet.getCellByPosition(0, 1).setFormula(neighbour_formula)
    elif values_override is not None:
        idx = 0
        for r in range(rows):
            for c in range(cols):
                if (r, c) != (0, 0) and idx < len(values_override):
                    sheet.getCellByPosition(c, r).setValue(values_override[idx])
                    idx += 1

    assert _spill._owned_rect(sheet, 0, 0, rec, delta) == expected


def test_cleanup_orphans_shrinks_record_on_negative_delta_with_changed_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from plugin.tests.testing_utils import CalcSheetStub

    monkeypatch.setattr("plugin.framework.queue_executor.main.is_current", lambda: True)
    monkeypatch.setattr(_spill, "save_spill_registry_for_doc", lambda _d, _st: None)
    sheet = CalcSheetStub("Sheet1")
    doc = CalcDocStub(sheets=[sheet], url="file:///neg-delta-shrink.ods")
    st = WorkbookState.for_doc(doc)
    assert st is not None

    sheet.getCellByPosition(0, 0).setFormula('=PY("x")')
    sheet.getCellByPosition(0, 1).setValue(2.0)
    sheet.getCellByPosition(0, 2).setValue(3.0)
    SPILL_REGISTRY[(st.key, "Sheet1", 0, 0)] = _seen_record(sheet, 0, 0, "x", 3, 1)
    st.used_ends["Sheet1"] = (3, 0)

    # Simulate a row delete of row 2 shifting user value 99 into A3 (used_end 3 -> 2):
    # A1:A2 survives as (2, 1), A3 (99) is outside the shrunk record.
    sheet.getCellByPosition(0, 1).setValue(3.0)
    sheet.getCellByPosition(0, 2).setValue(99.0)
    _fire_sheet_modify(MagicMock(), doc, sheet)

    rec = SPILL_REGISTRY[(st.key, "Sheet1", 0, 0)]
    assert (rec.rows, rec.cols) == (2, 1)
    assert rec.values == (((3.0,),),)
    assert sheet.getCellByPosition(0, 1).getValue() == 3.0
    assert sheet.getCellByPosition(0, 2).getValue() == 99.0

    # When no contiguous surviving run matches, fall back to (1, 1).
    sheet.getCellByPosition(0, 1).setValue(88.0)
    st.used_ends["Sheet1"] = (3, 0)
    _fire_sheet_modify(MagicMock(), doc, sheet)
    rec2 = SPILL_REGISTRY[(st.key, "Sheet1", 0, 0)]
    assert (rec2.rows, rec2.cols) == (1, 1)


def test_prepare_auto_spill_after_code_edit_before_modify_pass(monkeypatch: pytest.MonkeyPatch) -> None:
    from plugin.calc.python.formula_locator_cache import FormulaOrigin
    from plugin.tests.testing_utils import CalcSheetStub

    monkeypatch.setattr("plugin.framework.queue_executor.main.is_current", lambda: True)
    monkeypatch.setattr(_spill, "save_spill_registry_for_doc", lambda _d, _st: None)
    sheet = CalcSheetStub("Sheet1")
    doc = CalcDocStub(sheets=[sheet], url="file:///code-edit-prep.ods")
    st = WorkbookState.for_doc(doc)
    assert st is not None

    origin_cell = sheet.getCellByPosition(0, 0)
    origin_cell.setFormula('=PY("old")')
    sheet.getCellByPosition(0, 1).setValue(2.0)
    sheet.getCellByPosition(0, 2).setValue(3.0)
    st.spills[("Sheet1", 0, 0)] = _seen_record(sheet, 0, 0, "old", 3, 1)
    st.used_ends["Sheet1"] = _spill._used_end(sheet)

    origin_cell.setFormula('=PY("new")')
    prepared = _spill.prepare_auto_spill(
        _ctx_with_doc(doc), doc, st, FormulaOrigin(sheet, origin_cell, "Sheet1", 0, 0), "new", [[10], [20], [30]]
    )
    assert isinstance(prepared, _spill.PreparedSpill)

    # Also cover the "keep" path (code edit + positive used-area growth) through perform_deferred_spill.
    sheet.getCellByPosition(0, 5).setValue(1.0)
    _fire_sheet_modify(MagicMock(), doc, sheet)
    assert ("Sheet1", 0, 0) in st.spills and st.spills[("Sheet1", 0, 0)].code == "old"
    _spill.perform_deferred_spill("Sheet1", 0, 0, [[10], [20]], doc=doc, sheet=sheet, code="new", key=st.key)
    assert st.spills[("Sheet1", 0, 0)].code == "new"
    assert sheet.getCellByPosition(0, 1).getValue() == 20.0
    assert sheet.getCellByPosition(0, 2).getType() == _spill.EMPTY


def test_owned_rect_does_not_read_values_when_user_formula_inside(monkeypatch: pytest.MonkeyPatch) -> None:
    from plugin.tests.testing_utils import CalcSheetStub

    sheet = CalcSheetStub("Sheet1")
    sheet.getCellByPosition(0, 0).setFormula('=PY("x")')
    sheet.getCellByPosition(0, 1).setValue(2.0)
    rec = _seen_record(sheet, 0, 0, "x", 2, 1)
    sheet.getCellByPosition(0, 1).setFormula("=A1*2")

    def _boom(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("_rect_values must not be called when a user formula is inside")

    monkeypatch.setattr(_spill, "_rect_values", _boom)
    assert _spill._owned_rect(sheet, 0, 0, rec, (-1, 0)) == (0, 0)


def test_owned_rect_10000_row_linear_match(monkeypatch: pytest.MonkeyPatch) -> None:
    n = 10000
    comparisons = 0

    class _CountingFloat(float):
        def __eq__(self, other: object) -> bool:
            nonlocal comparisons
            comparisons += 1
            return super().__eq__(other)

        def __ne__(self, other: object) -> bool:
            nonlocal comparisons
            comparisons += 1
            return super().__ne__(other)

    old_col = tuple((float(r),) for r in range(1, n))
    cur_rows = list(range(1, n - 5)) + list(range(n - 4, n)) + [99999]
    cur_col = tuple((_CountingFloat(v),) for v in cur_rows)
    rec = _spill.SpillRecord(code="x", rows=n, cols=1, values=(old_col,))

    monkeypatch.setattr(_spill, "_user_formula_in_rect", lambda *_a: None)
    monkeypatch.setattr(_spill, "_rect_values", lambda *_a: (cur_col,))

    assert _spill._owned_rect(object(), 0, 0, rec, (-1, 0)) == (n - 1, 1)
    assert comparisons <= 5 * n


def test_perform_deferred_spill_code_in_cell_ref(monkeypatch: pytest.MonkeyPatch) -> None:
    from plugin.calc.python.formula_locator_cache import FormulaOrigin, locate_formula_cell_in_doc
    from plugin.tests.testing_utils import CalcSheetStub

    doc = CalcDocStub(
        sheets=[CalcSheetStub("Sheet1"), CalcSheetStub("Sheet2")],
        url="file:///code_in_cell.ods",
        props={"RuntimeUID": "uid-code-in-cell"},
    )
    sheet1 = doc.getSheets().getByName("Sheet1")
    sheet2 = doc.getSheets().getByName("Sheet2")
    code_same = "[[1, 2], [3, 4]]"
    code_cross = "[[10], [20]]"

    sheet1.getCellByPosition(3, 0).setString(code_same)  # D1
    sheet1.getCellByPosition(0, 0).setFormula("=PY(D1)")  # A1
    sheet2.getCellByPosition(0, 0).setString(code_cross)  # Sheet2.A1
    sheet1.getCellByPosition(0, 4).setFormula("=PY(Sheet2.A1)")  # A5

    monkeypatch.setattr(main, "is_current", lambda: True)
    reset_calc_workbook_states()
    state = WorkbookState.for_doc(doc)
    assert state is not None
    try:
        loc_same = locate_formula_cell_in_doc(doc, code_same)
        assert loc_same is not None
        prep_same = _spill.prepare_auto_spill(
            _ctx_with_doc(doc), doc, state, FormulaOrigin.from_located(loc_same), code_same, [[1, 2], [3, 4]]
        )
        assert isinstance(prep_same, _spill.PreparedSpill)
        perform_deferred_spill("Sheet1", 0, 0, [[1, 2], [3, 4]], doc=doc, sheet=sheet1, code=code_same, key=state.key)
        assert sheet1.getCellByPosition(1, 0).getValue() == 2.0
        assert sheet1.getCellByPosition(0, 1).getValue() == 3.0
        assert sheet1.getCellByPosition(1, 1).getValue() == 4.0

        perform_deferred_spill("Sheet1", 4, 0, [[10], [20]], doc=doc, sheet=sheet1, code=code_cross, key=state.key)
        assert sheet1.getCellByPosition(0, 5).getValue() == 20.0

        # cleanup_orphans must recognize both code-in-cell origins and keep their records.
        _spill.cleanup_orphans(state, sheet1, doc=doc, sheet_name="Sheet1")
        assert ("Sheet1", 0, 0) in state.spills
        assert ("Sheet1", 4, 0) in state.spills
    finally:
        WorkbookState.forget_key(state.key)


def test_load_spill_registry_failure_does_not_mark_loaded_or_clobber_save(monkeypatch: pytest.MonkeyPatch) -> None:
    stored = json.dumps({"Sheet1:0,0": {"code": "x", "rows": 2, "cols": 1}})

    for fail_kind in ("getPropertyValue", "getPropertySetInfo"):
        doc = CalcDocStub(url=f"file:///fail_{fail_kind}.ods", props={"RuntimeUID": f"uid-fail-{fail_kind}"})
        bag = doc.getDocumentProperties().UserDefinedProperties
        bag.values["WriterAgentSpillRegistry"] = stored
        orig_get_value = bag.getPropertyValue
        orig_get_info = bag.getPropertySetInfo
        fail_read = True

        def _maybe_fail_value(name: str, _orig=orig_get_value) -> Any:
            if fail_read and name == "WriterAgentSpillRegistry":
                raise RuntimeError("transient getPropertyValue error")
            return _orig(name)

        def _maybe_fail_info(_orig=orig_get_info) -> Any:
            if fail_read:
                raise RuntimeError("transient getPropertySetInfo error")
            return _orig()

        if fail_kind == "getPropertyValue":
            monkeypatch.setattr(bag, "getPropertyValue", _maybe_fail_value)
        else:
            monkeypatch.setattr(bag, "getPropertySetInfo", _maybe_fail_info)

        reset_calc_workbook_states()
        st = WorkbookState.for_doc(doc)
        assert st is not None
        try:
            _spill.load_spill_registry_for_doc(doc, st)
            assert not st.loaded
            assert st.spills == {}

            _spill.save_spill_registry_for_doc(doc, st)
            assert bag.values["WriterAgentSpillRegistry"] == stored

            fail_read = False
            _spill.load_spill_registry_for_doc(doc, st)
            assert st.loaded
            assert ("Sheet1", 0, 0) in st.spills

            _spill.save_spill_registry_for_doc(doc, st)
            assert json.loads(bag.values["WriterAgentSpillRegistry"]) == {
                "Sheet1:0,0": {"code": "x", "rows": 2, "cols": 1}
            }
        finally:
            WorkbookState.forget_key(st.key)


def test_cleanup_orphans_loads_registry_before_dropping_records(monkeypatch: pytest.MonkeyPatch) -> None:
    from plugin.tests.testing_utils import CalcSheetStub

    doc = CalcDocStub(
        sheets=[CalcSheetStub("Sheet1"), CalcSheetStub("Sheet2")],
        url="file:///cleanup_loads.ods",
        props={"RuntimeUID": "uid-cleanup-loads"},
    )
    sheet1 = doc.getSheets().getByName("Sheet1")
    bag = doc.getDocumentProperties().UserDefinedProperties
    bag.values["WriterAgentSpillRegistry"] = json.dumps({
        "Sheet1:0,0": {"code": "x", "rows": 2, "cols": 1},
        "Sheet2:0,0": {"code": "y", "rows": 3, "cols": 1},
    })

    monkeypatch.setattr(main, "is_current", lambda: True)
    reset_calc_workbook_states()
    state = WorkbookState.for_doc(doc)
    assert state is not None
    active_state = state
    try:
        assert not state.loaded
        state.spills[("Sheet1", 0, 0)] = _spill.SpillRecord(code="x", rows=2, cols=1)

        _spill.cleanup_orphans(state, sheet1, doc=doc, sheet_name="Sheet1")
        state.flush_spill_registry()

        assert state.loaded
        assert ("Sheet1", 0, 0) not in state.spills
        assert ("Sheet2", 0, 0) in state.spills
        assert json.loads(bag.values["WriterAgentSpillRegistry"]) == {
            "Sheet2:0,0": {"code": "y", "rows": 3, "cols": 1}
        }

        WorkbookState.forget_key(state.key)
        reloaded_state = WorkbookState.for_doc(doc)
        assert reloaded_state is not None
        active_state = reloaded_state
        assert not reloaded_state.loaded
        _spill.load_spill_registry_for_doc(doc, reloaded_state)
        assert ("Sheet1", 0, 0) not in reloaded_state.spills
        assert ("Sheet2", 0, 0) in reloaded_state.spills
    finally:
        WorkbookState.forget_key(active_state.key)


def test_load_spill_registry_unparsable_payload_marks_loaded_and_allows_recovery() -> None:
    for bad_payload in ("{not-json", "[1, 2]"):
        doc = CalcDocStub(url="file:///bad_json.ods", props={"RuntimeUID": "uid-bad-json"})
        bag = doc.getDocumentProperties().UserDefinedProperties
        bag.values["WriterAgentSpillRegistry"] = bad_payload

        reset_calc_workbook_states()
        st = WorkbookState.for_doc(doc)
        assert st is not None
        try:
            _spill.load_spill_registry_for_doc(doc, st)
            assert st.loaded
            assert st.spills == {}

            st.spills[("Sheet1", 0, 0)] = _spill.SpillRecord(code="recovered", rows=2, cols=1)
            _spill.save_spill_registry_for_doc(doc, st)
            assert json.loads(bag.values["WriterAgentSpillRegistry"]) == {
                "Sheet1:0,0": {"code": "recovered", "rows": 2, "cols": 1}
            }
        finally:
            WorkbookState.forget_key(st.key)


def test_load_spill_registry_missing_property_marks_loaded() -> None:
    reset_calc_workbook_states()
    doc = CalcDocStub(url="file:///clean_load.ods", props={"RuntimeUID": "uid-clean-load"})
    st = WorkbookState.for_doc(doc)
    assert st is not None
    try:
        _spill.load_spill_registry_for_doc(doc, st)
        assert st.loaded
        assert st.spills == {}
    finally:
        WorkbookState.forget_key(st.key)


def test_multiple_spills_in_one_debounce_drain_save_registry_once(monkeypatch: pytest.MonkeyPatch) -> None:
    """Multiple deferred spills in one cycle defer persistence until the registry debounce flushes."""
    from plugin.calc.python.formula_locator_cache import FormulaOrigin

    monkeypatch.setattr("plugin.framework.queue_executor.main.is_current", lambda: True)
    saved_payloads: list[str] = []

    def _mock_set_prop(_model: Any, name: str, value: Any) -> None:
        if name == "WriterAgentSpillRegistry":
            saved_payloads.append(value)

    monkeypatch.setattr("plugin.calc.python.spill.set_document_property", _mock_set_prop)

    reset_calc_workbook_states()
    doc = CalcDocStub(url="file:///batch-save.ods", props={"RuntimeUID": "uid-batch-save"})
    sheet = doc.getSheets().getByIndex(0)
    st = WorkbookState.for_doc(doc)
    assert st is not None

    try:
        specs = [
            (0, 0, "c0", [[1], [2]]),
            (0, 3, "c1", [[3], [4], [5]]),
            (0, 6, "c2", [[6, 7]]),
        ]
        for row, col, code, grid in specs:
            origin_cell = sheet.getCellByPosition(col, row)
            origin_cell.setFormula(f'=PY("{code}")')
            prepared = _spill.prepare_auto_spill(
                MagicMock(),
                doc,
                st,
                FormulaOrigin(sheet, origin_cell, "Sheet1", row, col),
                code,
                grid,
            )
            assert isinstance(prepared, _spill.PreparedSpill)
            _spill.queue_deferred_spill_write(st, doc, prepared, grid)
            st.debounce.flush_for_tests(f"spill:Sheet1:{row}:{col}")

        # All three cell blocks are written immediately, while registry persistence is deferred.
        assert saved_payloads == []
        assert "spill-registry" in st.debounce.pending_for_tests()
        assert sheet.getCellByPosition(0, 1).getValue() == 2
        assert sheet.getCellByPosition(3, 2).getValue() == 5
        assert sheet.getCellByPosition(7, 0).getValue() == 7

        st.debounce.flush_for_tests("spill-registry")
        assert len(saved_payloads) == 1
        assert json.loads(saved_payloads[0]) == {
            "Sheet1:0,0": {"code": "c0", "rows": 2, "cols": 1},
            "Sheet1:0,3": {"code": "c1", "rows": 3, "cols": 1},
            "Sheet1:0,6": {"code": "c2", "rows": 1, "cols": 2},
        }
        assert "spill-registry" not in st.debounce.pending_for_tests()
        st.flush_spill_registry()
        assert len(saved_payloads) == 1
    finally:
        WorkbookState.forget_key(st.key)
