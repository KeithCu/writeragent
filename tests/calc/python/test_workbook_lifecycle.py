# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

from typing import Any, Callable
from unittest.mock import MagicMock, patch

import pytest

from plugin.calc.python.spill import SpillCell, SpillRecord
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
    _doc_from_event,
    ensure_calc_workbook_unload_resets_python,
    ensure_python_session_cleared_on_unload,
    install_global_document_event_listener,
)
from plugin.tests.testing_utils import CalcDocStub


def _ensure_calc_unload(doc: Any) -> WorkbookState:
    st = WorkbookState.for_doc(doc)
    assert st is not None
    ensure_calc_workbook_unload_resets_python(doc, state=st)
    return st


@pytest.fixture(autouse=True)
def _reset_lifecycle_registry():
    WorkbookState.reset_for_tests()
    lifecycle.reset_listeners_for_tests()
    yield
    WorkbookState.reset_for_tests()
    lifecycle.reset_listeners_for_tests()


def test_for_doc_reuses_state_by_runtime_key_when_known_misses() -> None:
    """Weakref/id lookup can miss a new doc handle; for_doc must not mint an empty duplicate state."""
    doc_a = CalcDocStub(props={"RuntimeUID": "uid-reuse-state"})
    st = WorkbookState(key="uid-reuse-state")
    st.spills[("Sheet1", 0, 0)] = SpillRecord(code="", cells=[])
    WorkbookState._remember(doc_a, st)
    doc_b = CalcDocStub(props={"RuntimeUID": "uid-reuse-state"})
    assert WorkbookState.known(doc_b) is None
    reused = WorkbookState.for_doc(doc_b)
    assert reused is st
    assert ("Sheet1", 0, 0) in reused.spills


def test_unload_forgets_cached_workbook_key():
    """Off-main spill lookup must not keep a closed workbook's id."""
    doc = CalcDocStub(props={"RuntimeUID": "uid-forget"})
    st = WorkbookState(key="uid-forget")
    WorkbookState._remember(doc, st)
    assert WorkbookState.known(doc) is not None
    listener = _CalcPythonUnloadListener(st.key, "calc:wb-forget", st)
    with patch("plugin.calc.python.workbook_lifecycle.reset_python_session") as mock_reset:
        mock_reset.return_value = {"status": "ok"}
        listener.on_document_event(MagicMock(EventName="OnUnload"))
    assert WorkbookState.known(doc) is None
    assert WorkbookState.known(None) is None


def test_unload_listener_resets_worker_session():
    listener = _CalcPythonUnloadListener("key-1", "calc:wb-1", WorkbookState(key="key-1"))
    with patch("plugin.calc.python.workbook_lifecycle.reset_python_session") as mock_reset:
        mock_reset.return_value = {"status": "ok"}
        listener.on_document_event(MagicMock(EventName="OnUnload"))
        mock_reset.assert_called_once_with("calc:wb-1", timeout_sec=3, lease_wait_sec=3)
        listener.on_document_event(MagicMock(EventName="OnUnload"))
        mock_reset.assert_called_once()


def test_on_disposing_runs_teardown():
    listener = _CalcPythonUnloadListener("key-disposing", "calc:wb-disposing", WorkbookState(key="key-disposing"))
    with patch("plugin.calc.python.workbook_lifecycle.reset_python_session") as mock_reset:
        mock_reset.return_value = {"status": "ok"}
        listener.on_disposing(MagicMock())
        mock_reset.assert_called_once_with("calc:wb-disposing", timeout_sec=3, lease_wait_sec=3)
        listener.on_disposing(MagicMock())
        mock_reset.assert_called_once()


def test_non_calc_unload_resets_rps_and_notebook_sessions(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr("plugin.calc.python.workbook_lifecycle.HAVE_UNO", True)
    doc = MagicMock()
    doc.getPropertyValue.return_value = "writer-uid"

    ensure_python_session_cleared_on_unload(doc, "rps:file:///a.odt")
    ensure_python_session_cleared_on_unload(doc, "notebook:file:///a.odt")
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
    listener = _CalcPythonUnloadListener("key-spill", "calc:file:///gone.ods", state)
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
    state = WorkbookState(key="key-geo")
    listener = _CalcPythonUnloadListener("key-geo", key, state)
    with patch("plugin.calc.python.workbook_lifecycle.reset_python_session") as mock_reset:
        mock_reset.return_value = {"status": "ok"}
        listener.on_document_event(MagicMock(EventName="OnUnload"))
    assert (key, "Sheet1", "A2") not in _RUNTIME.records
    assert _RUNTIME.strip_safe == frozenset()
    clear_in_memory_geometric_state(workbook_key=key)


def test_ensure_registers_listener_once():
    doc = CalcDocStub(props={"RuntimeUID": "uid-reg"})
    with patch("plugin.calc.python.workbook_lifecycle.HAVE_UNO", True):
        _ensure_calc_unload(doc)
        _ensure_calc_unload(doc)
    assert len(doc._document_event_listeners) == 1


def test_unload_clears_registration_so_reopen_can_reregister():
    """After OnUnload the same RuntimeUID can register again (close then reopen)."""
    doc = CalcDocStub(props={"RuntimeUID": "uid-reopen"})
    with patch("plugin.calc.python.workbook_lifecycle.HAVE_UNO", True):
        with patch("plugin.calc.python.workbook_lifecycle.reset_python_session") as mock_reset:
            mock_reset.return_value = {"status": "ok"}
            _ensure_calc_unload(doc)
            assert "uid-reopen" in lifecycle._LISTENERS
            doc._document_event_listeners[0].on_document_event(MagicMock(EventName="OnUnload"))
            assert "uid-reopen" not in lifecycle._LISTENERS
            mock_reset.assert_called_once()

            _ensure_calc_unload(doc)
            assert len(doc._document_event_listeners) == 2
            assert "uid-reopen" in lifecycle._LISTENERS


def test_save_notes_new_session_id_reset_on_unload():
    """After Save the listener must reset both the uuid kernel and the file-URL kernel."""
    doc = CalcDocStub(props={"RuntimeUID": "uid-save"}, url="")
    with patch("plugin.calc.python.workbook_lifecycle.HAVE_UNO", True):
        with patch("plugin.calc.python.workbook_lifecycle.calc_workbook_base_session_id", side_effect=[
            "calc:uuid-1",
            "calc:uuid-1",
            "calc:file:///saved.ods",
            "calc:file:///saved.ods",
            "calc:file:///saved.ods",
        ]):
            with patch("plugin.calc.python.workbook_lifecycle.reset_python_session") as mock_reset:
                mock_reset.return_value = {"status": "ok"}
                _ensure_calc_unload(doc)
                doc.url = "file:///saved.ods"
                _ensure_calc_unload(doc)
                assert len(doc._document_event_listeners) == 1
                doc._document_event_listeners[0].on_document_event(MagicMock(EventName="OnUnload"))
    reset_ids = {call.args[0] for call in mock_reset.call_args_list}
    assert reset_ids == {"calc:uuid-1", "calc:file:///saved.ods"}
    assert "uid-save" not in lifecycle._LISTENERS


def test_for_doc_notes_session_id_on_second_call(monkeypatch: pytest.MonkeyPatch) -> None:
    """Each UI-thread for_doc refreshes the unload listener (note_calc_identity on Save)."""
    doc = CalcDocStub(props={"RuntimeUID": "uid-save"}, url="")
    monkeypatch.setattr("plugin.framework.queue_executor.main.is_current", lambda: True)
    with patch("plugin.calc.python.workbook_lifecycle.HAVE_UNO", True):
        with patch(
            "plugin.calc.python.workbook_lifecycle.calc_workbook_base_session_id",
            side_effect=["calc:uuid-1", "calc:file:///saved.ods", "calc:file:///saved.ods"],
        ):
            with patch("plugin.calc.python.workbook_lifecycle.reset_python_session") as mock_reset:
                mock_reset.return_value = {"status": "ok"}
                WorkbookState.for_doc(doc)
                doc.url = "file:///saved.ods"
                WorkbookState.for_doc(doc)
                assert len(doc._document_event_listeners) == 1
                doc._document_event_listeners[0].on_document_event(MagicMock(EventName="OnUnload"))
    reset_ids = {call.args[0] for call in mock_reset.call_args_list}
    assert reset_ids == {"calc:uuid-1", "calc:file:///saved.ods"}


def test_unload_clears_only_that_workbook_matrix(monkeypatch: pytest.MonkeyPatch) -> None:
    from plugin.calc.python.function import WorkerResultSession

    doc_a = CalcDocStub(props={"RuntimeUID": "uid-a"})
    doc_b = CalcDocStub(props={"RuntimeUID": "uid-b"})
    st_a = WorkbookState.for_doc(doc_a)
    st_b = WorkbookState.for_doc(doc_b)
    assert st_a is not None and st_b is not None
    st_a.matrix[(1, ("k",), "tok")] = WorkerResultSession([], (), timestamp=0.0)
    st_b.matrix[(2, ("k",), "tok")] = WorkerResultSession([], (), timestamp=0.0)
    listener = _CalcPythonUnloadListener(st_a.key, "calc:a", st_a)
    with patch("plugin.calc.python.workbook_lifecycle.reset_python_session", return_value={"status": "ok"}):
        listener.on_document_event(MagicMock(EventName="OnUnload"))
    assert st_a.matrix == {}
    assert len(st_b.matrix) == 1


def test_unload_resets_worker_when_busy():
    listener = _CalcPythonUnloadListener("key-busy", "calc:wb-1", WorkbookState(key="key-busy"))

    seen = []

    def mock_reset_side_effect(sid: str, **kwargs: Any) -> dict[str, Any]:
        seen.append(sid)
        if len(seen) == 1:
            return {"status": "error", "code": "WORKER_REENTRY"}
        return {"status": "ok"}

    with patch("plugin.calc.python.workbook_lifecycle.reset_python_session", side_effect=mock_reset_side_effect):
        with patch("plugin.calc.python.workbook_lifecycle.run_in_background") as mock_run:
            listener.on_document_event(MagicMock(EventName="OnUnload"))
            # Initial call
            assert len(seen) == 1
            # Ensure background fallback was scheduled with dedicated=True
            retry_calls = [c for c in mock_run.call_args_list if c[1].get("name") == "reset_python_session_retry"]
            assert len(retry_calls) == 1
            assert retry_calls[0][1].get("dedicated") is True
            callback = retry_calls[0][0][0]
            with patch("time.sleep"):
                callback()
            assert len(seen) == 2
    assert listener._teardown_done is True


def test_teardown_passes_lease_wait_sec_and_triggers_retry():
    listener = _CalcPythonUnloadListener("key-blocked", "calc:wb-blocked", WorkbookState(key="key-blocked"))
    with patch("plugin.calc.python.workbook_lifecycle.reset_python_session") as mock_reset:
        mock_reset.return_value = {"status": "error", "code": "WORKER_REENTRY"}
        with patch("plugin.calc.python.workbook_lifecycle.run_in_background") as mock_run:
            listener._teardown()
            mock_reset.assert_called_once()
            assert mock_reset.call_args[0] == ("calc:wb-blocked",)
            assert mock_reset.call_args[1]["timeout_sec"] > 0
            assert mock_reset.call_args[1]["lease_wait_sec"] > 0
            retry_calls = [c for c in mock_run.call_args_list if c[1].get("name") == "reset_python_session_retry"]
            assert len(retry_calls) == 1
            assert retry_calls[0][1].get("dedicated") is True

    listener2 = _CalcPythonUnloadListener("key-to", "calc:wb-to", WorkbookState(key="key-to"))
    with patch("plugin.calc.python.workbook_lifecycle.reset_python_session") as mock_reset:
        mock_reset.return_value = {"status": "error", "code": "VENV_TIMEOUT"}
        with patch("plugin.calc.python.workbook_lifecycle.run_in_background") as mock_run:
            listener2._teardown()
            mock_reset.assert_called_once()
            assert mock_reset.call_args[0] == ("calc:wb-to",)
            assert mock_reset.call_args[1]["timeout_sec"] > 0
            assert mock_reset.call_args[1]["lease_wait_sec"] > 0
            retry_calls = [c for c in mock_run.call_args_list if c[1].get("name") == "reset_python_session_retry"]
            assert len(retry_calls) == 1
            assert retry_calls[0][1].get("dedicated") is True


def test_teardown_drops_retry_when_reopened():
    """When a workbook is closed and immediately reopened, any pending retry must abort."""
    old_listener = _CalcPythonUnloadListener("key-old2", "calc:wb-reopen2", WorkbookState(key="key-old2"))
    new_listener = _CalcPythonUnloadListener("key-new2", "calc:wb-reopen2", WorkbookState(key="key-new2"))
    lifecycle._LISTENERS["key-new2"] = new_listener
    try:
        with patch("time.sleep"), patch("plugin.calc.python.workbook_lifecycle.reset_python_session") as mock_reset:
            old_listener._retry_reset("calc:wb-reopen2")
            mock_reset.assert_not_called()
    finally:
        lifecycle._LISTENERS.pop("key-new2", None)


def test_retry_reset_exhausts_retries_and_warns_on_timeout(caplog: pytest.LogCaptureFixture):
    listener = _CalcPythonUnloadListener("key-timeout", "calc:wb-timeout", WorkbookState(key="key-timeout"))
    with patch("plugin.calc.python.workbook_lifecycle.reset_python_session", return_value={"status": "error", "code": "WORKER_REENTRY"}) as mock_reset:
        with patch("time.sleep"):
            with caplog.at_level("WARNING", logger="plugin.calc.python.workbook_lifecycle"):
                listener._retry_reset("calc:wb-timeout")
                assert mock_reset.call_count > 1
                assert len([r for r in caplog.records if r.levelname == "WARNING"]) == 1


def test_retry_reset_aborts_after_reopen():
    old = _CalcPythonUnloadListener("key-old", "calc:wb-reopen", WorkbookState(key="key-old"))
    lifecycle._LISTENERS["key-new"] = _CalcPythonUnloadListener("key-new", "calc:wb-reopen", WorkbookState(key="key-new"))
    try:
        with patch("time.sleep"), patch("plugin.calc.python.workbook_lifecycle.reset_python_session") as mock_reset:
            old._retry_reset("calc:wb-reopen")
            mock_reset.assert_not_called()
    finally:
        lifecycle._LISTENERS.pop("key-new", None)


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


def test_release_calc_state_clears_state_once():
    """_release_calc_state calls state.clear once."""
    state = MagicMock(spec=WorkbookState)
    listener = _CalcPythonUnloadListener("key-dedupe", "calc:wb-dedupe", state)
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

    monkeypatch.setattr("plugin.calc.python.workbook_state.calc_workbook_base_session_id", sid)

    assert WorkbookState.for_doc(doc1).key == "calc:unsaved:1"
    assert WorkbookState.for_doc(doc1).key == "calc:unsaved:1"
    assert counter == 1
    assert WorkbookState.for_doc(doc2).key == "calc:unsaved:2"
    assert counter == 2


def test_spill_save_as_lifecycle_consistency() -> None:
    """Save As changes the document URL, so spills stay keyed by RuntimeUID."""
    mock_doc = MagicMock()
    mock_doc.getRuntimeUID.return_value = "uid-123"
    mock_doc.getPropertyValue.return_value = "uid-123"
    mock_doc.getURL.return_value = "file:///tmp/old.ods"

    state = WorkbookState.for_doc(mock_doc)
    assert state is not None and state.key == "uid-123"
    state.spills[("Sheet1", 0, 0)] = SpillRecord(
        code="result = 1",
        cells=[SpillCell(1, 0, "n", 1.0), SpillCell(2, 0, "n", 2.0)],
    )

    mock_doc.getURL.return_value = "file:///tmp/new.ods"
    new_state = WorkbookState.for_doc(mock_doc)
    assert new_state is state
    assert ("Sheet1", 0, 0) in new_state.spills
    assert new_state.spills[("Sheet1", 0, 0)].spills == [(1, 0), (2, 0)]


def test_runtime_uid_upgrade_keeps_one_listener(monkeypatch: pytest.MonkeyPatch) -> None:
    doc = CalcDocStub(url="", props={})
    monkeypatch.setattr(
        "plugin.calc.python.workbook_state.calc_workbook_base_session_id",
        lambda _doc: "calc:unsaved:1",
    )
    monkeypatch.setattr(
        "plugin.calc.python.workbook_lifecycle.calc_workbook_base_session_id",
        lambda _doc: "calc:unsaved:1",
    )
    with patch("plugin.calc.python.workbook_lifecycle.HAVE_UNO", True):
        _ensure_calc_unload(doc)
        doc.setPropertyValue("RuntimeUID", "uid-upgrade")
        _ensure_calc_unload(doc)

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
            mock_reset.assert_called_once_with("calc:unsaved:1", timeout_sec=3, lease_wait_sec=3)

        _ensure_calc_unload(doc)
        assert len(doc._document_event_listeners) == 2


def test_non_calc_unload_listener_has_no_workbook_state(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("plugin.calc.python.workbook_lifecycle.HAVE_UNO", True)
    doc = MagicMock()
    doc.getPropertyValue.return_value = "writer-uid"
    ensure_python_session_cleared_on_unload(doc, "rps:file:///a.odt")
    listener = doc.addDocumentEventListener.call_args[0][0]
    with (
        patch.object(WorkbookState, "forget_key") as mock_forget,
        patch("plugin.calc.python.workbook_lifecycle.reset_python_session", return_value={"status": "ok"}) as mock_reset,
    ):
        listener.on_document_event(MagicMock(EventName="OnUnload"))
    assert lifecycle._LISTENERS == {}
    mock_forget.assert_not_called()
    mock_reset.assert_called_once()


def _mock_broadcaster(ctx: Any | None = None) -> tuple[Any, Any]:
    ctx_obj = ctx if ctx is not None else MagicMock()
    smgr = MagicMock()
    broadcaster = MagicMock()
    ctx_obj.getServiceManager.return_value = smgr
    smgr.createInstanceWithContext.return_value = broadcaster
    return ctx_obj, broadcaster


def _installed(ctx: Any) -> Any:
    _ctx, broadcaster = _mock_broadcaster(ctx)
    install_global_document_event_listener(ctx)
    return broadcaster.addDocumentEventListener.call_args[0][0]


def test_install_global_document_event_listener_once():
    ctx, broadcaster = _mock_broadcaster()
    install_global_document_event_listener(ctx)
    install_global_document_event_listener(ctx)
    assert broadcaster.addDocumentEventListener.call_count == 1


def test_install_global_listener_reentrant_call_attaches_once():
    ctx, broadcaster = _mock_broadcaster()

    def _reenter(_listener: Any) -> None:
        install_global_document_event_listener(ctx)

    broadcaster.addDocumentEventListener.side_effect = _reenter
    install_global_document_event_listener(ctx)
    assert broadcaster.addDocumentEventListener.call_count == 1


def test_install_global_listener_failure_frees_slot():
    ctx, broadcaster = _mock_broadcaster()
    broadcaster.addDocumentEventListener.side_effect = [RuntimeError("disposed"), None]

    with patch.object(lifecycle.log, "warning") as warn:
        install_global_document_event_listener(ctx)
    assert lifecycle._global_listener is None
    warn.assert_called_once()

    install_global_document_event_listener(ctx)
    assert broadcaster.addDocumentEventListener.call_count == 2
    assert lifecycle._global_listener is not None


class _DisposedViewControllerEvent:
    """Simulates UNO DocumentEvent where ViewController access raises (disposed)."""

    def __init__(self, source=None):
        self.Source = source
        self.EventName = ""

    def __getattr__(self, name: str):
        if name == "ViewController":
            raise RuntimeError("DrawController object has already been disposed")
        raise AttributeError(name)


def test_doc_from_event_disposed_view_controller_returns_none():
    assert _doc_from_event(_DisposedViewControllerEvent()) is None


def test_doc_from_event_disposed_view_controller_falls_back_to_calc_source():
    source = CalcDocStub()
    setattr(source, "CurrentController", None)
    setattr(source, "_controller", None)
    assert _doc_from_event(_DisposedViewControllerEvent(source=source)) is source


def test_doc_from_event_onnew_controller_throw_still_returns_calc_source():
    """Hidden factory OnNew: Source is the scalc, getCurrentController raises."""
    source = CalcDocStub()

    def _boom():
        raise RuntimeError("no controller yet")

    setattr(source, "getCurrentController", _boom)

    class _OnNew:
        EventName = "OnNew"
        Source = source

    assert _doc_from_event(_OnNew()) is source


def test_onload_rewrites_collabora_py_via_call():
    """OnLoadFinished marshals geometric open (ctx, doc) then Collabora rewrite (doc only)."""
    from plugin.calc.python.collabora_formula import maybe_rewrite_collabora_py_formulas

    ctx = MagicMock()
    listener = _installed(ctx)
    doc = CalcDocStub()

    class _LoadEvent:
        EventName = "OnLoadFinished"
        Source = doc

    event = _LoadEvent()
    with (
        patch("plugin.calc.excel_py_convert.auto_open.maybe_convert_excel_py_document") as convert,
        patch("plugin.framework.queue_executor.main.call") as marshal,
        patch("plugin.framework.queue_executor.main.is_current", return_value=False),
    ):
        listener.on_document_event(event)
        convert.assert_called_once()
        assert [c.args for c in marshal.call_args_list] == [
            (lifecycle._geometric_open_job, ctx, doc),
            (maybe_rewrite_collabora_py_formulas, doc),
        ]


def _run_in_dummy_thread(fn: Callable[[], None], timeout: float = 5.0) -> None:
    import _thread
    import threading

    done = threading.Event()
    exc: list[BaseException] = []

    def target() -> None:
        try:
            fn()
        except BaseException as err:
            exc.append(err)
        finally:
            done.set()

    _thread.start_new_thread(target, ())
    if not done.wait(timeout):
        raise TimeoutError(f"Dummy thread failed to finish within {timeout} s")
    if exc:
        raise exc[0]


def test_onload_collabora_rewrite_inlines_on_untagged_dummy():
    """OnLoadFinished inlines Collabora rewrite on an untagged Dummy thread (dev UNTAGGED_DUMMY_IS_CURRENT)."""
    ctx = MagicMock()
    listener = _installed(ctx)
    doc = CalcDocStub()

    class _LoadEvent:
        EventName = "OnLoadFinished"
        Source = doc

    event = _LoadEvent()
    with (
        patch("plugin.calc.excel_py_convert.auto_open.maybe_convert_excel_py_document") as convert,
        patch.object(lifecycle, "_geometric_open_job"),
        patch(
            "plugin.calc.python.workbook_lifecycle.maybe_rewrite_collabora_py_formulas"
        ) as rewrite,
        patch("plugin.framework.queue_executor.main.call", side_effect=TimeoutError("main.call should not be reached")) as marshal,
        patch.object(lifecycle.log, "warning") as warn,
    ):
        def _fire() -> None:
            listener.on_document_event(event)

        _run_in_dummy_thread(_fire)
        convert.assert_called_once()
        rewrite.assert_called_once_with(doc)
        marshal.assert_not_called()
        warn.assert_not_called()


def test_onnew_records_geometric_session_inline_on_uno_thread():
    """OnNew is already on the VCL thread — do not enqueue (30s leftover hang)."""
    ctx = MagicMock()
    listener = _installed(ctx)
    doc = CalcDocStub()

    class _NewEvent:
        EventName = "OnNew"
        Source = doc

    with (
        patch("plugin.calc.excel_py_convert.auto_open.maybe_convert_excel_py_document") as convert,
        patch("plugin.framework.queue_executor.main.call") as marshal,
        patch("plugin.framework.queue_executor.main.is_current", return_value=True),
        patch(
            "plugin.calc.python.workbook_lifecycle.maybe_geometric_on_document_open"
        ) as open_geo,
    ):
        listener.on_document_event(_NewEvent())
        convert.assert_not_called()
        marshal.assert_not_called()
        open_geo.assert_called_once_with(ctx, doc)


def test_oncreate_without_resolvable_doc_still_runs_geometric_open():
    """Leftover OnCreate Source is often the Writer keeper — the open hook still runs."""
    ctx = MagicMock()
    listener = _installed(ctx)

    class _CreateEvent:
        EventName = "OnCreate"
        Source = None

    with (
        patch("plugin.calc.excel_py_convert.auto_open.maybe_convert_excel_py_document") as convert,
        patch("plugin.framework.queue_executor.main.call") as marshal,
        patch("plugin.framework.queue_executor.main.is_current", return_value=True),
        patch(
            "plugin.calc.python.workbook_lifecycle.maybe_geometric_on_document_open"
        ) as open_geo,
    ):
        listener.on_document_event(_CreateEvent())
        convert.assert_not_called()
        marshal.assert_not_called()
        open_geo.assert_called_once_with(ctx, None)


def test_geometric_open_job_wires_unload_listener():
    """Closing the workbook is the soffice-side clear for its kernel and geometric state."""
    doc = CalcDocStub()
    ctx = MagicMock()
    with (
        patch("plugin.calc.python.workbook_lifecycle.maybe_geometric_on_document_open") as open_geo,
        patch(
            "plugin.calc.python.workbook_state.WorkbookState.for_doc",
        ) as for_doc_mock,
    ):
        lifecycle._geometric_open_job(ctx, doc)
    open_geo.assert_called_once_with(ctx, doc)
    for_doc_mock.assert_called_once_with(doc)


def test_onnew_records_geometric_session_via_call():
    """Factory Calc fires OnNew off-main; leftover still needs a marshaled record."""
    ctx = MagicMock()
    listener = _installed(ctx)
    doc = CalcDocStub()

    class _NewEvent:
        EventName = "OnNew"
        Source = doc

    with (
        patch("plugin.calc.excel_py_convert.auto_open.maybe_convert_excel_py_document") as convert,
        patch("plugin.framework.queue_executor.main.call") as marshal,
        patch("plugin.framework.queue_executor.main.is_current", return_value=False),
    ):
        listener.on_document_event(_NewEvent())
        convert.assert_not_called()
        assert marshal.call_count == 1
        assert marshal.call_args_list[0][0][0] is lifecycle._geometric_open_job
        assert marshal.call_args_list[0][0][1:] == (ctx, doc)


def test_global_listener_disposed_view_controller_does_not_warn():
    """Listener must not hit the outer warning path when ViewController is disposed."""
    ctx = MagicMock()
    listener = _installed(ctx)
    event = _DisposedViewControllerEvent()
    event.EventName = "OnLoadFinished"
    with patch.object(lifecycle.log, "warning") as warn:
        listener.on_document_event(event)
        warn.assert_not_called()



