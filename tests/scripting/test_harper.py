# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Unit tests for the Harper Rust linter helper."""

from unittest.mock import MagicMock, patch
import json
import pytest
from plugin.scripting.venv.harper import lsp_range_to_offset, run_harper_check, HarperLSClient

def test_lsp_range_to_offset() -> None:
    text = "hello\nworld\n!"
    # line 0, char 0 -> 0
    assert lsp_range_to_offset(text, 0, 0) == 0
    # line 0, char 5 -> 5 (which is \n)
    assert lsp_range_to_offset(text, 0, 5) == 5
    # line 1, char 0 -> 6 (start of "world")
    assert lsp_range_to_offset(text, 1, 0) == 6
    # line 1, char 5 -> 11 (end of "world", which is \n)
    assert lsp_range_to_offset(text, 1, 5) == 11
    # line 2, char 0 -> 12 (start of "!")
    assert lsp_range_to_offset(text, 2, 0) == 12
    # line 5, char 0 -> len(text)
    assert lsp_range_to_offset(text, 5, 0) == len(text)


@patch("plugin.scripting.venv.harper._get_harper_binary")
@patch("subprocess.Popen")
def test_harper_ls_client_and_check(mock_popen: MagicMock, mock_get_bin: MagicMock) -> None:
    mock_get_bin.return_value = "/bin/harper-ls"
    
    # Configure mock process
    mock_proc = MagicMock()
    mock_popen.return_value = mock_proc
    mock_proc.poll.return_value = None
    
    # Mock communication:
    # 1. initialize response
    # 2. publishDiagnostics notification
    # 3. codeAction response
    # 4. shutdown response
    init_resp = json.dumps({
        "jsonrpc": "2.0",
        "id": 1,
        "result": {"capabilities": {}}
    }).encode("utf-8")
    
    # Stale diagnostic notification (version 0 when expecting version 1)
    diag_notification_stale = json.dumps({
        "jsonrpc": "2.0",
        "method": "textDocument/publishDiagnostics",
        "params": {
            "uri": "file:///tmp/writeragent_harper_lint_123.txt",
            "version": 0,
            "diagnostics": [
                {
                    "code": "SomeOldCode",
                    "message": "Old warning",
                    "range": {
                        "start": {"line": 0, "character": 0},
                        "end": {"line": 0, "character": 4}
                    },
                    "severity": 4,
                    "source": "Harper"
                }
            ]
        }
    }).encode("utf-8")

    diag_notification = json.dumps({
        "jsonrpc": "2.0",
        "method": "textDocument/publishDiagnostics",
        "params": {
            "uri": "file:///tmp/writeragent_harper_lint_123.txt",
            "version": 1,
            "diagnostics": [
                {
                    "code": "SentenceCapitalization",
                    "message": "Start with capital letter",
                    "range": {
                        "start": {"line": 0, "character": 0},
                        "end": {"line": 0, "character": 4}
                    },
                    "severity": 4,
                    "source": "Harper"
                }
            ]
        }
    }).encode("utf-8")
    
    code_action_resp = json.dumps({
        "jsonrpc": "2.0",
        "id": 2,
        "result": [
            {
                "kind": "quickfix",
                "title": "Replace with: “This”",
                "edit": {
                    "changes": {
                        "file:///tmp/writeragent_harper_lint_123.txt": [
                            {
                                "newText": "This",
                                "range": {
                                    "start": {"line": 0, "character": 0},
                                    "end": {"line": 0, "character": 4}
                                }
                            }
                        ]
                    }
                }
            }
        ]
    }).encode("utf-8")
    
    shutdown_resp = json.dumps({
        "jsonrpc": "2.0",
        "id": 3,
        "result": None
    }).encode("utf-8")

    # Construct the stream of reads
    # Format is Header \r\n\r\n Body
    def make_lsp_chunk(body: bytes) -> bytes:
        return f"Content-Length: {len(body)}\r\n\r\n".encode("utf-8") + body

    responses = [
        make_lsp_chunk(init_resp),
        make_lsp_chunk(diag_notification_stale),
        make_lsp_chunk(diag_notification),
        make_lsp_chunk(code_action_resp),
        make_lsp_chunk(shutdown_resp)
    ]
    
    # We feed the mocked stdout lines
    # Readline will get the headers first, then empty line, then read() gets the body.
    # To mock this easily, we can supply a side effect for readline/read on a BytesIO object.
    import io
    stream = io.BytesIO(b"".join(responses))
    mock_proc.stdout.readline = stream.readline
    mock_proc.stdout.read = stream.read
    
    with patch("time.time_ns", return_value=123):
        res = run_harper_check("this is text", "/tmp")
        
    assert "errors" in res
    assert len(res["errors"]) == 1
    err = res["errors"][0]
    assert err["wrong"] == "this"
    assert err["correct"] == "This"
    assert err["n_error_start"] == 0
    assert err["n_error_length"] == 4
    assert err["rule_identifier"] == "harper||SentenceCapitalization"
    assert err["suggestions"] == ["This"]


@patch("plugin.scripting.venv.harper._get_harper_binary")
@patch("subprocess.Popen")
def test_harper_ls_timeout(mock_popen: MagicMock, mock_get_bin: MagicMock) -> None:
    mock_get_bin.return_value = "/bin/harper-ls"
    
    # Configure mock process
    mock_proc = MagicMock()
    mock_popen.return_value = mock_proc
    mock_proc.poll.return_value = None
    
    init_resp = json.dumps({
        "jsonrpc": "2.0",
        "id": 1,
        "result": {"capabilities": {}}
    }).encode("utf-8")
    
    def make_lsp_chunk(body: bytes) -> bytes:
        return f"Content-Length: {len(body)}\r\n\r\n".encode("utf-8") + body
        
    import io
    stream = io.BytesIO(make_lsp_chunk(init_resp))
    mock_proc.stdout.readline = stream.readline
    mock_proc.stdout.read = stream.read
    
    client = HarperLSClient("/bin/harper-ls")
    
    # Mock queue.get to simulate a timeout
    import queue
    with patch.object(client.stdout_queue, "get", side_effect=queue.Empty):
        with pytest.raises(TimeoutError):
            client.lint("test text")

