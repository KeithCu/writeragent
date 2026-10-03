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
    assert second.doc_url == "file:///saved.ods"
    assert SHEET_MODIFY_LISTENERS[("uid-saveas", "Sheet1")] is first


def test_url_keyed_listener_is_reused_after_identity_change():
    doc = CalcDocStub(url="file:///new.ods", props={"RuntimeUID": "uid-migrate"})
    sheet = doc.getSheets().getByName("Sheet1")
    stale = SheetModifyDispatcher("ctx", doc, "file:///old.ods", "Sheet1", "file:///old.ods")
    sheet.addModifyListener(stale)
    SHEET_MODIFY_LISTENERS[("file:///old.ods", "Sheet1")] = stale
    got = ensure_sheet_modify_listener("ctx", doc, sheet)
    assert got is stale
    assert len(sheet._modify_listeners) == 1
    assert got.doc_url == "file:///new.ods"
    assert got.doc_identity == "uid-migrate"
    assert ("file:///old.ods", "Sheet1") not in SHEET_MODIFY_LISTENERS
    assert SHEET_MODIFY_LISTENERS[("uid-migrate", "Sheet1")] is stale


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
