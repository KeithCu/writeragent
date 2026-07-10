# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for the writeragent to plugin import redirection alias."""

from __future__ import annotations

import importlib.util
import sys
import pytest
from unittest.mock import MagicMock, patch

from plugin.framework.uno_bootstrap import (
    _WRITERAGENT_API,
    register_alias_importer,
)
from tests.testing_utils import setup_uno_mocks

setup_uno_mocks()


def _clear_writeragent_modules() -> None:
    for key in list(sys.modules):
        if key == "writeragent" or key.startswith("writeragent."):
            del sys.modules[key]


_ORIGINAL_FIND_SPEC = importlib.util.find_spec


def _find_spec_without_writeragent_api(name: str, package=None):
    if name == _WRITERAGENT_API:
        return None
    return _ORIGINAL_FIND_SPEC(name, package)


def test_alias_importer_redirects_writeragent():
    register_alias_importer()

    # Import top-level writeragent (should map to plugin.scripting.writeragent_api)
    import writeragent
    import plugin.scripting.writeragent_api as api
    assert writeragent is api

    # Import submodules
    from writeragent.scripting.viz import run_viz
    from plugin.scripting.viz import run_viz as real_run_viz
    assert run_viz is real_run_viz

    # Check sys.modules populated correctly
    assert "writeragent.scripting.viz" in sys.modules
    m1 = sys.modules["writeragent.scripting.viz"]
    m2 = sys.modules["plugin.scripting.viz"]
    assert m1 is m2 or m1.__file__ == m2.__file__


def test_sandboxed_code_resolves_writeragent_imports():
    from plugin.scripting.venv.venv_sandbox import run_sandboxed_code

    code = (
        "from writeragent.scripting.analysis import coerce_to_dataframe\n"
        "result = coerce_to_dataframe"
    )
    # run_sandboxed_code executes within LocalPythonExecutor.
    # We pass data=None and execute.
    response = run_sandboxed_code(code)
    assert response["status"] == "ok", response.get("message")
    assert response["result"] is not None


def test_writeragent_api_in_process_rpc():
    import plugin.main
    import writeragent

    with patch("plugin.main.get_tools") as mock_get_tools, \
         patch("plugin.framework.uno_context.get_ctx") as mock_get_ctx, \
         patch("plugin.framework.uno_context.get_active_document") as mock_get_doc, \
         patch("plugin.doc.document_helpers.is_calc") as mock_is_calc:

        mock_registry = MagicMock()
        mock_get_tools.return_value = mock_registry
        mock_registry.execute.return_value = "mocked_sheets_list"
        mock_is_calc.return_value = True
        mock_get_ctx.return_value = MagicMock()
        mock_get_doc.return_value = MagicMock()

        with patch("writeragent.IS_WORKER", False):
            res = writeragent.sheet.list_sheets()
            assert res == "mocked_sheets_list"
            # In-process: registry.execute is called with tool_name, tctx
            mock_registry.execute.assert_called_once()
            call_args = mock_registry.execute.call_args[0]
            assert call_args[0] == "list_sheets"
            assert call_args[1].__class__.__name__ == "ToolContext"


def test_venv_worker_bidirectional_tool_call():
    import plugin.main
    from plugin.scripting.venv_worker import run_code_in_user_venv
    from plugin.framework.uno_context import get_ctx

    code = (
        "import writeragent\n"
        "result = writeragent.bookmark.list_bookmarks()\n"
    )

    with patch("plugin.main.get_tools") as mock_get_tools, \
         patch("plugin.framework.uno_context.get_ctx") as mock_get_ctx, \
         patch("plugin.framework.uno_context.get_active_document") as mock_get_doc, \
         patch("plugin.doc.document_helpers.is_calc") as mock_is_calc, \
         patch("plugin.doc.document_helpers.is_writer") as mock_is_writer, \
         patch("plugin.doc.document_helpers.is_draw") as mock_is_draw:

        mock_registry = MagicMock()
        mock_get_tools.return_value = mock_registry
        mock_registry.execute.return_value = "mocked_bookmarks_list"
        mock_get_ctx.return_value = MagicMock()
        mock_get_doc.return_value = MagicMock()
        mock_is_calc.return_value = False
        mock_is_writer.return_value = False
        mock_is_draw.return_value = False

        ctx = get_ctx()
        res = run_code_in_user_venv(ctx, code)
        assert res.get("status") == "ok", res.get("message")
        assert res.get("result") == "mocked_bookmarks_list"


def test_writeragent_namespace_fallback_when_api_missing():
    _clear_writeragent_modules()
    with patch("importlib.util.find_spec", side_effect=_find_spec_without_writeragent_api):
        register_alias_importer()
        import writeragent

        assert "writeragent" in sys.modules
        from writeragent.scripting.analysis import run_analysis
        from plugin.scripting.analysis import run_analysis as real_run_analysis

        assert run_analysis is real_run_analysis
        assert "writeragent.scripting.analysis" in sys.modules


def test_sandboxed_code_writeragent_analysis_without_api():
    from plugin.scripting.venv.venv_sandbox import run_sandboxed_code

    _clear_writeragent_modules()
    code = (
        "from writeragent.scripting.analysis import coerce_to_dataframe\n"
        "result = coerce_to_dataframe"
    )
    with patch("importlib.util.find_spec", side_effect=_find_spec_without_writeragent_api):
        register_alias_importer()
        response = run_sandboxed_code(code)
    assert response["status"] == "ok", response.get("message")
    assert response["result"] is not None

