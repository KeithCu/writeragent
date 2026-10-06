# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Unit tests for PythonFunction add-in implementation and caller argument resolution."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from plugin.calc.python.addin_impl import PythonFunction


def test_python_addin_caller_forwards_calc_doc():
    ctx = MagicMock()
    fake_calc_doc = MagicMock()
    fake_calc_doc.supportsService.side_effect = lambda s: s == "com.sun.star.sheet.SpreadsheetDocument"

    func = PythonFunction(ctx, doc=MagicMock())
    with patch("plugin.calc.python.addin_impl.execute_python_addin") as mock_exec:
        mock_exec.return_value = 42
        res = func.python(fake_calc_doc, "result = 42")
        assert res == 42
        assert mock_exec.call_args.kwargs["doc"] is fake_calc_doc

        # Also test .py alias
        res_py = func.py(fake_calc_doc, "result = 42")
        assert res_py == 42
        assert mock_exec.call_args.kwargs["doc"] is fake_calc_doc


def test_python_addin_caller_none_falls_back_to_self_doc():
    ctx = MagicMock()
    self_doc = MagicMock()

    func = PythonFunction(ctx, doc=self_doc)
    with patch("plugin.calc.python.addin_impl.execute_python_addin") as mock_exec:
        mock_exec.return_value = 10
        res = func.python(None, "result = 10")
        assert res == 10
        assert mock_exec.call_args.kwargs["doc"] is self_doc


def test_python_addin_non_calc_caller_falls_back_to_self_doc():
    ctx = MagicMock()
    self_doc = MagicMock()
    non_calc_caller = MagicMock()
    non_calc_caller.supportsService.return_value = False

    func = PythonFunction(ctx, doc=self_doc)
    with patch("plugin.calc.python.addin_impl.execute_python_addin") as mock_exec:
        mock_exec.return_value = 20
        res = func.python(non_calc_caller, "result = 20")
        assert res == 20
        assert mock_exec.call_args.kwargs["doc"] is self_doc
