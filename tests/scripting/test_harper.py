# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Unit tests for the Harper Rust linter helper."""

from unittest.mock import MagicMock, patch
import json
import pytest
from plugin.scripting.venv.harper import lsp_range_to_offset, run_harper_check, HarperLSClient
import plugin.scripting.venv.harper as harper_module


@pytest.fixture(autouse=True)
def _reset_harper_client_cache() -> None:
    """Each run_harper_check test owns a fresh LSP client and mocked stdout stream."""
    for client in harper_module._HARPER_CLIENT_CACHE.values():
        client.close()
    harper_module._HARPER_CLIENT_CACHE.clear()


def test_lsp_range_to_offset_single_line() -> None:
    """Fast path: typical one-line sentence with no embedded newlines."""
    text = "This is a test sentence."
    assert lsp_range_to_offset(text, 0, 0) == 0
    assert lsp_range_to_offset(text, 0, 5) == 5  # space after "This"
    assert lsp_range_to_offset(text, 0, len(text)) == len(text)
    assert lsp_range_to_offset(text, 0, len(text) + 10) == len(text)  # clamp past end
    assert lsp_range_to_offset(text, 1, 0) == len(text)  # only one line
    assert lsp_range_to_offset("", 0, 0) == 0


def test_lsp_range_to_offset_multiline() -> None:
    """Multiline path: soft breaks and explicit line breaks inside one sentence."""
    text = "hello\nworld\n!"
    assert lsp_range_to_offset(text, 0, 0) == 0
    assert lsp_range_to_offset(text, 0, 5) == 5  # newline after "hello"
    assert lsp_range_to_offset(text, 1, 0) == 6  # start of "world"
    assert lsp_range_to_offset(text, 1, 5) == 11  # newline after "world"
    assert lsp_range_to_offset(text, 2, 0) == 12  # start of "!"
    assert lsp_range_to_offset(text, 5, 0) == len(text)  # line out of range

    soft_break = "Hello,\nworld."
    assert lsp_range_to_offset(soft_break, 1, 0) == 7  # "world."
    assert lsp_range_to_offset(soft_break, 1, 5) == 12  # end of "world."


def test_lsp_range_to_offset_crlf() -> None:
    """Multiline path must count \\r\\n terminators (splitlines keepends)."""
    text = "a\r\nb"
    assert lsp_range_to_offset(text, 0, 0) == 0
    assert lsp_range_to_offset(text, 1, 0) == 3  # start of "b"


def _make_lsp_chunk(body: bytes) -> bytes:
    return f"Content-Length: {len(body)}\r\n\r\n".encode("utf-8") + body


def _mock_harper_lsp_stream(responses: list[bytes]) -> MagicMock:
    import io

    mock_proc = MagicMock()
    mock_proc.poll.return_value = None
    stream = io.BytesIO(b"".join(responses))
    mock_proc.stdout.readline = stream.readline
    mock_proc.stdout.read = stream.read
    return mock_proc


@patch("plugin.scripting.venv.harper._get_harper_binary")
@patch("subprocess.Popen")
def test_harper_ls_client_and_check(mock_popen: MagicMock, mock_get_bin: MagicMock) -> None:
    mock_get_bin.return_value = "/bin/harper-ls"
    mock_popen.return_value = _mock_harper_lsp_stream(
        [
            _make_lsp_chunk(
                json.dumps({"jsonrpc": "2.0", "id": 1, "result": {"capabilities": {}}}).encode("utf-8")
            ),
            _make_lsp_chunk(
                json.dumps(
                    {
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
                                        "end": {"line": 0, "character": 4},
                                    },
                                    "severity": 4,
                                    "source": "Harper",
                                }
                            ],
                        },
                    }
                ).encode("utf-8")
            ),
            _make_lsp_chunk(
                json.dumps(
                    {
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
                                        "end": {"line": 0, "character": 4},
                                    },
                                    "severity": 4,
                                    "source": "Harper",
                                }
                            ],
                        },
                    }
                ).encode("utf-8")
            ),
            _make_lsp_chunk(
                json.dumps(
                    {
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
                                                    "end": {"line": 0, "character": 4},
                                                },
                                            }
                                        ]
                                    }
                                },
                            }
                        ],
                    }
                ).encode("utf-8")
            ),
            _make_lsp_chunk(json.dumps({"jsonrpc": "2.0", "id": 3, "result": None}).encode("utf-8")),
        ]
    )

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
def test_harper_check_soft_line_break_offsets(mock_popen: MagicMock, mock_get_bin: MagicMock) -> None:
    """Diagnostic on line 1 maps to offset after embedded newline in one sentence."""
    mock_get_bin.return_value = "/bin/harper-ls"
    mock_popen.return_value = _mock_harper_lsp_stream(
        [
            _make_lsp_chunk(
                json.dumps({"jsonrpc": "2.0", "id": 1, "result": {"capabilities": {}}}).encode("utf-8")
            ),
            _make_lsp_chunk(
                json.dumps(
                    {
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
                                        "start": {"line": 1, "character": 0},
                                        "end": {"line": 1, "character": 5},
                                    },
                                    "severity": 4,
                                    "source": "Harper",
                                }
                            ],
                        },
                    }
                ).encode("utf-8")
            ),
            _make_lsp_chunk(
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": 2,
                        "result": [
                            {
                                "kind": "quickfix",
                                "title": "Replace with: World",
                                "edit": {
                                    "changes": {
                                        "file:///tmp/writeragent_harper_lint_123.txt": [
                                            {
                                                "newText": "World",
                                                "range": {
                                                    "start": {"line": 1, "character": 0},
                                                    "end": {"line": 1, "character": 5},
                                                },
                                            }
                                        ]
                                    }
                                },
                            }
                        ],
                    }
                ).encode("utf-8")
            ),
            _make_lsp_chunk(json.dumps({"jsonrpc": "2.0", "id": 3, "result": None}).encode("utf-8")),
        ]
    )

    sentence = "Hello,\nworld."
    with patch("time.time_ns", return_value=123):
        res = run_harper_check(sentence, "/tmp")

    assert len(res["errors"]) == 1
    err = res["errors"][0]
    assert err["wrong"] == "world"
    assert err["n_error_start"] == 7
    assert err["n_error_length"] == 5
    assert err["correct"] == "World"


@patch("plugin.scripting.venv.harper._get_harper_binary")
@patch("subprocess.Popen")
def test_harper_ls_timeout(mock_popen: MagicMock, mock_get_bin: MagicMock) -> None:
    mock_get_bin.return_value = "/bin/harper-ls"
    mock_popen.return_value = _mock_harper_lsp_stream(
        [_make_lsp_chunk(json.dumps({"jsonrpc": "2.0", "id": 1, "result": {"capabilities": {}}}).encode("utf-8"))]
    )

    client = HarperLSClient("/bin/harper-ls")

    import queue

    with patch.object(client.stdout_queue, "get", side_effect=queue.Empty):
        with pytest.raises(TimeoutError):
            client.lint("test text")

