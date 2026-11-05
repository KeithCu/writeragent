# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from plugin.calc.python.spill import SpillRecord
import plugin.calc.python.workbook_lifecycle as lifecycle
from plugin.calc.python.workbook_state import WorkbookState
from plugin.calc.python.geometric_recalc import (
    EvalIndexKey,
    GeometricRecord,
    _RUNTIME,
    clear_in_memory_geometric_state,
    replace_geometric_strip_safe,
    reset_geometric_runtime_for_tests,
)
from plugin.calc.python.workbook_lifecycle import (
    _CalcPythonUnloadListener,
    ensure_calc_workbook_unload_resets_python,
    ensure_python_session_cleared_on_unload,
)
from plugin.tests.testing_utils import CalcDocStub


@pytest.fixture(autouse=True)
def _reset_lifecycle_registry():
    WorkbookState.reset_for_tests()
    lifecycle._LISTENERS.clear()
    yield
    WorkbookState.reset_for_tests()
    lifecycle._LISTENERS.clear()


def test_unload_forgets_cached_lifecycle_key():
    """Off-main spill lookup must not keep a closed workbook's id."""
    doc = CalcDocStub(props={"RuntimeUID": "uid-forget"})
    st = WorkbookState(key="uid-forget")
    WorkbookState._remember(doc, st)
    assert WorkbookState.known(doc) is not None
    listener = _CalcPythonUnloadListener(MagicMock(), "calc:wb-forget", st)
    with patch("plugin.calc.python.workbook_lifecycle.reset_python_session") as mock_reset:
        mock_reset.return_value = {"status": "ok"}
        listener.on_document_event(MagicMock(EventName="OnUnload"))
    assert WorkbookState.known(doc) is None
    assert WorkbookState.known(None) is None


def test_unload_listener_resets_worker_session():
    ctx = MagicMock()
    listener = _CalcPythonUnloadListener(ctx, "calc:wb-1", WorkbookState(key="key-1"))
    with patch("plugin.calc.python.workbook_lifecycle.reset_python_session") as mock_reset:
        mock_reset.return_value = {"status": "ok"}
        listener.on_document_event(MagicMock(EventName="OnUnload"))
        mock_reset.assert_called_once_with("calc:wb-1")
        listener.on_document_event(MagicMock(EventName="OnUnload"))
        mock_reset.assert_called_once()


def test_on_disposing_runs_teardown():
    ctx = MagicMock()
    listener = _CalcPythonUnloadListener(ctx, "calc:wb-disposing", WorkbookState(key="key-disposing"))
    with patch("plugin.calc.python.workbook_lifecycle.reset_python_session") as mock_reset:
        mock_reset.return_value = {"status": "ok"}
        listener.on_disposing(MagicMock())
        mock_reset.assert_called_once_with("calc:wb-disposing")
        listener.on_disposing(MagicMock())
        mock_reset.assert_called_once()


def test_non_calc_unload_resets_rps_and_notebook_sessions(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr("plugin.calc.python.workbook_lifecycle._HAVE_UNO_DOC_EVENTS", True)
    ctx = MagicMock()
    doc = MagicMock()
    doc.getPropertyValue.return_value = "writer-uid"

    ensure_python_session_cleared_on_unload(ctx, doc, "rps:file:///a.odt")
    ensure_python_session_cleared_on_unload(ctx, doc, "notebook:file:///a.odt")
    doc.addDocumentEventListener.assert_called_once()
    listener = doc.addDocumentEventListener.call_args[0][0]
    with patch("plugin.calc.python.workbook_lifecycle.reset_python_session") as mock_reset:
        mock_reset.return_value = {"status": "ok"}
        listener.on_document_event(MagicMock(EventName="OnUnload"))
    assert mock_reset.call_count == 2
    assert {call.args[0] for call in mock_reset.call_args_list} == {
        "rps:file:///a.odt",
        "notebook:file:///a.odt",
    }


def test_unload_clears_in_memory_spill_state():
    state = WorkbookState(key="key-spill")
    state.spills[("Sheet1", 0, 0)] = SpillRecord(code="", cells=[])
    state.loaded = True
    ctx = MagicMock()
    listener = _CalcPythonUnloadListener(ctx, "calc:file:///gone.ods", state)
    with patch("plugin.calc.python.workbook_lifecycle.reset_python_session") as mock_reset:
        mock_reset.return_value = {"status": "ok"}
        listener.on_document_event(MagicMock(EventName="OnUnload"))
    assert ("Sheet1", 0, 0) not in state.spills
    assert not state.loaded


def test_unload_clears_in_memory_geometric_state():
    reset_geometric_runtime_for_tests()
    key = "calc:file:///gone-geo.ods"
    _RUNTIME.records[(key, "Sheet1", "A2")] = GeometricRecord(predecessor="A1")
    replace_geometric_strip_safe(key, frozenset({EvalIndexKey(key, "x", 2)}))
    ctx = MagicMock()
    state = WorkbookState(key="key-geo")
    listener = _CalcPythonUnloadListener(ctx, key, state)
    with patch("plugin.calc.python.workbook_lifecycle.reset_python_session") as mock_reset:
        mock_reset.return_value = {"status": "ok"}
        listener.on_document_event(MagicMock(EventName="OnUnload"))
    assert (key, "Sheet1", "A2") not in _RUNTIME.records
    assert _RUNTIME.strip_safe == frozenset()
    clear_in_memory_geometric_state(workbook_key=key)


def test_ensure_registers_listener_once():
    ctx = MagicMock()
    doc = CalcDocStub(props={"RuntimeUID": "uid-reg"})
    with patch("plugin.calc.python.workbook_lifecycle._HAVE_UNO_DOC_EVENTS", True):
        ensure_calc_workbook_unload_resets_python(ctx, doc)
        ensure_calc_workbook_unload_resets_python(ctx, doc)
    assert len(doc._document_event_listeners) == 1


def test_unload_clears_registration_so_reopen_can_reregister():
    """After OnUnload the same RuntimeUID can register again (close then reopen)."""
    ctx = MagicMock()
    doc = CalcDocStub(props={"RuntimeUID": "uid-reopen"})
    with patch("plugin.calc.python.workbook_lifecycle._HAVE_UNO_DOC_EVENTS", True):
        with patch("plugin.calc.python.workbook_lifecycle.reset_python_session") as mock_reset:
            mock_reset.return_value = {"status": "ok"}
            ensure_calc_workbook_unload_resets_python(ctx, doc)
            assert "uid-reopen" in lifecycle._LISTENERS
            doc._document_event_listeners[0].on_document_event(MagicMock(EventName="OnUnload"))
            assert "uid-reopen" not in lifecycle._LISTENERS
            mock_reset.assert_called_once()

            ensure_calc_workbook_unload_resets_python(ctx, doc)
            assert len(doc._document_event_listeners) == 2
            assert "uid-reopen" in lifecycle._LISTENERS


def test_save_notes_new_session_id_reset_on_unload():
    """After Save the listener must reset both the uuid kernel and the file-URL kernel."""
    ctx = MagicMock()
    doc = CalcDocStub(props={"RuntimeUID": "uid-save"}, url="")
    with patch("plugin.calc.python.workbook_lifecycle._HAVE_UNO_DOC_EVENTS", True):
        with patch("plugin.calc.python.workbook_lifecycle.calc_workbook_base_session_id", side_effect=["calc:uuid-1", "calc:file:///saved.ods"]):
            with patch("plugin.calc.python.workbook_lifecycle.reset_python_session") as mock_reset:
                mock_reset.return_value = {"status": "ok"}
                ensure_calc_workbook_unload_resets_python(ctx, doc)
                doc.url = "file:///saved.ods"
                ensure_calc_workbook_unload_resets_python(ctx, doc)
                assert len(doc._document_event_listeners) == 1
                doc._document_event_listeners[0].on_document_event(MagicMock(EventName="OnUnload"))
    reset_ids = {call.args[0] for call in mock_reset.call_args_list}
    assert reset_ids == {"calc:uuid-1", "calc:file:///saved.ods"}
    assert "uid-save" not in lifecycle._LISTENERS


def test_unload_resets_worker_when_busy():
    ctx = MagicMock()
    listener = _CalcPythonUnloadListener(ctx, "calc:wb-1", WorkbookState(key="key-busy"))

    seen = []
    def mock_reset_side_effect(sid):
        seen.append(sid)
        if len(seen) == 1:
            return {"status": "error", "code": "WORKER_REENTRY"}
        return {"status": "ok"}

    with patch("plugin.calc.python.workbook_lifecycle.reset_python_session", side_effect=mock_reset_side_effect):
        with patch("plugin.framework.worker_pool.run_in_background") as mock_run_in_background:
            listener.on_document_event(MagicMock(EventName="OnUnload"))
            # Initial call
            assert len(seen) == 1
            # Ensure background fallback was scheduled with dedicated=True
            mock_run_in_background.assert_called_once()
            assert mock_run_in_background.call_args[1].get("dedicated") is True
            callback = mock_run_in_background.call_args[0][0]
            with patch("time.sleep"):
                callback()
            assert len(seen) == 2
    assert listener._teardown_done is True


def test_retry_reset_logs_warning_on_timeout():
    ctx = MagicMock()
    listener = _CalcPythonUnloadListener(ctx, "calc:wb-timeout", WorkbookState(key="key-timeout"))
    with patch("plugin.calc.python.workbook_lifecycle.reset_python_session", return_value={"status": "error", "code": "WORKER_REENTRY"}):
        with patch("time.sleep"):
            with patch("plugin.calc.python.workbook_lifecycle.log.warning") as mock_warn:
                listener._retry_reset("calc:wb-timeout")
                mock_warn.assert_called_once()
                assert "WORKER_REENTRY" in mock_warn.call_args[0][0]


def test_retry_reset_aborts_after_reopen():
    ctx = MagicMock()
    old = _CalcPythonUnloadListener(ctx, "calc:wb-reopen", WorkbookState(key="key-old"))
    lifecycle._LISTENERS["key-new"] = _CalcPythonUnloadListener(ctx, "calc:wb-reopen", WorkbookState(key="key-new"))
    with patch("time.sleep"), patch("plugin.calc.python.workbook_lifecycle.reset_python_session") as mock_reset:
        old._retry_reset("calc:wb-reopen")
        mock_reset.assert_not_called()


def test_remember_keeps_strong_ref_until_forget_key():
    """Objects rejecting weakref must be held strongly in id map until unload."""
    import plugin.calc.python.workbook_state as wb_state

    class NoWeakRefDoc:
        __slots__ = ()

    doc = NoWeakRefDoc()
    doc_id = id(doc)
    st = WorkbookState(key="key-strong-ref")
    WorkbookState._remember(doc, st)
    assert WorkbookState.known(doc).key == "key-strong-ref"
    assert doc_id in wb_state._STATE_BY_DOC_ID
    entry = wb_state._STATE_BY_DOC_ID[doc_id]
    assert entry[0].key == "key-strong-ref"
    assert entry[1] is doc

    WorkbookState.forget_key("key-strong-ref")
    assert doc_id not in wb_state._STATE_BY_DOC_ID
    assert WorkbookState.known(doc) is None


def test_release_calc_state_dedupes_doc_urls():
    """_release_calc_state calls state.clear once."""
    ctx = MagicMock()
    state = MagicMock(spec=WorkbookState)
    listener = _CalcPythonUnloadListener(ctx, "calc:wb-dedupe", state)
    with patch(
        "plugin.calc.python.workbook_lifecycle.reset_python_session",
        return_value={"status": "ok"},
    ):
        listener._release_calc_state(("calc:wb-dedupe",))
        state.clear.assert_called_once()


def test_for_doc_reuses_session_fallback_without_runtime_uid(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("plugin.framework.queue_executor.main.is_current", lambda: True)
    doc1 = CalcDocStub(url="", props={})
    doc2 = CalcDocStub(url="", props={})

    counter = 0

    def sid(_doc: Any) -> str:
        nonlocal counter
        counter += 1
        return f"calc:unsaved:{counter}"

    monkeypatch.setattr("plugin.calc.python.workbook_lifecycle.calc_workbook_base_session_id", sid)

    assert WorkbookState.for_doc(doc1).key == "calc:unsaved:1"
    assert WorkbookState.for_doc(doc1).key == "calc:unsaved:1"
    assert counter == 1
    assert WorkbookState.for_doc(doc2).key == "calc:unsaved:2"
    assert counter == 2


def test_runtime_uid_upgrade_keeps_one_listener(monkeypatch: pytest.MonkeyPatch) -> None:
    doc = CalcDocStub(url="", props={})
    ctx = MagicMock()
    monkeypatch.setattr(
        "plugin.calc.python.workbook_lifecycle.calc_workbook_base_session_id",
        lambda _doc: "calc:unsaved:1",
    )
    with patch("plugin.calc.python.workbook_lifecycle._HAVE_UNO_DOC_EVENTS", True):
        ensure_calc_workbook_unload_resets_python(ctx, doc)
        doc.setPropertyValue("RuntimeUID", "uid-upgrade")
        ensure_calc_workbook_unload_resets_python(ctx, doc)

        assert len(doc._document_event_listeners) == 1
        assert set(lifecycle._LISTENERS) == {"uid-upgrade"}
        known = WorkbookState.known(doc)
        assert known is not None and known.key == "uid-upgrade"
        assert all(s.key != "calc:unsaved:1" for s in WorkbookState.all_states())

        with patch("plugin.calc.python.workbook_lifecycle.reset_python_session") as mock_reset:
            mock_reset.return_value = {"status": "ok"}
            doc._document_event_listeners[0].on_document_event(MagicMock(EventName="OnUnload"))
            assert lifecycle._LISTENERS == {}
            assert WorkbookState.known(doc) is None
            assert WorkbookState.all_states() == []
            mock_reset.assert_called_once_with("calc:unsaved:1")

        ensure_calc_workbook_unload_resets_python(ctx, doc)
        assert len(doc._document_event_listeners) == 2



