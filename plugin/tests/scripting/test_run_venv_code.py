# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
"""Integration tests for the interactive venv RPC bridge."""

import json
from unittest.mock import MagicMock, patch

import pytest

from plugin.scripting.run_venv_code import _build_runner_script

# We need to setup UNO mocks because plugin.main and other modules import uno
from plugin.tests.testing_utils import setup_uno_mocks
setup_uno_mocks()

@patch("plugin.scripting.run_venv_code.subprocess.Popen")
@patch("plugin.scripting.run_venv_code.select.select")
def test_interactive_runner_tool_call_success(mock_select, mock_popen):
    from plugin.scripting.run_venv_code import VenvInteractiveRunner
    
    # Setup mock process
    mock_proc = MagicMock()
    mock_popen.return_value = mock_proc
    mock_proc.returncode = 0
    # Process exits quickly after data
    mock_proc.poll.side_effect = [None, None, 0, 0, 0]
    
    # Mock stdout with a tool call followed by a result
    mock_proc.stdout.readline.side_effect = [
        '{"type": "tool_call", "tool": "get_document_content", "args": {}, "id": "123"}\n',
        '__WRITERAGENT_VENV_RESULT__"success"\n',
        ''
    ]
    
    # Mock select.select
    mock_select.side_effect = [
        ([mock_proc.stdout], [], []), # Ready for tool call
        ([mock_proc.stdout], [], []), # Ready for final result
        ([], [], []),                 # poll=None, continue
        ([], [], []),                 # poll=None, continue
        ([], [], []),                 # poll=0, break
    ]
    
    ctx = MagicMock()
    runner = VenvInteractiveRunner("/bin/python", "/tmp/script.py", {}, 60, ctx)
    
    # Patch the actual tool execution
    import plugin.main
    with patch("plugin.main.get_tools") as mock_get_tools:
        mock_registry = mock_get_tools.return_value
        mock_registry.execute.return_value = {"content": "Doc content"}
        
        with patch("plugin.framework.uno_context.get_active_document", return_value=MagicMock()):
            with patch("plugin.doc.document_helpers.get_document_type", return_value=MagicMock()):
                res = runner.run()
    
    assert res["status"] == "ok"
    assert res["result"] == "success"

@patch("plugin.scripting.run_venv_code.subprocess.Popen")
@patch("plugin.scripting.run_venv_code.select.select")
def test_interactive_runner_whitelist_enforcement(mock_select, mock_popen):
    from plugin.scripting.run_venv_code import VenvInteractiveRunner
    
    mock_proc = MagicMock()
    mock_popen.return_value = mock_proc
    mock_proc.returncode = 0
    mock_proc.poll.side_effect = [None, None, 0, 0, 0]
    
    mock_proc.stdout.readline.side_effect = [
        '{"type": "tool_call", "tool": "forbidden_tool", "args": {}, "id": "666"}\n',
        '__WRITERAGENT_VENV_RESULT__"failed"\n',
        ''
    ]
    mock_select.side_effect = [
        ([mock_proc.stdout], [], []),
        ([mock_proc.stdout], [], []),
        ([], [], []),
        ([], [], []),
        ([], [], []),
    ]
    
    ctx = MagicMock()
    runner = VenvInteractiveRunner("/bin/python", "/tmp/s.py", {}, 10, ctx, active_domain="writer")
    
    import plugin.main
    with patch("plugin.main.get_tools") as mock_get_tools:
        with patch("plugin.scripting.writeragent_api.DOMAIN_TOOLS", {"writer": ["allowed_tool"]}):
            res = runner.run()
    
    assert res["status"] == "ok"
    # Check that error was written back to stdin
    written = "".join(call.args[0] for call in mock_proc.stdin.write.call_args_list)
    assert "Access denied" in written


@patch("plugin.scripting.run_venv_code.subprocess.Popen")
@patch("plugin.scripting.run_venv_code.select.select")
def test_interactive_runner_python_tool_domain_whitelist(mock_select, mock_popen):
    from plugin.scripting.run_venv_code import VenvInteractiveRunner
    
    mock_proc = MagicMock()
    mock_popen.return_value = mock_proc
    mock_proc.returncode = 0
    mock_proc.poll.side_effect = [None, None, 0, 0, 0]
    
    mock_proc.stdout.readline.side_effect = [
        '{"type": "tool_call", "tool": "writer_tool", "args": {}, "id": "1"}\n',
        '__WRITERAGENT_VENV_RESULT__"done"\n',
        ''
    ]
    mock_select.side_effect = [
        ([mock_proc.stdout], [], []),
        ([mock_proc.stdout], [], []),
        ([], [], []),
        ([], [], []),
        ([], [], []),
    ]
    
    ctx = MagicMock()
    runner = VenvInteractiveRunner("/bin/python", "/tmp/s.py", {}, 10, ctx, active_domain="python", python_tool_domain="writer")
    
    import plugin.main
    with patch("plugin.main.get_tools") as mock_get_tools:
        mock_registry = mock_get_tools.return_value
        mock_registry.execute.return_value = {"status": "ok"}
        
        with patch("plugin.framework.uno_context.get_active_document", return_value=MagicMock()):
            with patch("plugin.doc.document_helpers.get_document_type", return_value=MagicMock()):
                with patch("plugin.scripting.writeragent_api.DOMAIN_TOOLS", {"writer": ["writer_tool"]}):
                    res = runner.run()
    
    assert res["status"] == "ok"
    mock_registry.execute.assert_called_once()
    assert mock_registry.execute.call_args[0][0] == "writer_tool"


def test_build_runner_script_injects_data():
    script = _build_runner_script("result = sum(data[0])", data=[[1, 2], [3, 4]])
    assert "data = _json.loads" in script
    assert "result = sum(data[0])" in script
    assert "__WRITERAGENT_VENV_RESULT__" in script
    ns: dict = {}
    exec(script, ns)  # noqa: S102 — test fixture only
    assert ns.get("_wa") == 3


def test_build_runner_script_no_data_omits_preamble():
    script = _build_runner_script("result = 1")
    assert "data = _json.loads" not in script
