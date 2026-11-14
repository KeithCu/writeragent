# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Unit tests for Calc =PY() deferred spill formatting and neighbour classification."""

from __future__ import annotations

import math
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest

import plugin.calc.python.spill as _spill
from plugin.calc.python.spill import perform_deferred_spill
from plugin.calc.python.workbook_state import WorkbookState
from plugin.framework.queue_executor import main
from plugin.tests.testing_utils import CalcDocStub
from tests.calc.python.calc_workbook_test_access import SPILL_REGISTRY, ctx_with_doc as _ctx_with_doc, reset_calc_workbook_states


def test_perform_deferred_spill_applies_standard_keys_to_temporal_neighbours(monkeypatch: pytest.MonkeyPatch) -> None:
    formats = MagicMock()
    formats.getStandardFormat.side_effect = lambda fmt_type, _loc: {2: 37, 4: 63, 6: 70}[fmt_type]
    formats.getFormatIndex.return_value = 64
    formats.getByKey.side_effect = lambda key: SimpleNamespace(getPropertyValue=lambda prop: 2 if key == 40 and prop == "Type" else 16)

    doc = CalcDocStub(url="file:///spill_fmt.ods", props={"RuntimeUID": "uid-spill-fmt", "CharLocale": SimpleNamespace(Language="en", Country="US", Variant="")}, number_formats=formats)
    sheet = doc.getSheets().getByName("Sheet1")
    code = "grid"
    sheet.getCellByPosition(0, 0).setFormula(f'=PY("{code}")')
    # A3 (col 0, row 2) already carries a date NumberFormat key (40), which should be preserved.
    sheet.getCellByPosition(0, 2).setPropertyValue("NumberFormat", 40)

    monkeypatch.setattr(main, "is_current", lambda: True)
    state = WorkbookState.for_doc(doc)
    assert state is not None
    try:
        grid = [["2026-08-08", "PT30H"], ["x", "2026-08-08T12:00:00"], ["2026-08-08", 5]]
        perform_deferred_spill("Sheet1", 0, 0, grid, doc=doc, sheet=sheet, code=code, key=state.key)

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
        assert [(c.kind, c.value) for c in rec.cells] == [("n", 1.25), ("s", "x"), ("n", 46242.5), ("n", 46242.0), ("n", 5.0)]
    finally:
        WorkbookState.forget_key(state.key)


def test_perform_deferred_spill_error_and_empty_neighbours_record_kinds(monkeypatch: pytest.MonkeyPatch) -> None:
    doc = CalcDocStub(url="file:///spill_err.ods", props={"RuntimeUID": "uid-spill-err"})
    sheet = doc.getSheets().getByName("Sheet1")
    code = "errs"
    sheet.getCellByPosition(0, 0).setFormula(f'=PY("{code}")')

    monkeypatch.setattr(main, "is_current", lambda: True)
    state = WorkbookState.for_doc(doc)
    assert state is not None
    try:
        grid = [[1, "#N/A"], ["#VALUE!", ""]]
        perform_deferred_spill("Sheet1", 0, 0, grid, doc=doc, sheet=sheet, code=code, key=state.key)

        # B1 (#N/A): no =#ERR formula written
        assert sheet.getCellByPosition(1, 0).getFormula() == ""
        # A2 (#VALUE!): =#ERR519! formula written
        assert sheet.getCellByPosition(0, 1).getFormula() == "=#ERR519!"

        rec = state.spills[("Sheet1", 0, 0)]
        assert [(c.kind, c.value) for c in rec.cells] == [("err", 32767), ("err", 519), ("e", None)]
    finally:
        WorkbookState.forget_key(state.key)


def _fire_sheet_modify(ctx, doc, sheet, sheet_name="Sheet1"):
    from plugin.calc.python.sheet_modify import SheetModifyDispatcher, flush_sheet_modify_pass_for_tests
    from plugin.calc.python.workbook_state import WorkbookState

    st = WorkbookState.for_doc(doc)
    assert st is not None
    got = sheet.getName() if callable(getattr(sheet, "getName", None)) else None
    if not isinstance(got, str):
        sheet.getName = lambda: sheet_name  # MagicMock.getName() would rename spills
    SheetModifyDispatcher(ctx, doc, sheet_name).modified(SimpleNamespace(Source=sheet))
    flush_sheet_modify_pass_for_tests(ctx, doc, sheet, sheet_name=sheet_name)
    return st


def test_load_and_save_spill_registry(monkeypatch: pytest.MonkeyPatch) -> None:
    """Test that spill registry loads from and saves to document properties correctly."""
    import json
    from plugin.calc.python.spill import SpillCell, SpillRecord

    saved_payload = None

    def mock_get_prop(model, name, default=None):
        if name == "WriterAgentSpillRegistry":
            return saved_payload if saved_payload is not None else json.dumps({"Sheet1:1,1": {"code": "orig_code", "cells": [{"r": 2, "c": 1, "t": "n", "v": 20.0}, {"r": 3, "c": 1, "t": "n", "v": 30.0}]}})
        return default

    def mock_set_prop(model, name, value):
        nonlocal saved_payload
        if name == "WriterAgentSpillRegistry":
            saved_payload = value

    monkeypatch.setattr("plugin.calc.python.spill.get_document_property", mock_get_prop)
    monkeypatch.setattr("plugin.calc.python.spill.set_document_property", mock_set_prop)

    doc = CalcDocStub(url="file:///fake_doc.ods")

    reset_calc_workbook_states()
    from plugin.calc.python.workbook_state import WorkbookState

    st = WorkbookState.for_doc(doc)
    assert st is not None
    _spill.load_spill_registry_for_doc(doc, st)
    key = ("file:///fake_doc.ods", "Sheet1", 1, 1)
    assert key in SPILL_REGISTRY
    rec = SPILL_REGISTRY[key]
    assert rec.spills == [(2, 1), (3, 1)]
    assert rec.code == "orig_code"
    assert rec.cells[0].kind == "n" and rec.cells[0].value == 20.0
    assert rec.cells[1].kind == "n" and rec.cells[1].value == 30.0

    SPILL_REGISTRY[key] = SpillRecord(code="some_code", cells=[SpillCell(2, 1, "n", 20.0), SpillCell(3, 1, "n", 30.0)])
    _spill.save_spill_registry_for_doc(doc, st)

    assert saved_payload is not None
    data = json.loads(saved_payload)
    assert data["Sheet1:1,1"] == {"code": "some_code", "cells": [{"r": 2, "c": 1, "t": "n", "v": 20.0}, {"r": 3, "c": 1, "t": "n", "v": 30.0}]}

    # Add a second load of that object JSON
    reset_calc_workbook_states()
    st = WorkbookState.for_doc(doc)
    assert st is not None
    _spill.load_spill_registry_for_doc(doc, st)
    rec2 = SPILL_REGISTRY[key]
    assert rec2.spills == [(2, 1), (3, 1)]
    assert rec2.code == "some_code"
    assert rec2.cells[0].kind == "n" and rec2.cells[0].value == 20.0


def test_nonfinite_spill_values_round_trip_through_document_property(monkeypatch: pytest.MonkeyPatch) -> None:
    import json
    from plugin.calc.python.spill import SpillCell, SpillRecord

    stored: dict[str, Any] = {}

    def mock_get_prop(model: Any, name: str, default: Any = None) -> Any:
        return stored.get(name, default)

    def mock_set_prop(model: Any, name: str, value: Any) -> None:
        stored[name] = value

    monkeypatch.setattr("plugin.calc.python.spill.get_document_property", mock_get_prop)
    monkeypatch.setattr("plugin.calc.python.spill.set_document_property", mock_set_prop)

    doc = CalcDocStub(url="file:///nonfinite.ods")
    reset_calc_workbook_states()
    from plugin.calc.python.workbook_state import WorkbookState

    st = WorkbookState.for_doc(doc)
    assert st is not None
    st.spills[("Sheet1", 1, 1)] = SpillRecord(code="c", cells=[SpillCell(2, 1, "n", float("nan")), SpillCell(3, 1, "n", float("inf")), SpillCell(4, 1, "n", float("-inf")), SpillCell(5, 1, "n", 1.5)])
    key = (st.key, "Sheet1", 1, 1)
    try:
        _spill.save_spill_registry_for_doc(doc, st)

        saved = stored.get("WriterAgentSpillRegistry")
        assert saved is not None
        data = json.loads(saved)
        assert data["Sheet1:1,1"]["cells"] == [{"r": 2, "c": 1, "t": "n", "v": "nan"}, {"r": 3, "c": 1, "t": "n", "v": "inf"}, {"r": 4, "c": 1, "t": "n", "v": "-inf"}, {"r": 5, "c": 1, "t": "n", "v": 1.5}]

        reset_calc_workbook_states()
        st2 = WorkbookState.for_doc(doc)
        assert st2 is not None
        _spill.load_spill_registry_for_doc(doc, st2)
        rec = SPILL_REGISTRY[key]
        assert all(c.kind == "n" for c in rec.cells)
        assert math.isnan(rec.cells[0].value)
        assert rec.cells[1].value == math.inf
        assert rec.cells[2].value == -math.inf
        assert rec.cells[3].value == 1.5
    finally:
        reset_calc_workbook_states()


def test_reloaded_nan_spill_cell_keeps_user_edit_on_cleanup(monkeypatch: pytest.MonkeyPatch) -> None:
    from plugin.calc.python.spill import SpillCell, SpillRecord
    from plugin.tests.testing_utils import CalcSheetStub

    monkeypatch.setattr("plugin.framework.queue_executor.main.is_current", lambda: True)

    stored: dict[str, Any] = {}

    def mock_get_prop(model: Any, name: str, default: Any = None) -> Any:
        return stored.get(name, default)

    def mock_set_prop(model: Any, name: str, value: Any) -> None:
        stored[name] = value

    monkeypatch.setattr("plugin.calc.python.spill.get_document_property", mock_get_prop)
    monkeypatch.setattr("plugin.calc.python.spill.set_document_property", mock_set_prop)

    sheet = CalcSheetStub("Sheet1")
    doc = CalcDocStub(sheets=[sheet], url="file:///nan_cleanup.ods")

    reset_calc_workbook_states()
    from plugin.calc.python.workbook_state import WorkbookState

    st = WorkbookState.for_doc(doc)
    assert st is not None
    st.spills[("Sheet1", 0, 0)] = SpillRecord(code="result = [float('nan'), 1.0]", cells=[SpillCell(1, 0, "n", float("nan"))])
    key = (st.key, "Sheet1", 0, 0)
    try:
        _spill.save_spill_registry_for_doc(doc, st)
        reset_calc_workbook_states()
        st = WorkbookState.for_doc(doc)
        assert st is not None
        _spill.load_spill_registry_for_doc(doc, st)

        reloaded = SPILL_REGISTRY[key]
        assert reloaded.cells[0].kind == "n"

        sheet.getCellByPosition(0, 0).setFormula("")
        sheet.getCellByPosition(0, 1).setValue(42.0)

        _fire_sheet_modify(MagicMock(), doc, sheet)

        assert key not in SPILL_REGISTRY
        assert sheet.getCellByPosition(0, 1).getValue() == 42.0
    finally:
        reset_calc_workbook_states()


def test_untitled_docs_do_not_share_spill_registry(monkeypatch: pytest.MonkeyPatch) -> None:
    """Two unsaved books must not share spill rows (each has its own WorkbookState)."""
    import json
    from plugin.calc.python.spill import PreparedSpill, SpillCell, SpillRecord

    stored: dict[int, str] = {}

    def mock_get_prop(model, name, default=None):
        if name == "WriterAgentSpillRegistry":
            return stored.get(id(model), default)
        return default

    def mock_set_prop(model, name, value):
        if name == "WriterAgentSpillRegistry":
            stored[id(model)] = value

    monkeypatch.setattr("plugin.calc.python.spill.get_document_property", mock_get_prop)
    monkeypatch.setattr("plugin.calc.python.spill.set_document_property", mock_set_prop)

    doc1 = CalcDocStub(url="", props={"RuntimeUID": "uid-spill-1"}, selection="B2")
    doc2 = CalcDocStub(url="", props={"RuntimeUID": "uid-spill-2"}, selection="B2")
    sheet1 = doc1.getSheets().getByName("Sheet1")
    sheet2 = doc2.getSheets().getByName("Sheet1")
    sheet1.getCellByPosition(1, 1).setFormula('=PYTHON("untitled_spill_a")')
    sheet2.getCellByPosition(1, 1).setFormula('=PYTHON("untitled_spill_b")')
    stored[id(doc1)] = json.dumps({"Sheet1:1,1": {"code": "untitled_spill_a", "cells": [{"r": 2, "c": 1, "t": "n", "v": 1.0}]}})
    stored[id(doc2)] = json.dumps({"Sheet1:1,1": {"code": "untitled_spill_b", "cells": [{"r": 9, "c": 9, "t": "n", "v": 1.0}]}})

    reset_calc_workbook_states()
    reset_calc_workbook_states()
    from plugin.calc.python.formula_locator_cache import FormulaOrigin, locate_formula_cell_in_doc
    from plugin.calc.python.workbook_state import WorkbookState

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

        assert SPILL_REGISTRY[key1].spills == [(2, 1)]
        assert SPILL_REGISTRY[key2].spills == [(9, 9)]
        assert ("", "Sheet1", 1, 1) not in SPILL_REGISTRY
        from plugin.calc.python.workbook_state import WorkbookState

        assert WorkbookState.for_doc(doc1) is not None and WorkbookState.for_doc(doc1).loaded
        assert WorkbookState.for_doc(doc2) is not None and WorkbookState.for_doc(doc2).loaded

        SPILL_REGISTRY[key1] = SpillRecord(code="untitled_spill_a", cells=[SpillCell(2, 1, "n", 2.0), SpillCell(3, 1, "n", 3.0)])
        _spill.save_spill_registry_for_doc(doc1, WorkbookState.for_doc(doc1))
        assert json.loads(stored[id(doc1)]) == {"Sheet1:1,1": {"code": "untitled_spill_a", "cells": [{"r": 2, "c": 1, "t": "n", "v": 2.0}, {"r": 3, "c": 1, "t": "n", "v": 3.0}]}}
        assert json.loads(stored[id(doc2)]) == {"Sheet1:1,1": {"code": "untitled_spill_b", "cells": [{"r": 9, "c": 9, "t": "n", "v": 1.0}]}}

        # Book 1's modify clears only book 1, even though both sheets are Sheet1.
        sheet1.getCellByPosition(1, 1).setFormula("")
        _fire_sheet_modify(ctx1, doc1, sheet1)
        assert key1 not in SPILL_REGISTRY
        assert SPILL_REGISTRY[key2].spills == [(9, 9)]

        SPILL_REGISTRY[key1] = SpillRecord(code="untitled_spill_a", cells=[SpillCell(2, 1, "n", 2.0)])
        WorkbookState.forget_key("uid-spill-1")
        assert key1 not in SPILL_REGISTRY
        assert SPILL_REGISTRY[key2].spills == [(9, 9)]

        # A scheduled identity from book 1 must not write book 2.
        _spill.perform_deferred_spill("Sheet1", 1, 1, [[8], [9]], doc=doc2, sheet=sheet2, code="untitled_spill_b", key="uid-spill-1")
        assert sheet2.getCellByPosition(1, 2).getValue() in (0, 0.0)
        assert ("", "Sheet1", 1, 1) not in SPILL_REGISTRY

        # Matching lifecycle key spills this book.
        _spill.perform_deferred_spill("Sheet1", 1, 1, [[3], [4]], doc=doc2, sheet=sheet2, code="untitled_spill_b", key="uid-spill-2")
        assert sheet2.getCellByPosition(1, 2).getValue() == 4.0
        assert ("", "Sheet1", 1, 1) not in SPILL_REGISTRY
        assert ("uid-spill-2", "Sheet1", 1, 1) in SPILL_REGISTRY
    finally:
        reset_calc_workbook_states()
        reset_calc_workbook_states()


def test_calc_spill_modify_listener_cleanup(monkeypatch: pytest.MonkeyPatch) -> None:
    """Sheet modify pass cleans up spilled cells when formula is removed."""
    monkeypatch.setattr("plugin.framework.queue_executor.main.is_current", lambda: True)
    # Listener cleanup asserts clearContents call args; keep a MagicMock sheet for that.
    sheet = MagicMock()
    doc = CalcDocStub(url="file:///fake_cleanup.ods")

    saved = []
    monkeypatch.setattr(_spill, "save_spill_registry_for_doc", lambda d, st: saved.append(d))

    from plugin.calc.python.workbook_state import WorkbookState

    st = WorkbookState.for_doc(doc)
    assert st is not None
    key = (st.key, "Sheet1", 1, 1)
    SPILL_REGISTRY[key] = _spill.SpillRecord(code="some_code", cells=[_spill.SpillCell(2, 1, "n", 20.0)])

    cell_B2 = MagicMock()
    cell_B3 = MagicMock()
    cell_B3.getType.return_value = 1
    cell_B3.getValue.return_value = 20.0

    def get_cell(c, r):
        if r == 1 and c == 1:
            return cell_B2
        if r == 2 and c == 1:
            return cell_B3
        return MagicMock()

    sheet.getCellByPosition.side_effect = get_cell

    cell_B2.getFormula.return_value = '=PYTHON("some_code")'
    _fire_sheet_modify(MagicMock(), doc, sheet)

    assert key in SPILL_REGISTRY
    cell_B3.clearContents.assert_not_called()

    cell_B2.getFormula.return_value = ""
    _fire_sheet_modify(MagicMock(), doc, sheet)

    assert key not in SPILL_REGISTRY
    cell_B3.clearContents.assert_called_once_with(55)
    assert len(saved) == 1


def test_spill_orphan_cleanup_saves_sheet_owner_not_active_doc(monkeypatch: pytest.MonkeyPatch) -> None:
    """Orphan cleanup must undo/save the workbook that owns the sheet."""
    monkeypatch.setattr("plugin.framework.queue_executor.main.is_current", lambda: True)
    from plugin.calc.python.sheet_modify import SheetModifyDispatcher, flush_sheet_modify_pass_for_tests
    from plugin.calc.python.workbook_state import WorkbookState

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
    key = (st.key, "Sheet1", 1, 1)
    SPILL_REGISTRY[key] = _spill.SpillRecord(code='=PY("test")', cells=[_spill.SpillCell(2, 1, "n", 99.0)])
    try:
        ctx = MagicMock()
        SheetModifyDispatcher(ctx, active, "Sheet1").modified(SimpleNamespace(Source=sheet))
        flush_sheet_modify_pass_for_tests(ctx, owner, sheet, sheet_name="Sheet1")
        assert key not in SPILL_REGISTRY
        assert spilled.getValue() == 0
        assert saved == [owner]
    finally:
        SPILL_REGISTRY.pop(key, None)


def test_calc_spill_modify_listener_clears_when_formula_only_contains_py(monkeypatch: pytest.MonkeyPatch) -> None:
    """=PYMT and other formulas that merely contain PY/PYTHON must drop stale spills."""
    monkeypatch.setattr("plugin.framework.queue_executor.main.is_current", lambda: True)
    sheet = MagicMock()
    doc = CalcDocStub(url="file:///fake_pymt.ods")
    monkeypatch.setattr(_spill, "save_spill_registry_for_doc", lambda d, st: None)

    from plugin.calc.python.workbook_state import WorkbookState

    st = WorkbookState.for_doc(doc)
    assert st is not None
    key = (st.key, "Sheet1", 1, 1)
    SPILL_REGISTRY[key] = _spill.SpillRecord(code="keep", cells=[_spill.SpillCell(2, 1, "n", 20.0)])

    origin = MagicMock()
    spilled = MagicMock()
    spilled.getType.return_value = 1
    spilled.getValue.return_value = 20.0

    def get_cell(c, r):
        if r == 1 and c == 1:
            return origin
        if r == 2 and c == 1:
            return spilled
        return MagicMock()

    sheet.getCellByPosition.side_effect = get_cell
    try:
        origin.getFormula.return_value = '=PY("keep")'
        _fire_sheet_modify(MagicMock(), doc, sheet)
        assert key in SPILL_REGISTRY
        spilled.clearContents.assert_not_called()

        origin.getFormula.return_value = "=PYMT(0.05, 12, 1000)"
        _fire_sheet_modify(MagicMock(), doc, sheet)
        assert key not in SPILL_REGISTRY
        spilled.clearContents.assert_called_once_with(55)
    finally:
        SPILL_REGISTRY.pop(key, None)


def _clear_spill_timer_registry() -> None:
    from plugin.calc.python.workbook_state import WorkbookState

    WorkbookState.reset_for_tests()


def test_off_main_spill_timer_registers_cached_workbook_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """Unload cancel must see off-main timers; registering them must not call UNO."""
    from plugin.calc.python.workbook_state import WorkbookState

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
    from plugin.calc.python.workbook_state import WorkbookState

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
    from plugin.calc.python.workbook_state import WorkbookState

    doc = CalcDocStub(url="file:///spill.ods")
    sheet = doc.getSheets().getByIndex(0)
    sheet.getCellByPosition(0, 0).setFormula("=SUM(A2)")
    monkeypatch.setattr("plugin.framework.queue_executor.main.is_current", lambda: True)
    state = WorkbookState.for_doc(doc)
    assert state is not None
    _spill.perform_deferred_spill("Sheet1", 0, 0, [["x", "y"]], doc=doc, sheet=sheet, code="result = 1", key=state.key)
    b1 = sheet.getCellByPosition(1, 0)
    assert b1.getString() == ""
    assert b1.getValue() == 0
    assert ("Sheet1", 0, 0) not in state.spills


def test_off_main_spill_timer_does_not_register_empty_lkey() -> None:
    """Missing doc must not arm a debounce slot."""
    from plugin.calc.python.workbook_state import WorkbookState

    _clear_spill_timer_registry()
    try:
        _spill.queue_off_main_auto_spill(None, "code", [[1, 2]], None)
        assert WorkbookState.known(None) is None
    finally:
        _clear_spill_timer_registry()


def test_prepare_auto_spill_respects_reported_sheet_size() -> None:
    """Spills past 1024 columns succeed on a 16384-column sheet; overflowing the last column is #SPILL!."""

    class FallbackCellStub:
        def getType(self) -> int:
            return 0  # EMPTY

    class FallbackSheetStub:
        def __init__(self, cell: FallbackCellStub) -> None:
            self._cell = cell

        def getName(self) -> str:
            return "Sheet1"

        def getCellByPosition(self, col: int, row: int) -> FallbackCellStub:
            return self._cell

        def getColumns(self):
            return SimpleNamespace(getCount=lambda: 16384)

        def getRows(self):
            return SimpleNamespace(getCount=lambda: 1048576)

    cell = FallbackCellStub()
    sheet = FallbackSheetStub(cell)
    doc = CalcDocStub(url="file:///wide-fallback.ods", props={"RuntimeUID": "uid-wide-fallback"})
    ctx = MagicMock()
    grid = [[1, 2]]

    from plugin.calc.python.formula_locator_cache import FormulaOrigin
    from plugin.calc.python.workbook_state import WorkbookState

    doc_id = "uid-wide-fallback"
    state = WorkbookState.for_doc(doc)
    assert state is not None
    try:
        # Crosses past 1024 columns (1020..1029) and near the 16384 boundary (16300..16301).
        for formula_col, wide_grid in ((1020, [list(range(1, 11))]), (16300, grid)):
            prepared = _spill.prepare_auto_spill(ctx, doc, state, FormulaOrigin(sheet, cell, "Sheet1", 0, formula_col), "py_code", wide_grid)
            assert prepared != "#SPILL!"
            assert isinstance(prepared, _spill.PreparedSpill)
            assert prepared.key == doc_id
            assert prepared.sheet_name == "Sheet1"
            assert prepared.formula_row == 0
            assert prepared.formula_col == formula_col

        prepared_spill = _spill.prepare_auto_spill(ctx, doc, state, FormulaOrigin(sheet, cell, "Sheet1", 0, 16383), "py_code", grid)
        assert prepared_spill == "#SPILL!"
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

    from plugin.calc.python.workbook_state import WorkbookState

    _spill.perform_deferred_spill("Sheet1", 0, 0, grid, doc=doc, sheet=passed_sheet, code="result = [[1, 2], [3, 4]]", key=WorkbookState.for_doc(doc).key)

    assert passed_sheet.getCellByPosition(1, 0).getValue() == 2.0
    assert passed_sheet.getCellByPosition(0, 1).getValue() == 3.0
    assert passed_sheet.getCellByPosition(1, 1).getValue() == 4.0


def test_spill_remap_when_block_moves_with_formula(monkeypatch: pytest.MonkeyPatch) -> None:
    """Post-row-insert state: formula at row 5, spilled values at rows 6-8, registry at row 4. modified() remaps key without clearing."""
    from plugin.tests.testing_utils import CalcSheetStub

    monkeypatch.setattr("plugin.framework.queue_executor.main.is_current", lambda: True)
    sheet = CalcSheetStub("Sheet1")
    doc = CalcDocStub(sheets=[sheet], url="file:///remap.ods")

    from plugin.calc.python.workbook_state import WorkbookState

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

    # Old registry entry at row 4
    old_key = (st.key, "Sheet1", 4, 0)
    SPILL_REGISTRY[old_key] = _spill.SpillRecord(code=code, cells=[_spill.SpillCell(5, 0, "n", 1.0), _spill.SpillCell(6, 0, "n", 2.0), _spill.SpillCell(7, 0, "n", 3.0)])

    _fire_sheet_modify(MagicMock(), doc, sheet)

    new_key = (st.key, "Sheet1", 5, 0)
    assert old_key not in SPILL_REGISTRY
    assert new_key in SPILL_REGISTRY
    assert SPILL_REGISTRY[new_key].spills == [(6, 0), (7, 0), (8, 0)]
    assert sheet.getCellByPosition(0, 6).getValue() == 1.0
    assert sheet.getCellByPosition(0, 7).getValue() == 2.0
    assert sheet.getCellByPosition(0, 8).getValue() == 3.0


def test_spill_clear_does_not_follow_cut_paste(monkeypatch: pytest.MonkeyPatch) -> None:
    """Formula moved to another column (col 3), spill values still at old col (col 0). Listener clears old coordinates and does not write destination col into registry."""
    from plugin.tests.testing_utils import CalcSheetStub

    monkeypatch.setattr("plugin.framework.queue_executor.main.is_current", lambda: True)
    sheet = CalcSheetStub("Sheet1")
    doc = CalcDocStub(sheets=[sheet], url="file:///cutpaste.ods")

    from plugin.calc.python.workbook_state import WorkbookState

    st = WorkbookState.for_doc(doc)
    assert st is not None

    code = "result = [1, 2]"
    # Formula cut-pasted to col 3 row 4
    sheet.getCellByPosition(3, 4).setFormula(f'=PY("{code}")')
    # Old formula cell at col 0 row 4 is now empty
    sheet.getCellByPosition(0, 4).setFormula("")
    # Old spill values still at col 0 row 5
    sheet.getCellByPosition(0, 5).setValue(2.0)

    old_key = (st.key, "Sheet1", 4, 0)
    SPILL_REGISTRY[old_key] = _spill.SpillRecord(code=code, cells=[_spill.SpillCell(5, 0, "n", 2.0)])

    _fire_sheet_modify(MagicMock(), doc, sheet)

    assert old_key not in SPILL_REGISTRY
    assert (st.key, "Sheet1", 4, 3) not in SPILL_REGISTRY
    # Spilled cell at (col 0, row 5) was cleared
    assert sheet.getCellByPosition(0, 5).getValue() in (0, 0.0)


def test_spill_clear_skips_formula_cell_and_edited_value(monkeypatch: pytest.MonkeyPatch) -> None:
    """One spill cell is now =SUM(1) (not cleared), one value was edited (not cleared), one still matches (cleared)."""
    from plugin.tests.testing_utils import CalcSheetStub

    monkeypatch.setattr("plugin.framework.queue_executor.main.is_current", lambda: True)
    sheet = CalcSheetStub("Sheet1")
    doc = CalcDocStub(sheets=[sheet], url="file:///skip.ods")

    from plugin.calc.python.workbook_state import WorkbookState

    st = WorkbookState.for_doc(doc)
    assert st is not None

    # Formula at (0, 0) is deleted
    sheet.getCellByPosition(0, 0).setFormula("")

    # Cell (0, 1): now has =SUM(1)
    sheet.getCellByPosition(0, 1).setFormula("=SUM(1)")

    # Cell (0, 2): edited from 2.0 to 999.0
    sheet.getCellByPosition(0, 2).setValue(999.0)

    # Cell (0, 3): still matches 3.0
    sheet.getCellByPosition(0, 3).setValue(3.0)

    key = (st.key, "Sheet1", 0, 0)
    SPILL_REGISTRY[key] = _spill.SpillRecord(code="result = [1, 2, 3]", cells=[_spill.SpillCell(1, 0, "n", 1.0), _spill.SpillCell(2, 0, "n", 2.0), _spill.SpillCell(3, 0, "n", 3.0)])

    _fire_sheet_modify(MagicMock(), doc, sheet)

    assert key not in SPILL_REGISTRY
    # Formula cell preserved
    assert sheet.getCellByPosition(0, 1).getFormula() == "=SUM(1)"
    # Edited cell preserved
    assert sheet.getCellByPosition(0, 2).getValue() == 999.0
    # Matching cell cleared
    assert sheet.getCellByPosition(0, 3).getValue() in (0, 0.0)


def test_spill_clear_skips_user_text_matching_numeric_spill(monkeypatch: pytest.MonkeyPatch) -> None:
    """Spilled 0.0 does not clear neighbor setString('note'), while neighbor setValue(0.0) is cleared."""
    from plugin.tests.testing_utils import CalcSheetStub

    monkeypatch.setattr("plugin.framework.queue_executor.main.is_current", lambda: True)
    sheet = CalcSheetStub("Sheet1")
    doc = CalcDocStub(sheets=[sheet], url="file:///text_zero.ods")

    from plugin.calc.python.workbook_state import WorkbookState

    st = WorkbookState.for_doc(doc)
    assert st is not None

    # Formula cell cleared
    sheet.getCellByPosition(0, 0).setFormula("")

    # Neighbor 1 has user text "note"
    sheet.getCellByPosition(0, 1).setString("note")

    # Neighbor 2 has numeric 0.0
    sheet.getCellByPosition(0, 2).setValue(0.0)

    key = (st.key, "Sheet1", 0, 0)
    SPILL_REGISTRY[key] = _spill.SpillRecord(code="result = [0.0, 0.0]", cells=[_spill.SpillCell(1, 0, "n", 0.0), _spill.SpillCell(2, 0, "n", 0.0)])

    _fire_sheet_modify(MagicMock(), doc, sheet)

    assert key not in SPILL_REGISTRY
    assert sheet.getCellByPosition(0, 1).getString() == "note"
    assert sheet.getCellByPosition(0, 2).getType() == 0
    assert sheet.getCellByPosition(0, 2).getString() == ""


def test_cell_matches_stored_value_type_gate() -> None:
    """Direct _cell_matches_stored_value cases for VALUE/TEXT gate and PyUNO enum compatibility."""
    from plugin.calc.python.spill import SpillCell, _cell_matches_stored_value
    from plugin.tests.testing_utils import CalcSheetStub

    sheet = CalcSheetStub("Sheet1")
    sheet.getCellByPosition(0, 0).setValue(0.0)
    assert _cell_matches_stored_value(sheet, 0, 0, SpillCell(0, 0, "n", 0.0)) is True

    sheet.getCellByPosition(0, 1).setString("0.0")
    assert _cell_matches_stored_value(sheet, 1, 0, SpillCell(1, 0, "n", 0.0)) is False

    class _PyUnoEnum:
        def __init__(self, value: str) -> None:
            self.value = value

        def __eq__(self, other: object) -> bool:
            return isinstance(other, _PyUnoEnum) and self.value == other.value

    class _StandInCell:
        def __init__(self, enum_val: str, value: float = 0.0, text: str = "") -> None:
            self._type = _PyUnoEnum(enum_val)
            self._value = value
            self._text = text

        def getType(self) -> _PyUnoEnum:
            return self._type

        def getValue(self) -> float:
            return self._value

        def getString(self) -> str:
            return self._text

    class _StandInSheet:
        def __init__(self, cell: _StandInCell) -> None:
            self._cell = cell

        def getCellByPosition(self, _col: int, _row: int) -> _StandInCell:
            return self._cell

    # Stand-in whose getType returns object with .value == "VALUE" and __eq__ false for ints
    val_cell = _StandInCell("VALUE", value=123.0, text="123.0")
    val_sheet = _StandInSheet(val_cell)
    assert _cell_matches_stored_value(val_sheet, 0, 0, SpillCell(0, 0, "n", 123.0)) is True
    assert _cell_matches_stored_value(val_sheet, 0, 0, SpillCell(0, 0, "s", "123.0")) is False

    # The same stand-in with .value == "TEXT" and getString() equal to the stored string matches kind "s"
    text_cell = _StandInCell("TEXT", value=0.0, text="hello")
    text_sheet = _StandInSheet(text_cell)
    assert _cell_matches_stored_value(text_sheet, 0, 0, SpillCell(0, 0, "s", "hello")) is True

    # A VALUE stand-in whose getString() equals a stored string does not match kind "s"
    val_with_str = _StandInCell("VALUE", value=0.0, text="target")
    val_str_sheet = _StandInSheet(val_with_str)
    assert _cell_matches_stored_value(val_str_sheet, 0, 0, SpillCell(0, 0, "s", "target")) is False


def test_cell_matches_stored_value_err() -> None:
    fake_cell_519 = SimpleNamespace(getError=lambda: 519)
    fake_cell_no_err = SimpleNamespace()
    rec_519 = _spill.SpillCell(row=0, col=0, kind="err", value=519)

    class FakeSheet:
        def __init__(self, cell):
            self._cell = cell

        def getCellByPosition(self, col, row):
            return self._cell

    sheet_519 = FakeSheet(fake_cell_519)
    assert _spill._cell_matches_stored_value(sheet_519, 0, 0, rec_519) is True

    rec_532 = _spill.SpillCell(row=0, col=0, kind="err", value=532)
    assert _spill._cell_matches_stored_value(sheet_519, 0, 0, rec_532) is False

    sheet_no_err = FakeSheet(fake_cell_no_err)
    assert _spill._cell_matches_stored_value(sheet_no_err, 0, 0, rec_519) is False


def test_load_spill_registry_skips_malformed_entry(monkeypatch: pytest.MonkeyPatch) -> None:
    import json

    payload = json.dumps({"Sheet1:1,1": [[2, 1]], "Sheet1:x,1": [[0, 0]], "Sheet1:4,4": {"code": "later", "cells": [{"r": 5, "c": 4, "t": "n", "v": 1.0}]}})
    monkeypatch.setattr("plugin.calc.python.spill.get_document_property", lambda model, name, default=None: payload if name == "WriterAgentSpillRegistry" else default)
    doc = CalcDocStub(url="file:///fake_malformed.ods")

    reset_calc_workbook_states()
    from plugin.calc.python.workbook_state import WorkbookState

    st = WorkbookState.for_doc(doc)
    assert st is not None
    _spill.load_spill_registry_for_doc(doc, st)
    doc_key = st.key
    assert (doc_key, "Sheet1", 1, 1) not in SPILL_REGISTRY
    assert (doc_key, "Sheet1", 4, 4) in SPILL_REGISTRY
    assert len([k for k in SPILL_REGISTRY if k[0] == doc_key]) == 1


def test_spill_registry_doc_key_uses_runtime_uid_ladder() -> None:
    from plugin.calc.python.workbook_state import WorkbookState

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
    from plugin.calc.python.workbook_state import WorkbookState

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
