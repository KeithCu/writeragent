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
def test_prompt_function_metadata():
    from plugin.calc.prompt_function import PromptFunction
    func = PromptFunction(_ctx)
    
    # Metadata checks
    assert func.getProgrammaticFunctionName("PROMPT") == "prompt"
    assert func.getDisplayFunctionName("prompt") == "PROMPT"
    assert func.getProgrammaticFunctionName("PYTHON") == "python"
    assert func.getDisplayFunctionName("python") == "PYTHON"
    
    assert func.getArgumentCount("prompt") == 4
    assert func.getArgumentCount("python") == 2

    assert func.getArgumentName("python", 0) == "code"
    assert "Python code" in func.getArgumentDescription("python", 0)
    assert func.getArgumentName("python", 1) == "data"
    assert func.getArgumentIsOptional("python", 1) is True

@native_test
def test_prompt_function_python_execution():
    from plugin.calc.prompt_function import PromptFunction, _MATRIX_SCALAR_SESSIONS
    import unittest.mock
    
    # Ensure a clean state for this test session
    if hasattr(_MATRIX_SCALAR_SESSIONS, "sessions"):
        _MATRIX_SCALAR_SESSIONS.sessions.clear()
        
    func = PromptFunction(_ctx)
    
    # Mock run_blocking_in_thread to avoid actual subprocess spawning in this test
    # or we can let it run if sys.executable is used.
    try:
        with unittest.mock.patch("plugin.calc.prompt_function.run_code_in_user_venv") as mock_run:
            # Success case
            mock_run.return_value = {"status": "ok", "result": 42}
            res = func.python("result = 21 * 2")
            assert res == 42.0
            mock_run.assert_called_with(func.ctx, "result = 21 * 2", data=None)

            # Range data forwarded
            mock_run.reset_mock()
            mock_run.return_value = {"status": "ok", "result": 6}
            res = func.python("result = sum(data)", ((1.0,), (2.0,), (3.0,)))
            assert res == 6.0
            mock_run.assert_called_once()
            call_kw = mock_run.call_args
            assert call_kw[0][1] == "result = sum(data)"
            assert call_kw[1]["data"] == [1.0, 2.0, 3.0]

            # Single cell numeric data forwarded as data, not discarded as None
            mock_run.reset_mock()
            mock_run.return_value = {"status": "ok", "result": 7919}
            res = func.python("result = sp.prime(int(data[0]))", 1000.0)
            assert res == 7919.0
            mock_run.assert_called_once()
            call_kw = mock_run.call_args
            assert call_kw[0][1] == "result = sp.prime(int(data[0]))"
            assert call_kw[1]["data"] == [1000.0]

            # Error case
            mock_run.return_value = {"status": "error", "message": "Syntax error"}
            res = func.python("bad code")
            assert "Error: Syntax error" in res

            # 1D array return matrix formatting
            mock_run.reset_mock()
            mock_run.return_value = {"status": "ok", "result": [2, 3, 5]}
            res = func.python("some code 1d")
            assert res == 2.0

            # 2D array return matrix formatting
            mock_run.reset_mock()
            mock_run.return_value = {"status": "ok", "result": [[2, 3], [5, 7]]}
            res = func.python("some code 2d")
            assert res == 2.0

            # 1000th-1005th primes sequence return test with auto-imported sp
            mock_run.reset_mock()
            mock_run.return_value = {"status": "ok", "result": [7919, 7927, 7933, 7937, 7949, 7951]}
            res = func.python("[sp.prime(x) for x in range(1000, 1006)]")
            assert res == 7919.0
            mock_run.assert_called_with(func.ctx, "[sp.prime(x) for x in range(1000, 1006)]", data=None)
    finally:
        if hasattr(_MATRIX_SCALAR_SESSIONS, "sessions"):
            _MATRIX_SCALAR_SESSIONS.sessions.clear()
