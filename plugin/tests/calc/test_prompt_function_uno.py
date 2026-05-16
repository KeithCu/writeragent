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
    assert func.getArgumentCount("python") == 1
    
    assert func.getArgumentName("python", 0) == "code"
    assert "Python code" in func.getArgumentDescription("python", 0)

@native_test
def test_prompt_function_python_execution():
    from plugin.calc.prompt_function import PromptFunction
    import unittest.mock
    
    func = PromptFunction(_ctx)
    
    # Mock run_blocking_in_thread to avoid actual subprocess spawning in this test
    # or we can let it run if sys.executable is used.
    
    with unittest.mock.patch("plugin.calc.prompt_function.run_blocking_in_thread") as mock_run:
        # Success case
        mock_run.return_value = {"status": "ok", "result": 42}
        res = func.python("result = 21 * 2")
        assert res == 42
        
        # Error case
        mock_run.return_value = {"status": "error", "message": "Syntax error"}
        res = func.python("bad code")
        assert "Error: Syntax error" in res
