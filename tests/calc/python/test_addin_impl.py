# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Unit tests for PythonFunction add-in implementation and its caller argument."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from plugin.calc.calc_addin_data import _FALSE_STRINGS, _TRUE_STRINGS, calc_addin_data_to_python
from plugin.calc.python.addin_impl import PythonFunction


def test_python_addin_caller_is_forwarded_as_doc():
    ctx = MagicMock()
    caller = MagicMock()

    func = PythonFunction(ctx)
    with patch("plugin.calc.python.addin_impl.execute_python_addin") as mock_exec:
        mock_exec.return_value = 42
        res = func.python(caller, "result = 42")
        assert res == 42
        assert mock_exec.call_args.kwargs["doc"] is caller

        # Also test .py alias
        res_py = func.py(caller, "result = 42")
        assert res_py == 42
        assert mock_exec.call_args.kwargs["doc"] is caller
    # The caller is only passed through; the add-in entry makes no UNO call on it.
    caller.supportsService.assert_not_called()


def test_python_addin_missing_caller_returns_error_without_guessing():
    ctx = MagicMock()

    func = PythonFunction(ctx)
    with (
        patch("plugin.calc.python.addin_impl.execute_python_addin") as mock_exec,
        patch("plugin.scripting.document_scripts.get_calc_document_from_ctx") as front_window,
    ):
        assert func.python(None, "result = 1") == "Error: no calling document"
        assert func.py(None, "result = 1") == "Error: no calling document"
    mock_exec.assert_not_called()
    front_window.assert_not_called()


def test_python_addin_uses_english_logical_strings():
    ctx = MagicMock()
    PythonFunction(ctx)
    assert _TRUE_STRINGS == {"=TRUE()", "TRUE", "True"}
    assert _FALSE_STRINGS == {"=FALSE()", "FALSE", "False"}
    # Exact-case matching is intentional; lowercase stays text.
    assert calc_addin_data_to_python("true") == [["true"]]
    ctx.getServiceManager.assert_not_called()
