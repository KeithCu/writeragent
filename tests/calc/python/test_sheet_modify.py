# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

from types import SimpleNamespace

import pytest

from plugin.calc.python.function import SHEET_MODIFY_LISTENERS, clear_in_memory_spill_state
from plugin.calc.python.sheet_modify import (
    SheetModifyDispatcher,
    _PENDING_TIMERS,
    ensure_sheet_modify_listener,
    reset_sheet_modify_runtime_for_tests,
)
from plugin.tests.testing_utils import CalcDocStub


class _FakeTimer:
    def __init__(self, interval, function, *args, **kwargs):
        self.interval = interval
        self.function = function
        self.cancelled = False

    def start(self) -> None:
        return None

    def cancel(self) -> None:
        self.cancelled = True


@pytest.fixture(autouse=True)
def _reset_sheet_listeners():
    import plugin.calc.python.function as python_function

    reset_sheet_modify_runtime_for_tests()
    SHEET_MODIFY_LISTENERS.clear()
    with python_function._PENDING_SPILL_LOCK:
        python_function._PENDING_SPILL_TIMERS.clear()
    yield
    reset_sheet_modify_runtime_for_tests()
    SHEET_MODIFY_LISTENERS.clear()
    with python_function._PENDING_SPILL_LOCK:
        python_function._PENDING_SPILL_TIMERS.clear()


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
    assert second.doc_key == "file:///saved.ods"
    assert SHEET_MODIFY_LISTENERS[("uid-saveas", "Sheet1")] is first


def test_url_keyed_listener_is_ignored_after_identity_change():
    doc = CalcDocStub(url="file:///new.ods", props={"RuntimeUID": "uid-migrate"})
    sheet = doc.getSheets().getByName("Sheet1")
    stale = SheetModifyDispatcher("ctx", doc, "file:///old.ods", "Sheet1", "file:///old.ods")
    sheet.addModifyListener(stale)
    SHEET_MODIFY_LISTENERS[("file:///old.ods", "Sheet1")] = stale
    got = ensure_sheet_modify_listener("ctx", doc, sheet)
    assert got is not stale
    assert got.doc_identity == "uid-migrate"
    assert ("uid-migrate", "Sheet1") in SHEET_MODIFY_LISTENERS


def test_modify_uses_owning_document_not_active(monkeypatch):
    owner = CalcDocStub(url="file:///owner.ods", props={"RuntimeUID": "uid-owner"})
    other = CalcDocStub(url="file:///other.ods", props={"RuntimeUID": "uid-other"})
    sheet = owner.getSheets().getByName("Sheet1")
    sheet.getParent = lambda: owner
    monkeypatch.setattr("plugin.framework.thread_guard.on_main_thread", lambda: True)
    monkeypatch.setattr("plugin.calc.python.geometric_recalc.is_geometric_repairing", lambda: False)
    monkeypatch.setattr("plugin.calc.python.function._get_calc_doc", lambda _ctx: other)
    captured: dict[str, object] = {}

    def _capture(ctx, doc, sheet, **kwargs):
        captured["doc"] = doc
        captured["doc_url"] = kwargs.get("doc_url")

    monkeypatch.setattr("plugin.calc.python.sheet_modify.schedule_sheet_modify_pass", _capture)
    listener = ensure_sheet_modify_listener("ctx", owner, sheet)
    listener.modified(SimpleNamespace(Source=sheet))
    assert captured["doc"] is owner
    assert captured["doc_url"] == "file:///owner.ods"


def test_modify_uses_stored_document_when_source_has_no_parent(monkeypatch):
    owner = CalcDocStub(url="file:///stored.ods", props={"RuntimeUID": "uid-stored"})
    other = CalcDocStub(url="file:///active.ods", props={"RuntimeUID": "uid-active"})
    sheet = owner.getSheets().getByName("Sheet1")
    monkeypatch.setattr("plugin.framework.thread_guard.on_main_thread", lambda: True)
    monkeypatch.setattr("plugin.calc.python.geometric_recalc.is_geometric_repairing", lambda: False)
    monkeypatch.setattr("plugin.calc.python.function._get_calc_doc", lambda _ctx: other)
    captured: dict[str, object] = {}

    def _capture(ctx, doc, sheet, **kwargs):
        captured["doc"] = doc

    monkeypatch.setattr("plugin.calc.python.sheet_modify.schedule_sheet_modify_pass", _capture)
    listener = ensure_sheet_modify_listener("ctx", owner, sheet)
    listener.modified(SimpleNamespace(Source=sheet))
    assert captured["doc"] is owner


def test_debounce_keys_differ_for_unsaved_workbooks(monkeypatch):
    monkeypatch.setattr("plugin.calc.python.function.threading.Timer", _FakeTimer)
    from plugin.calc.python.sheet_modify import schedule_sheet_modify_pass

    doc_a = CalcDocStub(url="", props={"RuntimeUID": "uid-timer-a"})
    doc_b = CalcDocStub(url="", props={"RuntimeUID": "uid-timer-b"})
    sheet_a = doc_a.getSheets().getByName("Sheet1")
    sheet_b = doc_b.getSheets().getByName("Sheet1")
    schedule_sheet_modify_pass("ctx", doc_a, sheet_a)
    schedule_sheet_modify_pass("ctx", doc_b, sheet_b)
    assert set(_PENDING_TIMERS) == {("uid-timer-a", "Sheet1"), ("uid-timer-b", "Sheet1")}


def test_unload_drops_lifecycle_keyed_listener():
    doc = CalcDocStub(url="", props={"RuntimeUID": "uid-unload"})
    sheet = doc.getSheets().getByName("Sheet1")
    ensure_sheet_modify_listener("ctx", doc, sheet)
    assert ("uid-unload", "Sheet1") in SHEET_MODIFY_LISTENERS
    clear_in_memory_spill_state(lifecycle_key="uid-unload")
    assert ("uid-unload", "Sheet1") not in SHEET_MODIFY_LISTENERS


def test_rename_migrates_spill_registry_and_listener(monkeypatch):
    from plugin.calc.python.function import SPILL_REGISTRY
    from plugin.calc.python.sheet_modify import flush_sheet_modify_pass_for_tests

    doc = CalcDocStub(url="file:///rename.ods", props={"RuntimeUID": "uid-rename"})
    sheet = doc.getSheets().getByName("Sheet1")
    sheet.getParent = lambda: doc
    listener = ensure_sheet_modify_listener("ctx", doc, sheet)
    assert ("uid-rename", "Sheet1") in SHEET_MODIFY_LISTENERS

    cell_formula = sheet.getCellByPosition(0, 0)
    cell_formula.setFormula('=PY("result = [1, 2]")')
    cell_neighbor = sheet.getCellByPosition(1, 0)
    cell_neighbor.setValue(2.0)
    from plugin.calc.python.function import SpillCell, SpillRecord

    SPILL_REGISTRY[("uid-rename", "Sheet1", 0, 0)] = SpillRecord(
        code='result = [1, 2]',
        cells=[SpillCell(0, 1, "n", 2.0)],
    )

    sheet.setName("SheetRenamed")
    doc.getSheets()._sheets["SheetRenamed"] = sheet
    doc.getSheets()._sheets.pop("Sheet1", None)

    monkeypatch.setattr("plugin.framework.thread_guard.on_main_thread", lambda: True)
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
    flush_sheet_modify_pass_for_tests("ctx", doc, sheet, doc_url="file:///rename.ods", sheet_name="SheetRenamed")

    assert ("uid-rename", "SheetRenamed", 0, 0) not in SPILL_REGISTRY
    assert cell_neighbor.getValue() == 0.0 or cell_neighbor.getString() == ""


def test_rename_without_identity_does_not_touch_other_workbooks():
    from plugin.calc.python.function import SPILL_REGISTRY, SpillCell, SpillRecord
    from plugin.calc.python.sheet_modify import migrate_sheet_rename

    SPILL_REGISTRY[("uid-other", "Sheet1", 0, 0)] = SpillRecord(
        code="result = 1",
        cells=[SpillCell(0, 1, "n", 1.0)],
    )

    doc_no_id = CalcDocStub(url="", props={})
    migrate_sheet_rename(doc_no_id, "Sheet1", "Sheet2")

    assert ("uid-other", "Sheet1", 0, 0) in SPILL_REGISTRY
    assert ("uid-other", "Sheet2", 0, 0) not in SPILL_REGISTRY


def test_perform_deferred_spill_does_not_schedule_modify(monkeypatch):
    """While perform_deferred_spill runs, is_sheet_modify_dispatching() is true so no modify pass is scheduled."""
    from plugin.calc.python.function import perform_deferred_spill
    from plugin.calc.python.sheet_modify import dispatch_sheet_modified

    doc = CalcDocStub(url="file:///spill_dispatch.ods", props={"RuntimeUID": "uid-spill-dispatch"})
    sheet = doc.getSheets().getByName("Sheet1")
    sheet.getParent = lambda: doc

    scheduled_calls = 0

    def mock_schedule(*args, **kwargs):
        nonlocal scheduled_calls
        scheduled_calls += 1

    monkeypatch.setattr("plugin.calc.python.sheet_modify.schedule_sheet_modify_pass", mock_schedule)
    monkeypatch.setattr("plugin.framework.thread_guard.on_main_thread", lambda: True)

    orig_clear = sheet.getCellByPosition(0, 0).clearContents

    def clear_and_dispatch(flags):
        dispatch_sheet_modified(SimpleNamespace(Source=sheet))
        return orig_clear(flags)

    cell = sheet.getCellByPosition(1, 0)
    cell.clearContents = clear_and_dispatch

    perform_deferred_spill(
        "ctx",
        "file:///spill_dispatch.ods",
        "Sheet1",
        0,
        0,
        [[1, 2]],
        doc=doc,
        sheet=sheet,
        code='=PY("result = [1, 2]")',
        lifecycle_key="uid-spill-dispatch",
    )

    assert scheduled_calls == 0


def test_schedule_timer_callback_clears_pending_timer(monkeypatch):
    """Timer callback removes itself from _PENDING_TIMERS on fire."""
    from plugin.calc.python.sheet_modify import schedule_sheet_modify_pass

    captured_callbacks: list = []

    class _CaptureTimer:
        def __init__(self, interval, function, *args, **kwargs):
            self.interval = interval
            self.function = function
            self.cancelled = False
            captured_callbacks.append(function)

        def start(self) -> None:
            pass

        def cancel(self) -> None:
            self.cancelled = True

    monkeypatch.setattr("plugin.calc.python.function.threading.Timer", _CaptureTimer)
    monkeypatch.setattr(
        "plugin.framework.queue_executor.post_to_main_thread",
        lambda fn, *a, **k: None,
    )

    doc = CalcDocStub(url="", props={"RuntimeUID": "uid-fire-timer"})
    sheet = doc.getSheets().getByName("Sheet1")
    key = ("uid-fire-timer", "Sheet1")

    schedule_sheet_modify_pass("ctx", doc, sheet)
    assert key in _PENDING_TIMERS
    assert len(captured_callbacks) == 1

    # Fire the callback
    captured_callbacks[0]()
    assert key not in _PENDING_TIMERS


def test_rekeyed_pending_timer_drops_on_fire(monkeypatch: pytest.MonkeyPatch) -> None:
    """Rekeyed pending timer is dropped on fire and posts pass for new sheet name."""
    from plugin.calc.python.sheet_modify import migrate_sheet_rename, schedule_sheet_modify_pass

    captured_callbacks: list = []
    posted_callables: list = []

    class _CaptureTimer:
        def __init__(self, interval, function, *args, **kwargs):
            self.interval = interval
            self.function = function
            self.cancelled = False
            captured_callbacks.append(function)

        def start(self) -> None:
            pass

        def cancel(self) -> None:
            self.cancelled = True

    monkeypatch.setattr("plugin.calc.python.function.threading.Timer", _CaptureTimer)
    monkeypatch.setattr(
        "plugin.framework.queue_executor.post_to_main_thread",
        lambda fn, *a, **k: posted_callables.append(fn),
    )

    doc = CalcDocStub(url="", props={"RuntimeUID": "uid-rekey-timer"})
    sheet = doc.getSheets().getByName("Sheet1")
    old_key = ("uid-rekey-timer", "Sheet1")
    new_key = ("uid-rekey-timer", "SheetNew")

    schedule_sheet_modify_pass("ctx", doc, sheet)
    assert old_key in _PENDING_TIMERS
    assert len(captured_callbacks) == 1

    migrate_sheet_rename(doc, "Sheet1", "SheetNew")
    assert old_key not in _PENDING_TIMERS
    assert new_key in _PENDING_TIMERS

    # Fire the callback
    captured_callbacks[0]()
    assert len(_PENDING_TIMERS) == 0
    assert len(posted_callables) == 1

    passed_kwargs: dict[str, object] = {}

    def fake_run_sheet_modify_pass(ctx, d, s, *, doc_url="", sheet_name=""):
        passed_kwargs["doc_url"] = doc_url
        passed_kwargs["sheet_name"] = sheet_name

    monkeypatch.setattr(
        "plugin.calc.python.sheet_modify.run_sheet_modify_pass",
        fake_run_sheet_modify_pass,
    )
    posted_callables[0]()
    assert passed_kwargs.get("sheet_name") == "SheetNew"


def test_unload_drops_pending_sheet_timer(monkeypatch: pytest.MonkeyPatch) -> None:
    from plugin.calc.python.sheet_modify import cancel_pending_sheet_timers, schedule_sheet_modify_pass

    monkeypatch.setattr("threading.Timer", _FakeTimer)
    monkeypatch.setattr("plugin.calc.python.function.threading.Timer", _FakeTimer)

    doc_unload = CalcDocStub(url="", props={"RuntimeUID": "uid-unload"})
    sheet_unload = doc_unload.getSheets().getByName("Sheet1")
    doc_keep = CalcDocStub(url="", props={"RuntimeUID": "uid-keep"})
    sheet_keep = doc_keep.getSheets().getByName("Sheet1")

    schedule_sheet_modify_pass("ctx", doc_unload, sheet_unload)
    schedule_sheet_modify_pass("ctx", doc_keep, sheet_keep)

    timer_unload = _PENDING_TIMERS.get(("uid-unload", "Sheet1"))
    timer_keep = _PENDING_TIMERS.get(("uid-keep", "Sheet1"))
    assert timer_unload is not None
    assert timer_keep is not None
    assert not timer_unload.cancelled
    assert not timer_keep.cancelled

    clear_in_memory_spill_state(lifecycle_key="uid-unload")

    assert ("uid-unload", "Sheet1") not in _PENDING_TIMERS
    assert timer_unload.cancelled is True
    assert ("uid-keep", "Sheet1") in _PENDING_TIMERS
    assert timer_keep.cancelled is False

    cancel_pending_sheet_timers("")
    assert ("uid-keep", "Sheet1") in _PENDING_TIMERS
    assert timer_keep.cancelled is False


