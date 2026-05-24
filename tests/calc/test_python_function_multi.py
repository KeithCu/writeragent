# WriterAgent - multi-range =PYTHON() add-in tests

from __future__ import annotations

import unittest.mock

from plugin.calc.python_function import MATRIX_SCALAR_SESSIONS, execute_python_addin
from plugin.scripting.payload_codec import is_multi_data


class _Ctx:
    pass


def _clear_sessions() -> None:
    if hasattr(MATRIX_SCALAR_SESSIONS, "sessions"):
        MATRIX_SCALAR_SESSIONS.sessions.clear()


def test_execute_python_addin_multi_range_uses_multi_envelope():
    ctx = _Ctx()
    col_a = ((1.0,), (2.0,), (3.0,))
    col_b = ((4.0,), (5.0,))
    try:
        with unittest.mock.patch("plugin.calc.python_function.run_code_in_user_venv") as mock_run:
            mock_run.return_value = {"status": "ok", "result": 15.0}
            res = execute_python_addin(ctx, "result = sum(data[0]) + sum(data[1])", (col_a, col_b))
            assert res == 15.0
            mock_run.assert_called_once()
            wire = mock_run.call_args.kwargs["data"]
            assert is_multi_data(wire)
            assert len(wire["items"]) == 2
    finally:
        _clear_sessions()


def test_execute_python_addin_single_range_unchanged():
    ctx = _Ctx()
    col = ((1.0,), (2.0,), (3.0,))
    try:
        with unittest.mock.patch("plugin.calc.python_function.run_code_in_user_venv") as mock_run:
            mock_run.return_value = {"status": "ok", "result": 6.0}
            res = execute_python_addin(ctx, "result = sum(data)", col)
            assert res == 6.0
            wire = mock_run.call_args.kwargs["data"]
            assert not is_multi_data(wire)
            assert wire == [1.0, 2.0, 3.0]
    finally:
        _clear_sessions()


def test_execute_python_addin_matrix_index_still_works():
    ctx = _Ctx()
    try:
        with unittest.mock.patch("plugin.calc.python_function.run_code_in_user_venv") as mock_run:
            mock_run.return_value = {"status": "ok", "result": [10, 20, 30]}
            res = execute_python_addin(ctx, "code", 1.0)
            assert res == 20.0
    finally:
        _clear_sessions()


def test_execute_python_addin_wrapped_varargs_single_range():
    ctx = _Ctx()
    col = ((1.0,), (2.0,), (3.0,))
    try:
        with unittest.mock.patch("plugin.calc.python_function.run_code_in_user_venv") as mock_run:
            mock_run.return_value = {"status": "ok", "result": 6.0}
            res = execute_python_addin(ctx, "result = sum(data)", (col,))
            assert res == 6.0
            wire = mock_run.call_args.kwargs["data"]
            assert not is_multi_data(wire)
    finally:
        _clear_sessions()


def test_execute_python_addin_splits_varargs_once():
    ctx = _Ctx()
    col = ((1.0,), (2.0,), (3.0,))
    try:
        with unittest.mock.patch("plugin.calc.python_function.split_python_addin_data_args") as mock_split:
            mock_split.return_value = [col]
            with unittest.mock.patch("plugin.calc.python_function.run_code_in_user_venv") as mock_run:
                mock_run.return_value = {"status": "ok", "result": 6.0}
                execute_python_addin(ctx, "result = sum(data)", col)
                assert mock_split.call_count == 1
    finally:
        _clear_sessions()
