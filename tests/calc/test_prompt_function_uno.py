# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

from plugin.testing_runner import setup, native_test
import uno

_ctx = None

@setup
def setup_test(ctx):
    global _ctx
    _ctx = ctx

@native_test
def test_prompt_addin_metadata():
    from plugin.calc.prompt_addin import PromptFunction

    func = PromptFunction(_ctx)
    assert func.getProgrammaticFunctionName("PROMPT") == "prompt"
    assert func.getDisplayFunctionName("prompt") == "PROMPT"
    assert func.getArgumentCount("prompt") == 4
    assert func.getProgrammaticFunctionName("PYTHON") == ""
    assert func.getArgumentCount("python") == 0


# @native_test
# def test_python_addin_metadata():
#     from plugin.calc.python.addin import PythonFunction
#
#     func = PythonFunction(_ctx)
#     assert func.getProgrammaticFunctionName("PY") == "py"
#     assert func.getProgrammaticFunctionName("PYTHON") == "python"
#     assert func.getDisplayFunctionName("py") == "PY"
#     assert func.getDisplayFunctionName("python") == "PYTHON"
#     assert func.getArgumentCount("py") == 2
#     assert func.getArgumentCount("python") == 2
#     assert func.getArgumentName("python", 0) == "code"
#     assert "Python code" in func.getArgumentDescription("python", 0)
#     assert func.getArgumentName("python", 1) == "data"
#     assert func.getArgumentIsOptional("python", 1) is True
#     assert func.getProgrammaticFunctionName("PROMPT") == ""


@native_test
def test_python_addin_execution():
    from plugin.calc.python.addin import PythonFunction
    from plugin.calc.python.function import MATRIX_SCALAR_SESSIONS
    import unittest.mock

    if hasattr(MATRIX_SCALAR_SESSIONS, "sessions"):
        MATRIX_SCALAR_SESSIONS.sessions.clear()

    func = PythonFunction(_ctx)

    try:
        with unittest.mock.patch("plugin.calc.python.function.run_code_in_user_venv") as mock_run:
            mock_run.reset_mock()
            mock_run.return_value = {"status": "ok", "result": 42}
            res = func.py("result = 21 * 2")
            assert res == 42.0
            mock_run.assert_called_with(func.ctx, "result = 21 * 2", data=None, session_id=None)

            mock_run.reset_mock()
            mock_run.return_value = {"status": "ok", "result": 42}
            res = func.python("result = 21 * 2")
            assert res == 42.0
            mock_run.assert_called_with(func.ctx, "result = 21 * 2", data=None, session_id=None)

            mock_run.reset_mock()
            mock_run.return_value = {"status": "ok", "result": 6}
            res = func.python("result = sum(data)", (((1.0,), (2.0,), (3.0,)),))
            assert res == 6.0
            mock_run.assert_called_once()
            call_kw = mock_run.call_args
            assert call_kw[0][1] == "result = sum(data)"
            assert call_kw[1]["data"] == [1.0, 2.0, 3.0]

            mock_run.reset_mock()
            mock_run.return_value = {"status": "ok", "result": 9.0}
            col_a = ((1.0,), (2.0,), (3.0,))
            col_b = ((4.0,), (5.0,))
            res = func.python("result = sum(data[0]) + sum(data[1])", (col_a, col_b))
            assert res == 9.0
            wire = mock_run.call_args.kwargs["data"]
            from plugin.scripting.payload_codec import is_multi_data

            assert is_multi_data(wire)

            mock_run.reset_mock()
            mock_run.return_value = {"status": "ok", "result": 7919}
            res = func.python("result = sp.prime(int(data[0]))", 1000.0)
            assert res == 7919.0
            mock_run.assert_called_once()
            call_kw = mock_run.call_args
            assert call_kw[0][1] == "result = sp.prime(int(data[0]))"
            assert call_kw[1]["data"] == [1000.0]

            mock_run.return_value = {"status": "error", "message": "Syntax error"}
            res = func.python("bad code")
            assert "Error: Syntax error" in res

            mock_run.reset_mock()
            mock_run.return_value = {"status": "ok", "result": [2, 3, 5]}
            res = func.python("some code 1d")
            assert res == 2.0

            mock_run.reset_mock()
            mock_run.return_value = {"status": "ok", "result": [[2, 3], [5, 7]]}
            res = func.python("some code 2d")
            assert res == 2.0

            mock_run.reset_mock()
            mock_run.return_value = {"status": "ok", "result": [7919, 7927, 7933, 7937, 7949, 7951]}
            res = func.python("[sp.prime(x) for x in range(1000, 1006)]")
            assert res == 7919.0
            mock_run.assert_called_with(func.ctx, "[sp.prime(x) for x in range(1000, 1006)]", data=None, session_id=None)
    finally:
        if hasattr(MATRIX_SCALAR_SESSIONS, "sessions"):
            MATRIX_SCALAR_SESSIONS.sessions.clear()
