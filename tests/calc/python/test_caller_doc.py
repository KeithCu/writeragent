# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Unit tests for caller-document resolution in =PY() execution paths."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from plugin.calc.python.caller_doc import resolve_formula_document
from plugin.calc.python.function import (
    _execute_python_addin_impl,
    _py_scoped_dir_bindings,
    finalize_python_return,
    get_python_init_kwargs,
)
from plugin.scripting.session_manager import workbook_session_id


def test_resolve_formula_document_caller_authoritative():
    ctx = MagicMock()
    caller = MagicMock(name="caller")
    # Caller doc is authoritative; no fallback or queries performed
    assert resolve_formula_document(ctx, "x = 1", caller_doc=caller) is caller


def test_resolve_formula_document_none_caller_main_thread():
    ctx = MagicMock()
    preferred = MagicMock(name="preferred")
    with (
        patch("plugin.framework.thread_guard.on_main_thread", return_value=True),
        patch("plugin.scripting.session_manager._calc_document", return_value=preferred),
        patch(
            "plugin.calc.python.formula_locator_cache.locate_formula_cell_in_open_docs",
            return_value=(preferred, MagicMock(), MagicMock(), (0, 0)),
        ) as locate,
    ):
        res = resolve_formula_document(ctx, "x = 1", caller_doc=None)
        assert res is preferred
        locate.assert_called_once_with(ctx, preferred, "x = 1")


def test_resolve_formula_document_none_caller_off_main():
    ctx = MagicMock()
    with patch("plugin.framework.thread_guard.on_main_thread", return_value=False):
        assert resolve_formula_document(ctx, "x = 1", caller_doc=None) is None


def test_execute_python_addin_impl_with_caller_never_queries_front_window():
    """When caller doc is provided, _execute_python_addin_impl must not query the desktop."""
    ctx = MagicMock()
    caller = MagicMock(name="caller")
    caller.getURL.return_value = "file:///demo.ods"
    caller.supportsService.return_value = True

    with (
        patch("plugin.scripting.session_manager._calc_document", side_effect=AssertionError("_calc_document called")),
        patch("plugin.scripting.document_scripts.get_calc_document_from_ctx", side_effect=AssertionError("get_calc_document_from_ctx called")),
        patch("plugin.calc.python.function.run_code_in_user_venv", return_value={"status": "ok", "result": 123}),
        patch("plugin.calc.python.function.finalize_python_return", return_value=123),
        patch("plugin.framework.thread_guard.on_main_thread", return_value=True),
    ):
        ret = _execute_python_addin_impl(ctx, "result = 123", None, set(), set(), doc=caller)
        assert ret == 123


def test_finalize_python_return_targets_caller_doc():
    """Multi-cell check and spill preparation must target the caller doc, not the focused doc."""
    ctx = MagicMock()
    caller_doc = MagicMock(name="caller_doc")
    caller_doc.getURL.return_value = "file:///caller.ods"
    focused_doc = MagicMock(name="focused_doc")
    focused_doc.getURL.return_value = "file:///focused.ods"

    with (
        patch("plugin.framework.config.get_config_bool", return_value=True),
        patch("plugin.framework.thread_guard.on_main_thread", return_value=True),
        patch("plugin.calc.python.function._get_calc_doc", return_value=focused_doc),
        patch("plugin.calc.python.function._selection_is_multi_cell") as mock_selection,
        patch("plugin.calc.python.function.locate_formula_cell_in_doc", return_value=None),
        patch("plugin.calc.python.function._spill_target_doc", return_value=caller_doc),
        patch("plugin.calc.python.function._prepare_auto_spill", return_value=("file:///caller.ods", "Sheet1", 0, 0)) as mock_prep,
        patch("plugin.calc.python.function._queue_deferred_spill_write") as mock_queue,
    ):
        # Multi-cell check returns True for focused_doc but False for caller_doc
        def selection_check(d):
            return d is focused_doc

        mock_selection.side_effect = selection_check

        res = finalize_python_return(ctx, "result = [[1, 2], [3, 4]]", [[1, 2], [3, 4]], doc=caller_doc)
        # If caller_doc was checked, selection is not multi-cell, so it prepares auto-spill
        assert res == 1
        mock_selection.assert_called_with(caller_doc)
        mock_prep.assert_called_once()
        assert mock_prep.call_args[0][3] is caller_doc
        mock_queue.assert_called_once()
        assert mock_queue.call_args[0][3] is caller_doc


def test_init_kwargs_and_scoped_dir_use_caller_doc_on_main():
    ctx = MagicMock()
    doc = MagicMock(name="doc")
    doc.getURL.return_value = "file:///workspace/test.ods"

    with (
        patch("plugin.framework.thread_guard.on_main_thread", return_value=True),
        patch("plugin.doc.document_research.get_document_directory", return_value="/workspace"),
        patch("plugin.scripting.document_scripts.build_python_eval_init_kwargs", return_value={"init_code": "x=1"}) as mock_build,
        patch("plugin.calc.python.workbook_lifecycle.ensure_calc_workbook_unload_resets_python"),
    ):
        scoped = _py_scoped_dir_bindings(doc)
        assert scoped == {"scoped_dir": "/workspace"}

        kwargs = get_python_init_kwargs(ctx, doc=doc)
        assert kwargs == {"init_code": "x=1"}
        mock_build.assert_called_once_with(doc)


def test_no_uno_on_caller_doc_off_main():
    """Off-main execution must never call UNO on caller doc."""
    ctx = MagicMock()
    doc = MagicMock(name="doc")
    doc.getURL.side_effect = AssertionError("getURL called off-main")
    doc.supportsService.side_effect = AssertionError("supportsService called off-main")
    doc.createInstance.side_effect = AssertionError("createInstance called off-main")

    with (
        patch("plugin.framework.thread_guard.on_main_thread", return_value=False),
        patch("plugin.scripting.session_manager.get_cached_calc_scoped_dir", return_value="/cached"),
        patch("plugin.scripting.session_manager.get_cached_calc_init_kwargs", return_value={"cached": True}),
        patch("plugin.scripting.session_manager.unambiguous_cached_calc_session_id", return_value="calc:cached"),
        patch("plugin.scripting.session_manager.python_session_mode", return_value="shared"),
    ):
        scoped = _py_scoped_dir_bindings(doc)
        assert scoped == {"scoped_dir": "/cached"}

        kwargs = get_python_init_kwargs(ctx, doc=doc)
        assert kwargs == {"cached": True}

        # function.py passes session_doc = doc if on_main_thread() else None
        session_doc = doc if False else None
        sid = workbook_session_id(ctx, doc=session_doc)
        assert sid == "calc:cached"
