# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from plugin.calc.python.workbook_lifecycle import (
    _CalcPythonUnloadListener,
    _lifecycle_key,
    ensure_calc_workbook_unload_resets_python,
)
from plugin.tests.testing_utils import CalcDocStub


@pytest.fixture(autouse=True)
def _reset_lifecycle_registry():
    import plugin.calc.python.workbook_lifecycle as lifecycle

    lifecycle._LISTENERS.clear()
    lifecycle._LIFECYCLE_KEYS.clear()
    lifecycle._LIFECYCLE_REFS_BY_KEY.clear()
    lifecycle._LIFECYCLE_KEY_BY_DOC_ID.clear()
    lifecycle._DOC_IDS_BY_LIFECYCLE_KEY.clear()
    yield
    lifecycle._LISTENERS.clear()
    lifecycle._LIFECYCLE_KEYS.clear()
    lifecycle._LIFECYCLE_REFS_BY_KEY.clear()
    lifecycle._LIFECYCLE_KEY_BY_DOC_ID.clear()
    lifecycle._DOC_IDS_BY_LIFECYCLE_KEY.clear()


def test_lifecycle_key_prefers_runtime_uid():
    doc = CalcDocStub(props={"RuntimeUID": "uid-abc"})
    assert _lifecycle_key(doc) == "uid-abc"


def test_unload_forgets_cached_lifecycle_key():
    """Off-main spill lookup must not keep a closed workbook's id."""
    from plugin.calc.python.workbook_lifecycle import lifecycle_key_if_known

    doc = CalcDocStub(props={"RuntimeUID": "uid-forget"})
    assert _lifecycle_key(doc) == "uid-forget"
    assert lifecycle_key_if_known(doc) == "uid-forget"
    listener = _CalcPythonUnloadListener(MagicMock(), "calc:wb-forget", "uid-forget")
    with patch("plugin.calc.python.workbook_lifecycle.reset_python_session") as mock_reset:
        mock_reset.return_value = {"status": "ok"}
        listener.on_document_event(MagicMock(EventName="OnUnload"))
    assert lifecycle_key_if_known(doc) == ""
    assert lifecycle_key_if_known(None) == ""


def test_unload_listener_resets_worker_session():
    ctx = MagicMock()
    listener = _CalcPythonUnloadListener(ctx, "calc:wb-1", "key-1")
    with patch("plugin.calc.python.workbook_lifecycle.reset_python_session") as mock_reset:
        mock_reset.return_value = {"status": "ok"}
        listener.on_document_event(MagicMock(EventName="OnUnload"))
        mock_reset.assert_called_once_with(ctx, "calc:wb-1")
        listener.on_document_event(MagicMock(EventName="OnUnload"))
        mock_reset.assert_called_once()


def test_non_calc_unload_resets_rps_and_notebook_sessions(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr("plugin.calc.python.workbook_lifecycle._HAVE_UNO_DOC_EVENTS", True)
    ctx = MagicMock()
    doc = MagicMock()
    doc.getPropertyValue.return_value = "writer-uid"
    from plugin.calc.python.workbook_lifecycle import ensure_python_session_cleared_on_unload

    ensure_python_session_cleared_on_unload(ctx, doc, "rps:file:///a.odt")
    ensure_python_session_cleared_on_unload(ctx, doc, "notebook:file:///a.odt")
    doc.addDocumentEventListener.assert_called_once()
    listener = doc.addDocumentEventListener.call_args[0][0]
    with patch("plugin.calc.python.workbook_lifecycle.reset_python_session") as mock_reset:
        mock_reset.return_value = {"status": "ok"}
        listener.on_document_event(MagicMock(EventName="OnUnload"))
    assert mock_reset.call_count == 2
    assert {call.args[1] for call in mock_reset.call_args_list} == {
        "rps:file:///a.odt",
        "notebook:file:///a.odt",
    }


def test_unload_clears_in_memory_spill_state():
    import plugin.calc.python.function as python_function

    python_function.SPILL_REGISTRY[("file:///gone.ods", "Sheet1", 0, 0)] = [(0, 1)]
    python_function.LOADED_DOCUMENTS.add("file:///gone.ods")
    ctx = MagicMock()
    listener = _CalcPythonUnloadListener(ctx, "calc:file:///gone.ods", "key-spill", doc_url="file:///gone.ods")
    with patch("plugin.calc.python.workbook_lifecycle.reset_python_session") as mock_reset:
        mock_reset.return_value = {"status": "ok"}
        listener.on_document_event(MagicMock(EventName="OnUnload"))
    assert ("file:///gone.ods", "Sheet1", 0, 0) not in python_function.SPILL_REGISTRY
    assert "file:///gone.ods" not in python_function.LOADED_DOCUMENTS


def test_unload_clears_in_memory_geometric_state():
    from plugin.calc.python.geometric_recalc import (
        EvalIndexKey,
        GeometricRecord,
        _RUNTIME,
        clear_in_memory_geometric_state,
        current_geometric_strip_safe,
        replace_geometric_strip_safe,
        reset_geometric_runtime_for_tests,
    )

    reset_geometric_runtime_for_tests()
    key = "calc:file:///gone-geo.ods"
    _RUNTIME.records[(key, "Sheet1", "A2")] = GeometricRecord(predecessor="A1")
    replace_geometric_strip_safe(key, frozenset({EvalIndexKey(key, "x", 2)}))
    ctx = MagicMock()
    listener = _CalcPythonUnloadListener(ctx, key, "key-geo", doc_url="file:///gone-geo.ods")
    with patch("plugin.calc.python.workbook_lifecycle.reset_python_session") as mock_reset:
        mock_reset.return_value = {"status": "ok"}
        listener.on_document_event(MagicMock(EventName="OnUnload"))
    assert (key, "Sheet1", "A2") not in _RUNTIME.records
    assert current_geometric_strip_safe() == frozenset()
    clear_in_memory_geometric_state()


def test_unload_clears_formula_location_cache():
    from plugin.calc.python.formula_locator_cache import FORMULA_LOCATION_CACHE

    FORMULA_LOCATION_CACHE.put("key-1", "plt.show()", "Sheet1", 0, 0)
    ctx = MagicMock()
    listener = _CalcPythonUnloadListener(ctx, "calc:wb-1", "key-1")
    with patch("plugin.calc.python.workbook_lifecycle.reset_python_session") as mock_reset:
        mock_reset.return_value = {"status": "ok"}
        listener.on_document_event(MagicMock(EventName="OnUnload"))
    assert FORMULA_LOCATION_CACHE.get("key-1", "plt.show()") == []


def test_ensure_registers_listener_once():
    ctx = MagicMock()
    doc = CalcDocStub(props={"RuntimeUID": "uid-reg"})
    with patch("plugin.calc.python.workbook_lifecycle._HAVE_UNO_DOC_EVENTS", True):
        ensure_calc_workbook_unload_resets_python(ctx, doc)
        ensure_calc_workbook_unload_resets_python(ctx, doc)
    assert len(doc._document_event_listeners) == 1


def test_unload_clears_registration_so_reopen_can_reregister():
    """After OnUnload the same RuntimeUID can register again (close then reopen)."""
    import plugin.calc.python.workbook_lifecycle as lifecycle

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
    import plugin.calc.python.workbook_lifecycle as lifecycle

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
    reset_ids = {call.args[1] for call in mock_reset.call_args_list}
    assert reset_ids == {"calc:uuid-1", "calc:file:///saved.ods"}
    assert "uid-save" not in lifecycle._LISTENERS


def test_unload_resets_worker_when_busy():
    from unittest.mock import MagicMock, patch
    from plugin.calc.python.workbook_lifecycle import _CalcPythonUnloadListener
    ctx = MagicMock()
    listener = _CalcPythonUnloadListener(ctx, "calc:wb-1", "key-busy", doc_url="")

    seen = []
    def mock_reset_side_effect(_ctx, sid):
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
    from unittest.mock import MagicMock, patch
    from plugin.calc.python.workbook_lifecycle import _CalcPythonUnloadListener
    ctx = MagicMock()
    listener = _CalcPythonUnloadListener(ctx, "calc:wb-timeout", "key-timeout")
    with patch("plugin.calc.python.workbook_lifecycle.reset_python_session", return_value={"status": "error", "code": "WORKER_REENTRY"}):
        with patch("time.sleep"):
            with patch("plugin.calc.python.workbook_lifecycle.log.warning") as mock_warn:
                listener._retry_reset("calc:wb-timeout")
                mock_warn.assert_called_once()
                assert "WORKER_REENTRY" in mock_warn.call_args[0][0]


def test_remember_doc_lifecycle_key_keeps_strong_reference():
    """Objects rejecting weakref must be held strongly in id map until unload."""
    import plugin.calc.python.workbook_lifecycle as lifecycle
    from plugin.calc.python.workbook_lifecycle import _forget_doc_lifecycle_key, _remember_doc_lifecycle_key, lifecycle_key_if_known

    class NoWeakRefDoc:
        __slots__ = ()

    doc = NoWeakRefDoc()
    doc_id = id(doc)
    _remember_doc_lifecycle_key(doc, "key-strong-ref")
    assert lifecycle_key_if_known(doc) == "key-strong-ref"
    assert doc_id in lifecycle._LIFECYCLE_KEY_BY_DOC_ID
    entry = lifecycle._LIFECYCLE_KEY_BY_DOC_ID[doc_id]
    assert entry[0] == "key-strong-ref"
    assert entry[1] is doc

    _forget_doc_lifecycle_key("key-strong-ref")
    assert doc_id not in lifecycle._LIFECYCLE_KEY_BY_DOC_ID
    assert lifecycle_key_if_known(doc) == ""


def test_release_calc_state_dedupes_doc_urls():
    """_release_calc_state dedupes URLs and skips empty strings."""
    from unittest.mock import MagicMock, patch
    from plugin.calc.python.workbook_lifecycle import _CalcPythonUnloadListener

    ctx = MagicMock()
    listener = _CalcPythonUnloadListener(ctx, "calc:wb-dedupe", "key-dedupe")
    with patch("plugin.calc.python.function.clear_in_memory_spill_state") as mock_spill:
        listener._release_calc_state(
            ("calc:wb-dedupe",),
            ("file:///a.ods", "", "file:///a.ods", ""),
            "key-dedupe",
            reset_sessions=False,
        )
        mock_spill.assert_called_once_with(doc_url="file:///a.ods", lifecycle_key="key-dedupe")
