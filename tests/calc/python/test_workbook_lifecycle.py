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
from plugin.tests.testing_utils import CalcDocStub, setup_uno_mocks

setup_uno_mocks()


@pytest.fixture(autouse=True)
def _reset_lifecycle_registry():
    import plugin.calc.python.workbook_lifecycle as lifecycle

    lifecycle._LISTENERS.clear()
    yield
    lifecycle._LISTENERS.clear()


def test_lifecycle_key_prefers_runtime_uid():
    doc = CalcDocStub(props={"RuntimeUID": "uid-abc"})
    assert _lifecycle_key(doc) == "uid-abc"


def test_unload_listener_resets_worker_session():
    ctx = MagicMock()
    listener = _CalcPythonUnloadListener(ctx, "calc:wb-1", "key-1")
    with patch("plugin.calc.python.workbook_lifecycle.reset_python_session") as mock_reset:
        mock_reset.return_value = {"status": "ok"}
        listener.on_document_event(MagicMock(EventName="OnUnload"))
        mock_reset.assert_called_once_with(ctx, "calc:wb-1")
        listener.on_document_event(MagicMock(EventName="OnUnload"))
        mock_reset.assert_called_once()


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
        GEOMETRIC_LOADED,
        GEOMETRIC_RECORDS,
        clear_in_memory_geometric_state,
        current_geometric_strip_safe,
        replace_geometric_strip_safe,
        reset_geometric_runtime_for_tests,
    )

    reset_geometric_runtime_for_tests()
    key = "calc:file:///gone-geo.ods"
    GEOMETRIC_RECORDS[(key, "Sheet1", "A2")] = GeometricRecord(predecessor="A1")
    GEOMETRIC_LOADED.add(key)
    replace_geometric_strip_safe(key, frozenset({EvalIndexKey(key, "x", 2)}))
    ctx = MagicMock()
    listener = _CalcPythonUnloadListener(ctx, key, "key-geo", doc_url="file:///gone-geo.ods")
    with patch("plugin.calc.python.workbook_lifecycle.reset_python_session") as mock_reset:
        mock_reset.return_value = {"status": "ok"}
        listener.on_document_event(MagicMock(EventName="OnUnload"))
    assert (key, "Sheet1", "A2") not in GEOMETRIC_RECORDS
    assert key not in GEOMETRIC_LOADED
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
