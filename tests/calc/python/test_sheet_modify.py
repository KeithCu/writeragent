# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

from types import SimpleNamespace

import pytest

from plugin.calc.python.sheet_modify import (
    SheetModifyDispatcher,
    dispatch_sheet_modified,
    ensure_sheet_modify_listener,
    flush_sheet_modify_pass_for_tests,
    migrate_sheet_rename,
    reset_sheet_modify_runtime_for_tests,
    run_sheet_modify_pass,
    schedule_sheet_modify_pass,
)
from plugin.calc.python.spill import (
    SpillCell,
    SpillRecord,
    perform_deferred_spill,
)
from plugin.calc.python.workbook_state import WorkbookState
from tests.calc.python.calc_workbook_test_access import (
    SHEET_MODIFY_LISTENERS,
    SPILL_REGISTRY,
    reset_calc_workbook_states,
)
from plugin.tests.testing_utils import CalcDocStub, CalcSheetStub


@pytest.fixture(autouse=True)
def _reset_sheet_listeners():

    reset_sheet_modify_runtime_for_tests()
    reset_calc_workbook_states()
    yield
    reset_sheet_modify_runtime_for_tests()
    reset_calc_workbook_states()


def test_unsaved_workbooks_get_separate_listeners():
    doc_a = CalcDocStub(url="", props={"RuntimeUID": "uid-a"})
    doc_b = CalcDocStub(url="", props={"RuntimeUID": "uid-b"})
    sheet_a = doc_a.getSheets().getByName("Sheet1")
    sheet_b = doc_b.getSheets().getByName("Sheet1")
    first = ensure_sheet_modify_listener("ctx", doc_a, sheet_a)
    second = ensure_sheet_modify_listener("ctx", doc_b, sheet_b)
    assert first is not second
    assert len(sheet_a._modify_listeners) == 1
    assert len(sheet_b._modify_listeners) == 1
    assert ("uid-a", "Sheet1") in SHEET_MODIFY_LISTENERS
    assert ("uid-b", "Sheet1") in SHEET_MODIFY_LISTENERS


def test_save_as_reuses_listener():
    doc = CalcDocStub(url="", props={"RuntimeUID": "uid-saveas"})
    sheet = doc.getSheets().getByName("Sheet1")
    first = ensure_sheet_modify_listener("ctx", doc, sheet)
    doc.url = "file:///saved.ods"
    second = ensure_sheet_modify_listener("ctx", doc, sheet)
    assert first is second
    assert len(sheet._modify_listeners) == 1
    assert SHEET_MODIFY_LISTENERS[("uid-saveas", "Sheet1")] is first


def test_url_keyed_listener_is_ignored_after_identity_change():
    doc = CalcDocStub(url="file:///new.ods", props={"RuntimeUID": "uid-migrate"})
    sheet = doc.getSheets().getByName("Sheet1")
    stale = SheetModifyDispatcher("ctx", doc, "Sheet1")
    sheet.addModifyListener(stale)
    SHEET_MODIFY_LISTENERS[("file:///old.ods", "Sheet1")] = stale
    got = ensure_sheet_modify_listener("ctx", doc, sheet)
    assert got is not stale
    assert ("uid-migrate", "Sheet1") in SHEET_MODIFY_LISTENERS


def test_modify_uses_owning_document_not_active(monkeypatch):
    owner = CalcDocStub(url="file:///owner.ods", props={"RuntimeUID": "uid-owner"})
    other = CalcDocStub(url="file:///other.ods", props={"RuntimeUID": "uid-other"})
    sheet = owner.getSheets().getByName("Sheet1")
    sheet.getParent = lambda: owner
    monkeypatch.setattr("plugin.framework.queue_executor.main.is_current", lambda: True)
    monkeypatch.setattr("plugin.calc.python.geometric_recalc.is_geometric_repairing", lambda: False)
    captured: dict[str, object] = {}

    def _capture(ctx, doc, sheet, **kwargs):
        captured["doc"] = doc

    monkeypatch.setattr("plugin.calc.python.sheet_modify.schedule_sheet_modify_pass", _capture)
    listener = SheetModifyDispatcher("ctx", other, "Sheet1")
    listener.modified(SimpleNamespace(Source=sheet))
    assert captured["doc"] is owner


def test_modify_uses_stored_document_when_source_has_no_parent(monkeypatch):
    owner = CalcDocStub(url="file:///stored.ods", props={"RuntimeUID": "uid-stored"})
    sheet = owner.getSheets().getByName("Sheet1")
    monkeypatch.setattr("plugin.framework.queue_executor.main.is_current", lambda: True)
    monkeypatch.setattr("plugin.calc.python.geometric_recalc.is_geometric_repairing", lambda: False)
    captured: dict[str, object] = {}

    def _capture(ctx, doc, sheet, **kwargs):
        captured["doc"] = doc

    monkeypatch.setattr("plugin.calc.python.sheet_modify.schedule_sheet_modify_pass", _capture)
    listener = ensure_sheet_modify_listener("ctx", owner, sheet)
    listener.modified(SimpleNamespace(Source=sheet))
    assert captured["doc"] is owner


def test_debounce_keys_differ_for_unsaved_workbooks():
    from plugin.calc.python.workbook_state import WorkbookState

    doc_a = CalcDocStub(url="", props={"RuntimeUID": "uid-timer-a"})
    doc_b = CalcDocStub(url="", props={"RuntimeUID": "uid-timer-b"})
    sheet_a = doc_a.getSheets().getByName("Sheet1")
    sheet_b = doc_b.getSheets().getByName("Sheet1")
    schedule_sheet_modify_pass("ctx", doc_a, sheet_a)
    schedule_sheet_modify_pass("ctx", doc_b, sheet_b)
    state_a = WorkbookState.for_doc(doc_a)
    state_b = WorkbookState.for_doc(doc_b)
    assert state_a is not None and "sheet:Sheet1" in state_a.debounce.pending_for_tests()
    assert state_b is not None and "sheet:Sheet1" in state_b.debounce.pending_for_tests()
    assert state_a is not state_b


def test_unload_drops_lifecycle_keyed_listener():
    doc = CalcDocStub(url="", props={"RuntimeUID": "uid-unload"})
    sheet = doc.getSheets().getByName("Sheet1")
    ensure_sheet_modify_listener("ctx", doc, sheet)
    assert ("uid-unload", "Sheet1") in SHEET_MODIFY_LISTENERS
    WorkbookState.forget_key("uid-unload")
    assert ("uid-unload", "Sheet1") not in SHEET_MODIFY_LISTENERS


def test_rename_migrates_spill_registry_and_listener(monkeypatch):
    doc = CalcDocStub(url="file:///rename.ods", props={"RuntimeUID": "uid-rename"})
    sheet = doc.getSheets().getByName("Sheet1")
    sheet.getParent = lambda: doc
    listener = ensure_sheet_modify_listener("ctx", doc, sheet)
    assert ("uid-rename", "Sheet1") in SHEET_MODIFY_LISTENERS

    cell_formula = sheet.getCellByPosition(0, 0)
    cell_formula.setFormula('=PY("result = [1, 2]")')
    cell_neighbor = sheet.getCellByPosition(1, 0)
    cell_neighbor.setValue(2.0)

    SPILL_REGISTRY[("uid-rename", "Sheet1", 0, 0)] = SpillRecord(
        code='result = [1, 2]',
        cells=[SpillCell(0, 1, "n", 2.0)],
    )

    sheet.setName("SheetRenamed")
    doc.getSheets()._sheets["SheetRenamed"] = sheet
    doc.getSheets()._sheets.pop("Sheet1", None)

    monkeypatch.setattr("plugin.framework.queue_executor.main.is_current", lambda: True)
    monkeypatch.setattr("plugin.calc.python.geometric_recalc.is_geometric_repairing", lambda: False)
    listener.modified(SimpleNamespace(Source=sheet))

    assert ("uid-rename", "Sheet1") not in SHEET_MODIFY_LISTENERS
    assert ("uid-rename", "SheetRenamed") in SHEET_MODIFY_LISTENERS
    assert ("uid-rename", "Sheet1", 0, 0) not in SPILL_REGISTRY
    assert ("uid-rename", "SheetRenamed", 0, 0) in SPILL_REGISTRY
    assert SPILL_REGISTRY[("uid-rename", "SheetRenamed", 0, 0)].spills == [(0, 1)]
    assert listener.sheet_name == "SheetRenamed"

    # Now clear formula cell and run modify pass
    cell_formula.setFormula("")
    cell_formula.setString("")
    flush_sheet_modify_pass_for_tests("ctx", doc, sheet, sheet_name="SheetRenamed")

    assert ("uid-rename", "SheetRenamed", 0, 0) not in SPILL_REGISTRY
    assert cell_neighbor.getValue() == 0.0 or cell_neighbor.getString() == ""


def test_rename_without_identity_does_not_touch_other_workbooks():
    SPILL_REGISTRY[("uid-other", "Sheet1", 0, 0)] = SpillRecord(
        code="result = 1",
        cells=[SpillCell(0, 1, "n", 1.0)],
    )

    doc_no_id = CalcDocStub(url="", props={})
    migrate_sheet_rename(doc_no_id, "Sheet1", "Sheet2")

    assert ("uid-other", "Sheet1", 0, 0) in SPILL_REGISTRY
    assert ("uid-other", "Sheet2", 0, 0) not in SPILL_REGISTRY


def test_perform_deferred_spill_does_not_schedule_modify(monkeypatch):
    """While perform_deferred_spill runs, sheet-modify dispatch depth is non-zero, so no modify pass is scheduled."""
    doc = CalcDocStub(url="file:///spill_dispatch.ods", props={"RuntimeUID": "uid-spill-dispatch"})
    sheet = doc.getSheets().getByName("Sheet1")
    sheet.getParent = lambda: doc
    sheet.getCellByPosition(0, 0).setFormula('=PY("result = [1, 2]")')
    WorkbookState.for_doc(doc).spills[("Sheet1", 0, 0)] = SpillRecord(
        code="result = [1, 2]",
        cells=[SpillCell(0, 2, "n", 9.0)],
    )

    scheduled_calls = 0
    hook_calls = 0

    def mock_schedule(*args, **kwargs):
        nonlocal scheduled_calls
        scheduled_calls += 1

    monkeypatch.setattr("plugin.calc.python.sheet_modify.schedule_sheet_modify_pass", mock_schedule)
    monkeypatch.setattr("plugin.framework.queue_executor.main.is_current", lambda: True)

    old_neighbor = sheet.getCellByPosition(2, 0)
    orig_clear = old_neighbor.clearContents

    def clear_and_dispatch(flags):
        nonlocal hook_calls
        hook_calls += 1
        dispatch_sheet_modified("ctx", "Sheet1", SimpleNamespace(Source=sheet), doc)
        return orig_clear(flags)

    old_neighbor.clearContents = clear_and_dispatch

    perform_deferred_spill(
        "Sheet1",
        0,
        0,
        [[1, 2]],
        doc=doc,
        sheet=sheet,
        code="result = [1, 2]",
        key="uid-spill-dispatch",
    )

    assert hook_calls == 1
    assert scheduled_calls == 0


def test_schedule_timer_callback_clears_pending_timer() -> None:
    """Timer callback removes itself on fire."""
    from plugin.calc.python.workbook_state import WorkbookState

    doc = CalcDocStub(url="", props={"RuntimeUID": "uid-fire-timer"})
    sheet = doc.getSheets().getByName("Sheet1")

    schedule_sheet_modify_pass("ctx", doc, sheet)
    state = WorkbookState.for_doc(doc)
    assert state is not None
    assert "sheet:Sheet1" in state.debounce.pending_for_tests()

    # Fire / flush
    state.debounce.flush_for_tests("sheet:Sheet1")
    assert "sheet:Sheet1" not in state.debounce.pending_for_tests()


def test_rekeyed_pending_timer_drops_on_fire(monkeypatch: pytest.MonkeyPatch) -> None:
    """Rekeyed pending timer is dropped on fire and runs pass for new sheet name."""
    from plugin.calc.python.workbook_state import WorkbookState

    doc = CalcDocStub(url="", props={"RuntimeUID": "uid-rekey-timer"})
    sheet = doc.getSheets().getByName("Sheet1")
    ensure_sheet_modify_listener("ctx", doc, sheet)

    schedule_sheet_modify_pass("ctx", doc, sheet)
    state = WorkbookState.for_doc(doc)
    assert state is not None
    assert "sheet:Sheet1" in state.debounce.pending_for_tests()

    migrate_sheet_rename(doc, "Sheet1", "SheetNew")
    assert "sheet:Sheet1" not in state.debounce.pending_for_tests()
    assert "sheet:SheetNew" in state.debounce.pending_for_tests()

    passed_kwargs: dict[str, object] = {}

    def fake_run_sheet_modify_pass(ctx, d, s, *, sheet_name=""):
        passed_kwargs["sheet_name"] = sheet_name

    monkeypatch.setattr(
        "plugin.calc.python.sheet_modify.run_sheet_modify_pass",
        fake_run_sheet_modify_pass,
    )
    state.debounce.flush_for_tests("sheet:SheetNew")
    assert "sheet:SheetNew" not in state.debounce.pending_for_tests()
    assert passed_kwargs.get("sheet_name") == "SheetNew"


def test_unload_drops_pending_sheet_timer() -> None:
    from plugin.calc.python.workbook_state import WorkbookState

    doc_unload = CalcDocStub(url="", props={"RuntimeUID": "uid-unload"})
    sheet_unload = doc_unload.getSheets().getByName("Sheet1")
    doc_keep = CalcDocStub(url="", props={"RuntimeUID": "uid-keep"})
    sheet_keep = doc_keep.getSheets().getByName("Sheet1")

    schedule_sheet_modify_pass("ctx", doc_unload, sheet_unload)
    schedule_sheet_modify_pass("ctx", doc_keep, sheet_keep)

    state_unload = WorkbookState.for_doc(doc_unload)
    state_keep = WorkbookState.for_doc(doc_keep)
    assert state_unload is not None and "sheet:Sheet1" in state_unload.debounce.pending_for_tests()
    assert state_keep is not None and "sheet:Sheet1" in state_keep.debounce.pending_for_tests()

    WorkbookState.forget_key("uid-unload")

    assert "sheet:Sheet1" not in state_unload.debounce.pending_for_tests()
    assert "sheet:Sheet1" in state_keep.debounce.pending_for_tests()


def test_posted_pass_with_unreadable_sheet_name_skips_cleanup(monkeypatch: pytest.MonkeyPatch) -> None:
    doc = CalcDocStub(url="file:///gone.ods", props={"RuntimeUID": "uid-gone"})
    sheet = doc.getSheets().getByName("Sheet1")
    sheet.getName = lambda: ""
    monkeypatch.setattr("plugin.framework.queue_executor.main.is_current", lambda: True)
    monkeypatch.setattr("plugin.calc.python.geometric_recalc.is_geometric_repairing", lambda: False)

    cleanup_calls: list[tuple[tuple, dict]] = []

    def fail_cleanup(*args, **kwargs):
        cleanup_calls.append((args, kwargs))

    monkeypatch.setattr("plugin.calc.python.spill.cleanup_orphans", fail_cleanup)
    run_sheet_modify_pass("ctx", doc, sheet)
    assert cleanup_calls == []


def test_rename_of_one_sheet_keeps_other_sheets_spills(monkeypatch: pytest.MonkeyPatch) -> None:
    import plugin.calc.python.sheet_modify as sm

    sheet1 = CalcSheetStub("Sheet1")
    sheet2 = CalcSheetStub("Sheet2")
    doc = CalcDocStub(sheets=[sheet1, sheet2], props={"RuntimeUID": "uid-rename-keep"})
    sheet1.getCellByPosition(0, 0).setFormula('=PY("x")')
    state = WorkbookState.for_doc(doc)
    assert state is not None
    state.spills[("Sheet1", 0, 0)] = SpillRecord(code="x", cells=[SpillCell(1, 0, "n", 2.0)])

    ensure_sheet_modify_listener("ctx", doc, sheet1)
    ensure_sheet_modify_listener("ctx", doc, sheet2)
    schedule_sheet_modify_pass("ctx", doc, sheet2)
    migrate_sheet_rename(doc, "Sheet2", "Renamed")

    seen: list[str] = []
    real_run = sm.run_sheet_modify_pass

    def _spy(ctx, d, s, *, sheet_name=""):
        seen.append(sheet_name)
        return real_run(ctx, d, s, sheet_name=sheet_name)

    monkeypatch.setattr(sm, "run_sheet_modify_pass", _spy)
    monkeypatch.setattr("plugin.framework.queue_executor.main.is_current", lambda: True)
    monkeypatch.setattr("plugin.calc.python.geometric_recalc.geometric_flag_enabled", lambda: False)
    monkeypatch.setattr("plugin.calc.python.geometric_recalc.is_geometric_repairing", lambda: False)

    state.debounce.flush_for_tests("sheet:Renamed")
    assert seen == ["Renamed"]
    assert ("Sheet1", 0, 0) in state.spills



