# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Unit tests for Harper LSP client, binary install, and grammar-queue host entry."""

from io import BytesIO
from pathlib import Path
from unittest.mock import MagicMock, patch
import inspect
import json
import logging
import os
import queue
import threading
import time
import pytest

from plugin.contrib.lsp.json_rpc_framing import read_exactly
from plugin.writer.locale.harper import (
    HARPER_SLOW_RESULT_MS,
    HarperLSClient,
    HarperRuntimeState,
    _harper_lsp_settings,
    _pump_grammar_status_ui,
    harper_runtime_is_ready,
    harper_try_lint,
    lsp_range_to_offset,
    maybe_start_harper_async,
    run_harper_check,
    run_harper_lint,
    shutdown_harper_runtime,
    warn_if_harper_result_slow,
)
from plugin.writer.locale.harper_binary import (
    HarperReleaseAsset,
    _fetch_latest_release_asset,
    _read_installed_version,
)
import plugin.writer.locale.harper as harper_module
import plugin.writer.locale.harper_binary as harper_binary_module
from tests.harness.strip_bundle import module_source_contains


@pytest.fixture(autouse=True)
def _reset_harper_client_cache() -> None:
    """Each run_harper_lint test owns a fresh LSP client and mocked stdout stream."""
    shutdown_harper_runtime()
    harper_binary_module._release_cache.clear()
    yield
    shutdown_harper_runtime()
    harper_binary_module._release_cache.clear()


def test_harper_client_parameterizes_stdout_queue() -> None:
    """Bare queue.Queue leftovers trip reportMissingTypeArgument."""
    src = inspect.getsource(HarperLSClient.__init__)
    assert "stdout_queue: queue.Queue[dict[str, Any] | None]" in src


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


def test_lsp_range_to_offset_utf16_surrogate_pair() -> None:
    """LSP character offsets count UTF-16 code units, not Python code points."""
    text = "a👋b"
    assert lsp_range_to_offset(text, 0, 0) == 0
    assert lsp_range_to_offset(text, 0, 1) == 1  # after "a"
    assert lsp_range_to_offset(text, 0, 3) == 2  # after emoji (2 UTF-16 units)
    assert lsp_range_to_offset(text, 0, 4) == 3  # start of "b"


def _make_lsp_chunk(body: bytes) -> bytes:
    return f"Content-Length: {len(body)}\r\n\r\n".encode("utf-8") + body


def _mock_harper_lsp_stream(responses: list[bytes]) -> MagicMock:
    mock_proc = MagicMock()
    mock_proc.poll.return_value = None
    stream = BytesIO(b"".join(responses))
    mock_proc.stdout.readline = stream.readline
    mock_proc.stdout.read = stream.read
    return mock_proc


def test_harper_lsp_settings_dialect_mapping() -> None:
    assert _harper_lsp_settings("en-GB", "/tmp")["harper-ls"]["dialect"] == "British"
    assert _harper_lsp_settings("en-AU", "/tmp")["harper-ls"]["dialect"] == "Australian"
    assert _harper_lsp_settings("en-CA", "/tmp")["harper-ls"]["dialect"] == "Canadian"
    assert _harper_lsp_settings("en-IN", "/tmp")["harper-ls"]["dialect"] == "Indian"
    assert _harper_lsp_settings("en-US", "/tmp")["harper-ls"]["dialect"] == "American"
    assert _harper_lsp_settings("en-GB", "/tmp")["harper-ls"]["userDictPath"] == str(Path("/tmp") / "harper-dictionary.txt")

@patch("plugin.writer.locale.harper._get_harper_binary")
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
        res = run_harper_lint("this is text", "/tmp")

    assert "errors" in res
    assert len(res["errors"]) == 1
    err = res["errors"][0]
    assert err["wrong"] == "this"
    assert err["correct"] == "This"
    assert err["n_error_start"] == 0
    assert err["n_error_length"] == 4
    assert err["rule_identifier"] == "harper||SentenceCapitalization"
    assert err["suggestions"] == ["This"]


@patch("plugin.writer.locale.harper._get_harper_binary")
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
        res = run_harper_lint(sentence, "/tmp")

    assert len(res["errors"]) == 1
    err = res["errors"][0]
    assert err["wrong"] == "world"
    assert err["n_error_start"] == 7
    assert err["n_error_length"] == 5
    assert err["correct"] == "World"


@patch("plugin.writer.locale.harper._get_harper_binary")
@patch("subprocess.Popen")
def test_harper_ls_timeout(mock_popen: MagicMock, mock_get_bin: MagicMock) -> None:
    mock_get_bin.return_value = "/bin/harper-ls"
    mock_popen.return_value = _mock_harper_lsp_stream(
        [_make_lsp_chunk(json.dumps({"jsonrpc": "2.0", "id": 1, "result": {"capabilities": {}}}).encode("utf-8"))]
    )

    client = HarperLSClient("/bin/harper-ls")

    with patch.object(client.stdout_queue, "get", side_effect=queue.Empty):
        with pytest.raises(TimeoutError):
            client.lint("test text")


def _bare_harper_client() -> HarperLSClient:
    """Client shell for queue tests. Does not spawn harper-ls."""
    client = HarperLSClient.__new__(HarperLSClient)
    client.proc = MagicMock()
    client.proc.poll.return_value = None
    client._bcp47 = "en-US"
    client.user_config_dir = ""
    client._lsp_settings = {}
    client._heartbeat_fn = None
    client._doc_opened = True
    client._doc_version = 0
    client._lint_cancel = None
    client.uri = "file:///tmp/writeragent_harper_lint_eof.txt"
    client.request_id = 0
    client.stdout_queue = queue.Queue()
    client.stdout_thread = None
    client.binary_path = "/bin/harper-ls"
    return client


def test_collect_diagnostics_empty_publish_is_clean() -> None:
    """A real publish with an empty list is a clean sentence, even if EOF follows."""
    client = _bare_harper_client()
    client.stdout_queue.put(
        {
            "jsonrpc": "2.0",
            "method": "textDocument/publishDiagnostics",
            "params": {"uri": client.uri, "version": 1, "diagnostics": []},
        }
    )
    client.stdout_queue.put(None)
    assert client._collect_diagnostics(1, time.monotonic() + 1) == []


def test_collect_diagnostics_sentinel_is_not_clean() -> None:
    client = _bare_harper_client()
    client.stdout_queue.put(None)
    with pytest.raises(RuntimeError, match="closed before publishDiagnostics"):
        client._collect_diagnostics(1, time.monotonic() + 1)


def test_collect_diagnostics_dead_process_is_not_clean() -> None:
    client = _bare_harper_client()
    client.proc.poll.return_value = 1
    started = time.monotonic()
    with pytest.raises(RuntimeError, match="died before publishDiagnostics"):
        client._collect_diagnostics(1, time.monotonic() + 30)
    assert time.monotonic() - started < 1.0


def test_collect_diagnostics_expired_deadline_is_not_clean() -> None:
    client = _bare_harper_client()
    with pytest.raises(TimeoutError):
        client._collect_diagnostics(1, time.monotonic() - 1)


def test_lint_stdout_eof_raises_instead_of_empty() -> None:
    client = _bare_harper_client()
    client.stdout_queue.put(None)
    with patch.object(client, "_write", lambda _payload: None), pytest.raises(RuntimeError, match="closed"):
        client.lint("Hello.")


@patch("plugin.writer.locale.harper._get_harper_binary")
@patch("subprocess.Popen")
def test_harper_check_empty_diagnostics(mock_popen: MagicMock, mock_get_bin: MagicMock) -> None:
    mock_get_bin.return_value = "/bin/harper-ls"
    mock_popen.return_value = _mock_harper_lsp_stream(
        [
            _make_lsp_chunk(json.dumps({"jsonrpc": "2.0", "id": 1, "result": {"capabilities": {}}}).encode("utf-8")),
            _make_lsp_chunk(
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "method": "textDocument/publishDiagnostics",
                        "params": {
                            "uri": "file:///tmp/writeragent_harper_lint_123.txt",
                            "version": 1,
                            "diagnostics": [],
                        },
                    }
                ).encode("utf-8")
            ),
        ]
    )

    with patch("time.time_ns", return_value=123):
        res = run_harper_lint("clean sentence.", "/tmp")

    assert res == {"errors": []}


@patch("plugin.writer.locale.harper._get_harper_binary")
@patch("subprocess.Popen")
def test_harper_check_zero_width_diagnostic(mock_popen: MagicMock, mock_get_bin: MagicMock) -> None:
    mock_get_bin.return_value = "/bin/harper-ls"
    mock_popen.return_value = _mock_harper_lsp_stream(
        [
            _make_lsp_chunk(json.dumps({"jsonrpc": "2.0", "id": 1, "result": {"capabilities": {}}}).encode("utf-8")),
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
                                    "code": "PointDiag",
                                    "message": "Insert comma",
                                    "range": {
                                        "start": {"line": 0, "character": 5},
                                        "end": {"line": 0, "character": 5},
                                    },
                                }
                            ],
                        },
                    }
                ).encode("utf-8")
            ),
            _make_lsp_chunk(json.dumps({"jsonrpc": "2.0", "id": 2, "result": []}).encode("utf-8")),
        ]
    )

    with patch("time.time_ns", return_value=123):
        res = run_harper_lint("hello world", "/tmp")

    assert len(res["errors"]) == 1
    err = res["errors"][0]
    assert err["wrong"] == ""
    assert err["n_error_start"] == 5
    assert err["n_error_length"] == 0


@patch("plugin.writer.locale.harper._get_harper_binary")
@patch("subprocess.Popen")
def test_harper_workspace_configuration_dialect(mock_popen: MagicMock, mock_get_bin: MagicMock) -> None:
    mock_get_bin.return_value = "/bin/harper-ls"
    mock_proc = _mock_harper_lsp_stream(
        [
            _make_lsp_chunk(json.dumps({"jsonrpc": "2.0", "id": 1, "result": {"capabilities": {}}}).encode("utf-8")),
            _make_lsp_chunk(
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": 99,
                        "method": "workspace/configuration",
                        "params": {"items": [{"section": "harper-ls"}]},
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
                            "diagnostics": [],
                        },
                    }
                ).encode("utf-8")
            ),
        ]
    )
    mock_popen.return_value = mock_proc

    with patch("time.time_ns", return_value=123):
        run_harper_lint("colour is fine.", "/tmp", bcp47="en-GB")

    written = b"".join(call.args[0] for call in mock_proc.stdin.write.call_args_list if call.args)
    assert b'"dialect": "British"' in written


@patch("plugin.writer.locale.harper._get_harper_binary")
def test_harper_run_harper_lint_retries_after_failure(mock_get_bin: MagicMock) -> None:
    mock_get_bin.return_value = "/bin/harper-ls"
    broken_client = MagicMock()
    broken_client.lint.side_effect = TimeoutError("Harper LSP operation timed out")
    fresh_client = MagicMock()
    fresh_client.lint.return_value = []

    with patch("plugin.writer.locale.harper._get_or_create_client", return_value=broken_client), \
         patch("plugin.writer.locale.harper.HarperLSClient", return_value=fresh_client) as mock_ctor:
        res = run_harper_lint("retry me.", "/tmp")

    assert res == {"errors": []}
    broken_client.close.assert_called_once()
    mock_ctor.assert_called_once()
    fresh_client.lint.assert_called_once_with("retry me.", bcp47="en-US", heartbeat_fn=None)


def test_read_exactly_handles_partial_reads() -> None:
    payload = b"abcdefghij"

    class PartialReader:
        def __init__(self) -> None:
            self._parts = iter([payload[:3], payload[3:7], payload[7:]])

        def read(self, n: int) -> bytes:
            return next(self._parts, b"")

    assert read_exactly(PartialReader(), len(payload)) == payload


def test_read_installed_version_sidecar(tmp_path: Path) -> None:
    harper_dir = tmp_path / "harper"
    harper_dir.mkdir()
    assert _read_installed_version(harper_dir) is None
    (harper_dir / "harper-ls.version").write_text("2.6.0", encoding="utf-8")
    assert _read_installed_version(harper_dir) == "2.6.0"


def _sample_release(version: str = "2.6.0") -> HarperReleaseAsset:
    return HarperReleaseAsset(
        version=version,
        asset_name="harper-ls-x86_64-unknown-linux-gnu.tar.gz",
        download_url=f"https://github.com/Automattic/harper/releases/download/v{version}/harper-ls-x86_64-unknown-linux-gnu.tar.gz",
        sha256="abc123",
    )


def _harper_binary_name() -> str:
    return "harper-ls.exe" if os.name == "nt" else "harper-ls"


@patch("plugin.writer.locale.harper_binary._download_harper_binary")
@patch("plugin.writer.locale.harper_binary._fetch_latest_release_asset")
def test_get_harper_binary_redownloads_when_latest_changes(
    mock_fetch: MagicMock,
    mock_download: MagicMock,
    tmp_path: Path,
) -> None:
    mock_fetch.return_value = _sample_release("2.7.0")
    harper_dir = tmp_path / "harper"
    harper_dir.mkdir()
    binary_path = harper_dir / _harper_binary_name()
    binary_path.write_bytes(b"old")
    (harper_dir / "harper-ls.version").write_text("2.6.0", encoding="utf-8")

    with patch("plugin.writer.locale.harper_binary.shutil.which", return_value=None):
        path = harper_binary_module._get_harper_binary(str(tmp_path))

    mock_download.assert_called_once_with(binary_path, mock_fetch.return_value, heartbeat_fn=None)
    assert path == str(binary_path)


@patch("plugin.writer.locale.harper_binary._download_harper_binary")
@patch("plugin.writer.locale.harper_binary._fetch_latest_release_asset")
def test_get_harper_binary_skips_download_when_up_to_date(
    mock_fetch: MagicMock,
    mock_download: MagicMock,
    tmp_path: Path,
) -> None:
    mock_fetch.return_value = _sample_release("2.6.0")
    harper_dir = tmp_path / "harper"
    harper_dir.mkdir()
    binary_path = harper_dir / _harper_binary_name()
    binary_path.write_bytes(b"current")
    (harper_dir / "harper-ls.version").write_text("2.6.0", encoding="utf-8")

    with patch("plugin.writer.locale.harper_binary.shutil.which", return_value=None):
        path = harper_binary_module._get_harper_binary(str(tmp_path))

    mock_download.assert_not_called()
    assert path == str(binary_path)


# TEMP(2026-08): Remove with _cleanup_harper_install_leftovers after ~2026-11.
@patch("plugin.writer.locale.harper_binary._download_harper_binary")
@patch("plugin.writer.locale.harper_binary._fetch_latest_release_asset")
def test_get_harper_binary_removes_untar_leftover(
    mock_fetch: MagicMock,
    mock_download: MagicMock,
    tmp_path: Path,
) -> None:
    mock_fetch.return_value = _sample_release("2.6.0")
    harper_dir = tmp_path / "harper"
    harper_dir.mkdir()
    binary_path = harper_dir / _harper_binary_name()
    binary_path.write_bytes(b"current")
    (harper_dir / "harper-ls.version").write_text("2.6.0", encoding="utf-8")
    leftover = harper_dir / "harper-ls-x86_64-unknown-linux-gnu.tar.gz.untar" / "harper-ls"
    leftover.parent.mkdir(parents=True)
    leftover.write_bytes(b"duplicate-50mb")

    with patch("plugin.writer.locale.harper_binary.shutil.which", return_value=None):
        path = harper_binary_module._get_harper_binary(str(tmp_path))

    mock_download.assert_not_called()
    assert path == str(binary_path)
    assert binary_path.read_bytes() == b"current"
    assert not leftover.exists()
    assert not leftover.parent.exists()


# TEMP(2026-08): Remove with _cleanup_harper_install_leftovers after ~2026-11.
@patch("plugin.writer.locale.harper_binary._download_harper_binary")
@patch("plugin.writer.locale.harper_binary._fetch_latest_release_asset")
def test_get_harper_binary_removes_windows_unzip_leftover(
    mock_fetch: MagicMock,
    mock_download: MagicMock,
    tmp_path: Path,
) -> None:
    # Cleanup is OS-agnostic; exercise the .zip.unzip leftover shape without
    # patching os.name (that would force WindowsPath on non-Windows hosts).
    mock_fetch.return_value = _sample_release("2.6.0")
    harper_dir = tmp_path / "harper"
    harper_dir.mkdir()
    binary_path = harper_dir / _harper_binary_name()
    binary_path.write_bytes(b"current")
    (harper_dir / "harper-ls.version").write_text("2.6.0", encoding="utf-8")
    leftover = harper_dir / "harper-ls-x86_64-pc-windows-msvc.zip.unzip" / "harper-ls.exe"
    leftover.parent.mkdir(parents=True)
    leftover.write_bytes(b"duplicate-50mb")

    with patch("plugin.writer.locale.harper_binary.shutil.which", return_value=None):
        path = harper_binary_module._get_harper_binary(str(tmp_path))

    mock_download.assert_not_called()
    assert path == str(binary_path)
    assert binary_path.read_bytes() == b"current"
    assert not leftover.exists()
    assert not leftover.parent.exists()


@patch("plugin.writer.locale.harper_binary._download_harper_binary")
@patch("plugin.writer.locale.harper_binary._fetch_latest_release_asset")
def test_migrate_legacy_bin_install_moves_binary(
    mock_fetch: MagicMock,
    mock_download: MagicMock,
    tmp_path: Path,
) -> None:
    mock_fetch.return_value = _sample_release("2.6.0")
    legacy_dir = tmp_path / "bin"
    legacy_dir.mkdir()
    binary_name = _harper_binary_name()
    legacy_binary = legacy_dir / binary_name
    legacy_binary.write_bytes(b"legacy-binary")
    (legacy_dir / "harper-ls.version").write_text("2.6.0", encoding="utf-8")
    (legacy_dir / "harper-ls.release.json").write_text("{}", encoding="utf-8")

    with patch("plugin.writer.locale.harper_binary.shutil.which", return_value=None):
        path = harper_binary_module._get_harper_binary(str(tmp_path))

    harper_dir = tmp_path / "harper"
    assert path == str(harper_dir / binary_name)
    assert (harper_dir / binary_name).read_bytes() == b"legacy-binary"
    assert (harper_dir / "harper-ls.version").read_text(encoding="utf-8") == "2.6.0"
    assert (harper_dir / "harper-ls.release.json").is_file()
    assert not legacy_binary.exists()
    assert not legacy_dir.exists()
    mock_download.assert_not_called()


@patch("plugin.writer.locale.harper_binary.retrieve")
def test_download_harper_binary_installs_binary(mock_retrieve: MagicMock, tmp_path: Path) -> None:
    release = HarperReleaseAsset(
        version="2.6.0",
        asset_name="harper-ls-x86_64-unknown-linux-gnu.tar.gz",
        download_url="https://example.com/harper.tar.gz",
        sha256="abc123",
    )
    harper_dir = tmp_path / "harper"
    dest = harper_dir / "harper-ls"
    dest.parent.mkdir(parents=True, exist_ok=True)
    captured: dict[str, Path] = {}

    def fake_retrieve(*, path: str, fname: str, processor=None, **kwargs) -> str:
        del kwargs
        assert processor is None
        download_dir = Path(path)
        archive_path = download_dir / fname
        archive_path.write_bytes(b"fake-archive")
        extracted = download_dir / f"{fname}.untar" / "harper-ls"
        extracted.parent.mkdir(parents=True)
        extracted.write_bytes(b"fake-binary")
        captured["download_dir"] = download_dir
        captured["extracted"] = extracted
        return str(archive_path)

    mock_retrieve.side_effect = fake_retrieve

    def fake_processor(fname: str, action: str, pup) -> list[str]:
        del fname, action, pup
        return [str(captured["extracted"])]

    with patch("plugin.writer.locale.harper_binary.Untar", return_value=fake_processor):
        harper_binary_module._download_harper_binary(dest, release)

    mock_retrieve.assert_called_once()
    assert dest.read_bytes() == b"fake-binary"
    assert (dest.parent / "harper-ls.version").read_text(encoding="utf-8") == "2.6.0"
    # Temp extract tree must not persist under harper/
    assert not (harper_dir / f"{release.asset_name}.untar").exists()
    assert not captured["download_dir"].exists()


@patch("plugin.writer.locale.harper_binary.retrieve")
def test_download_harper_binary_removes_archive_after_success(mock_retrieve: MagicMock, tmp_path: Path) -> None:
    release = HarperReleaseAsset(
        version="2.6.0",
        asset_name="harper-ls-x86_64-unknown-linux-gnu.tar.gz",
        download_url="https://example.com/harper.tar.gz",
        sha256="abc123",
    )
    harper_dir = tmp_path / "harper"
    harper_dir.mkdir(parents=True)
    dest = harper_dir / "harper-ls"
    captured: dict[str, Path] = {}

    def fake_retrieve(*, path: str, fname: str, processor=None, **kwargs) -> str:
        del kwargs
        assert processor is None
        download_dir = Path(path)
        archive_path = download_dir / fname
        archive_path.write_bytes(b"fake-archive")
        extracted = download_dir / f"{fname}.untar" / "harper-ls"
        extracted.parent.mkdir(parents=True)
        extracted.write_bytes(b"fake-binary")
        captured["download_dir"] = download_dir
        captured["archive_path"] = archive_path
        captured["extracted"] = extracted
        return str(archive_path)

    mock_retrieve.side_effect = fake_retrieve

    def fake_processor(fname: str, action: str, pup) -> list[str]:
        del fname, action, pup
        return [str(captured["extracted"])]

    with patch("plugin.writer.locale.harper_binary.Untar", return_value=fake_processor):
        harper_binary_module._download_harper_binary(dest, release)

    assert dest.read_bytes() == b"fake-binary"
    assert (harper_dir / "harper-ls.version").read_text(encoding="utf-8") == "2.6.0"
    assert captured["download_dir"] != harper_dir
    assert not captured["archive_path"].exists()
    assert not (harper_dir / release.asset_name).exists()
    assert not (harper_dir / f"{release.asset_name}.untar").exists()
    assert not captured["download_dir"].exists()


@patch("plugin.writer.locale.harper_binary.retrieve")
def test_download_harper_binary_windows_zip_leaves_single_binary(mock_retrieve: MagicMock, tmp_path: Path) -> None:
    release = HarperReleaseAsset(
        version="2.6.0",
        asset_name="harper-ls-x86_64-pc-windows-msvc.zip",
        download_url="https://example.com/harper.zip",
        sha256="abc123",
    )
    harper_dir = tmp_path / "harper"
    # Dest name matches Windows install layout; avoid patching os.name (WindowsPath).
    dest = harper_dir / "harper-ls.exe"
    dest.parent.mkdir(parents=True, exist_ok=True)
    captured: dict[str, Path] = {}

    def fake_retrieve(*, path: str, fname: str, processor=None, **kwargs) -> str:
        del kwargs
        assert processor is None
        download_dir = Path(path)
        archive_path = download_dir / fname
        archive_path.write_bytes(b"fake-archive")
        extracted = download_dir / f"{fname}.unzip" / "harper-ls.exe"
        extracted.parent.mkdir(parents=True)
        extracted.write_bytes(b"fake-windows-binary")
        captured["download_dir"] = download_dir
        captured["extracted"] = extracted
        return str(archive_path)

    mock_retrieve.side_effect = fake_retrieve

    def fake_processor(fname: str, action: str, pup) -> list[str]:
        del fname, action, pup
        return [str(captured["extracted"])]

    with patch("plugin.writer.locale.harper_binary.Unzip", return_value=fake_processor):
        harper_binary_module._download_harper_binary(dest, release)

    assert dest.read_bytes() == b"fake-windows-binary"
    assert (harper_dir / "harper-ls.version").read_text(encoding="utf-8") == "2.6.0"
    assert not (harper_dir / f"{release.asset_name}.unzip").exists()
    assert not captured["download_dir"].exists()
    leftover_names = {p.name for p in harper_dir.iterdir()}
    assert leftover_names == {"harper-ls.exe", "harper-ls.version"}


@patch("plugin.writer.locale.harper_binary.retrieve")
def test_download_harper_binary_propagates_retrieve_failure(mock_retrieve: MagicMock, tmp_path: Path) -> None:
    release = HarperReleaseAsset(
        version="2.6.0",
        asset_name="harper-ls-x86_64-unknown-linux-gnu.tar.gz",
        download_url="https://example.com/harper.tar.gz",
        sha256="deadbeef",
    )
    mock_retrieve.side_effect = ValueError("SHA256 hash of downloaded file does not match")

    dest = tmp_path / "harper" / "harper-ls"
    harper_dir = dest.parent
    harper_dir.mkdir(parents=True, exist_ok=True)

    with pytest.raises(RuntimeError, match="Failed to auto-download Harper binary"):
        harper_binary_module._download_harper_binary(dest, release)

    assert not (harper_dir / release.asset_name).exists()


def test_fetch_latest_release_asset_uses_github_api(tmp_path: Path) -> None:
    harper_binary_module._release_cache.clear()
    api_payload = {
        "tag_name": "v2.7.0",
        "assets": [
            {
                "name": "harper-ls-x86_64-unknown-linux-gnu.tar.gz",
                "browser_download_url": "https://example.com/harper.tar.gz",
                "digest": "sha256:abc123",
            }
        ],
    }

    with patch("plugin.writer.locale.harper_binary._github_api_request", return_value=api_payload), \
         patch("plugin.writer.locale.harper_binary.platform.system", return_value="Linux"), \
         patch("plugin.writer.locale.harper_binary.platform.machine", return_value="x86_64"):
        release = _fetch_latest_release_asset("linux", "x86_64", tmp_path / "harper")

    assert release.version == "2.7.0"
    assert release.sha256 == "abc123"


@patch("plugin.writer.locale.harper_binary._download_harper_binary")
@patch("plugin.writer.locale.harper_binary._fetch_latest_release_asset")
def test_get_harper_binary_emits_heartbeat_progress(
    mock_fetch: MagicMock,
    mock_download: MagicMock,
    tmp_path: Path,
) -> None:
    mock_fetch.return_value = _sample_release("2.6.0")
    harper_dir = tmp_path / "harper"
    harper_dir.mkdir()
    binary_path = harper_dir / _harper_binary_name()
    binary_path.write_bytes(b"current")
    (harper_dir / "harper-ls.version").write_text("2.6.0", encoding="utf-8")
    messages: list[str] = []

    def heartbeat_fn(payload: dict[str, str]) -> None:
        messages.append(str(payload.get("message") or ""))

    with patch("plugin.writer.locale.harper_binary.shutil.which", return_value=None):
        harper_binary_module._get_harper_binary(str(tmp_path), heartbeat_fn=heartbeat_fn)

    assert "Resolving harper-ls binary…" in messages
    assert "Using installed harper-ls v2.6.0" in messages
    mock_download.assert_not_called()


@patch("plugin.writer.locale.harper._get_harper_binary")
@patch("subprocess.Popen")
def test_run_harper_lint_emits_heartbeat_progress(mock_popen: MagicMock, mock_get_bin: MagicMock) -> None:
    mock_get_bin.return_value = "/bin/harper-ls"
    messages: list[str] = []

    def heartbeat_fn(payload: dict[str, str]) -> None:
        messages.append(str(payload.get("message") or ""))

    mock_popen.return_value = _mock_harper_lsp_stream(
        [
            _make_lsp_chunk(json.dumps({"jsonrpc": "2.0", "id": 1, "result": {"capabilities": {}}}).encode("utf-8")),
            _make_lsp_chunk(
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "method": "textDocument/publishDiagnostics",
                        "params": {
                            "uri": "file:///tmp/writeragent_harper_lint_123.txt",
                            "version": 1,
                            "diagnostics": [],
                        },
                    }
                ).encode("utf-8")
            ),
        ]
    )

    with patch("time.time_ns", return_value=123):
        run_harper_lint("clean sentence.", "/tmp", heartbeat_fn=heartbeat_fn)

    assert "Linting…" in messages
    mock_get_bin.assert_called_once()
    assert mock_get_bin.call_args.kwargs.get("heartbeat_fn") is heartbeat_fn


@patch("plugin.writer.locale.harper_binary.log")
def test_fetch_latest_release_asset_logs_error_when_asset_missing(mock_log: MagicMock, tmp_path: Path) -> None:
    harper_binary_module._release_cache.clear()
    api_payload = {"tag_name": "v2.7.0", "assets": []}

    with patch("plugin.writer.locale.harper_binary._github_api_request", return_value=api_payload):
        with pytest.raises(RuntimeError, match="not found in latest release"):
            _fetch_latest_release_asset("linux", "x86_64", tmp_path / "harper")

    mock_log.error.assert_called()
    assert "not found" in mock_log.error.call_args[0][1]


@patch("plugin.writer.locale.harper_binary.log")
def test_fetch_latest_release_asset_logs_github_api_failure(mock_log: MagicMock, tmp_path: Path) -> None:
    harper_binary_module._release_cache.clear()

    with patch("plugin.writer.locale.harper_binary._github_api_request", side_effect=OSError("network down")):
        with pytest.raises(RuntimeError, match="Harper releases API request failed"):
            _fetch_latest_release_asset("linux", "x86_64", tmp_path / "harper")

    mock_log.exception.assert_called()
    assert "GitHub releases API request failed" in mock_log.exception.call_args[0][0]


@patch("plugin.writer.locale.harper_binary.retrieve")
@patch("plugin.writer.locale.harper_binary.log")
def test_download_harper_binary_logs_error_with_exc_info(mock_log: MagicMock, mock_retrieve: MagicMock, tmp_path: Path) -> None:
    release = HarperReleaseAsset(
        version="2.6.0",
        asset_name="harper-ls-x86_64-unknown-linux-gnu.tar.gz",
        download_url="https://example.com/harper.tar.gz",
        sha256="deadbeef",
    )
    mock_retrieve.side_effect = ValueError("hash mismatch")
    dest = tmp_path / "harper" / "harper-ls"
    dest.parent.mkdir(parents=True, exist_ok=True)

    with pytest.raises(RuntimeError, match="Failed to auto-download Harper binary"):
        harper_binary_module._download_harper_binary(dest, release)

    mock_log.exception.assert_called()
    assert "Failed to download and extract binary" in mock_log.exception.call_args[0][0]


@patch("plugin.writer.locale.harper._get_harper_binary")
@patch("plugin.writer.locale.harper.log")
def test_run_harper_lint_logs_binary_resolve_failure(mock_log: MagicMock, mock_get_bin: MagicMock) -> None:
    mock_get_bin.side_effect = RuntimeError("Failed to auto-download Harper binary: boom")

    with pytest.raises(RuntimeError, match="Failed to auto-download"):
        run_harper_lint("They is here.", "/tmp")

    mock_log.exception.assert_called()
    assert "Failed to resolve harper-ls binary" in mock_log.exception.call_args[0][0]


@patch("plugin.writer.locale.harper.log")
def test_harper_lsp_initialize_logs_exception_on_failure(mock_log: MagicMock) -> None:
    with patch("subprocess.Popen", side_effect=OSError("exec failed")):
        with pytest.raises(RuntimeError, match="Failed to start/initialize harper-ls"):
            HarperLSClient("/bin/harper-ls")

    mock_log.exception.assert_called()
    assert "Failed to start/initialize harper-ls" in mock_log.exception.call_args[0][0]


def test_pump_grammar_status_ui_posts_not_blocking_execute() -> None:
    ctx = MagicMock()
    with (
        patch("plugin.framework.queue_executor.post_to_main_thread") as mock_post,
        patch("plugin.framework.queue_executor.execute_on_main_thread") as mock_execute,
    ):
        _pump_grammar_status_ui(ctx)

    mock_post.assert_called_once()
    mock_execute.assert_not_called()


def test_pump_grammar_status_ui_swallows_post_errors() -> None:
    ctx = MagicMock()
    with patch("plugin.framework.queue_executor.post_to_main_thread", side_effect=RuntimeError("no AsyncCallback")):
        _pump_grammar_status_ui(ctx)  # must not raise


def test_run_harper_check_continues_when_pump_post_times_out() -> None:
    """Regression: status UI pump must not abort Harper when main-thread post fails."""
    ctx = MagicMock()

    def _fake_lint(text, config_dir, *, bcp47="en-US", heartbeat_fn=None):
        if heartbeat_fn is not None:
            heartbeat_fn({"message": "Downloading harper-ls…"})
        return {"errors": [{"n_error_start": 0, "n_error_length": 4}]}

    with (
        patch("plugin.writer.locale.grammar_obs.emit_harper_worker_status"),
        patch(
            "plugin.framework.queue_executor.post_to_main_thread",
            side_effect=TimeoutError("Main-thread execution of _pump timed out after 2.0s"),
        ),
        patch("plugin.writer.locale.harper.run_harper_lint", side_effect=_fake_lint) as mock_lint,
    ):
        result = run_harper_check(ctx, "They is here.", "/tmp/cfg", bcp47="en-US")

    assert result == {"errors": [{"n_error_start": 0, "n_error_length": 4}]}
    mock_lint.assert_called_once()


def test_run_harper_check_pumps_ui_after_start_and_heartbeat() -> None:
    ctx = MagicMock()
    pump_calls: list[object] = []

    def _record_pump(c: object) -> None:
        pump_calls.append(c)

    def _fake_lint(text, config_dir, *, bcp47="en-US", heartbeat_fn=None):
        if heartbeat_fn is not None:
            heartbeat_fn({"message": "Downloading harper-ls v2.7.0…"})
        return {"errors": []}

    with (
        patch("plugin.writer.locale.grammar_obs.emit_harper_worker_status") as mock_emit,
        patch("plugin.writer.locale.harper._pump_grammar_status_ui", side_effect=_record_pump),
        patch("plugin.writer.locale.harper.run_harper_lint", side_effect=_fake_lint) as mock_lint,
    ):
        result = run_harper_check(ctx, "They is here.", "/tmp/cfg", bcp47="en-US")

    assert result == {"errors": []}
    mock_lint.assert_called_once()
    assert mock_emit.call_args_list[0].args == ("They is here.", "Starting Harper…")
    assert mock_emit.call_args_list[1].args == ("They is here.", "Downloading harper-ls v2.7.0…")
    assert pump_calls == [ctx, ctx]


def test_run_harper_check_heartbeat_skips_empty_message() -> None:
    ctx = MagicMock()

    def _fake_lint(text, config_dir, *, bcp47="en-US", heartbeat_fn=None):
        if heartbeat_fn is not None:
            heartbeat_fn({"message": "   "})
        return {"errors": []}

    with (
        patch("plugin.writer.locale.grammar_obs.emit_harper_worker_status") as mock_emit,
        patch("plugin.writer.locale.harper._pump_grammar_status_ui") as mock_pump,
        patch("plugin.writer.locale.harper.run_harper_lint", side_effect=_fake_lint),
    ):
        run_harper_check(ctx, "Hi.", "/tmp/cfg")

    assert mock_emit.call_count == 1
    mock_emit.assert_called_once_with("Hi.", "Starting Harper…")
    # Start pump once; empty heartbeat must not emit or pump again
    assert mock_pump.call_count == 1


def test_run_harper_check_does_not_pass_ctx_to_lint() -> None:
    """Grammar-queue lint stays on the drain thread; PE2I wait is doProofreading only."""
    ctx = MagicMock()
    with (
        patch("plugin.writer.locale.grammar_obs.emit_harper_worker_status"),
        patch("plugin.writer.locale.harper._pump_grammar_status_ui"),
        patch("plugin.writer.locale.harper.run_harper_lint", return_value={"errors": []}) as mock_lint,
        patch("plugin.framework.uno_context.wait_while_pumping") as mock_wait,
    ):
        run_harper_check(ctx, "Hi.", "/tmp/cfg")
    assert "ctx" not in mock_lint.call_args.kwargs
    mock_wait.assert_not_called()


def test_normalize_spaces_1to1() -> None:
    from plugin.writer.locale.harper import normalize_spaces_1to1

    assert normalize_spaces_1to1("") == ""
    assert normalize_spaces_1to1("Hello world") == "Hello world"
    # NBSP, CJK ideographic space, typography thin space
    text_with_spaces = "Hello\xa0world\u3000test\u2009sentence\nNew\r\nline"
    normalized = normalize_spaces_1to1(text_with_spaces)
    assert normalized == "Hello world test sentence\nNew\r\nline"
    assert len(normalized) == len(text_with_spaces)


def test_run_harper_lint_normalizes_unicode_whitespace() -> None:
    """Ensure run_harper_lint passes 1:1 normalized text to client.lint but slices original text."""
    with (
        patch("plugin.writer.locale.harper._get_harper_binary", return_value="/bin/harper-ls"),
        patch("plugin.writer.locale.harper._get_or_create_client") as mock_get_client,
    ):
        mock_client = MagicMock()
        mock_client.lint.return_value = [
            {
                "diagnostic": {
                    "message": "Incorrect article",
                    "code": "AnA",
                    "range": {"start": {"line": 0, "character": 10}, "end": {"line": 0, "character": 12}},
                },
                "suggestions": ["a"],
            }
        ]
        mock_get_client.return_value = mock_client

        original_text = "Harper\xa0is\xa0an\xa0language\xa0checker."
        res = run_harper_lint(original_text, "/tmp/cfg", bcp47="en-US")

        # client.lint must receive normalized string with standard ASCII spaces
        mock_client.lint.assert_called_once_with("Harper is an language checker.", bcp47="en-US", heartbeat_fn=None)
        # Sliced wrong text must match exact substring from original text
        assert res["errors"][0]["wrong"] == "an"
        assert res["errors"][0]["n_error_start"] == 10
        assert res["errors"][0]["n_error_length"] == 2


@patch("plugin.framework.worker_pool.run_in_background")
def test_harper_try_lint_idle_kicks_one_ensure(mock_bg: MagicMock) -> None:
    assert harper_try_lint("Hello.", "/tmp") is None
    assert mock_bg.call_count == 1
    assert harper_module._HARPER_STATE is HarperRuntimeState.RESOLVING
    assert harper_try_lint("Hello again.", "/tmp") is None
    assert mock_bg.call_count == 1


@patch("plugin.framework.worker_pool.run_in_background")
def test_harper_try_lint_ready_returns_errors(mock_bg: MagicMock) -> None:
    mock_client = MagicMock()
    mock_client.is_alive.return_value = True
    mock_client.lint.return_value = [
        {
            "diagnostic": {
                "message": "Start with capital letter",
                "code": "SentenceCapitalization",
                "range": {"start": {"line": 0, "character": 0}, "end": {"line": 0, "character": 4}},
            },
            "suggestions": ["This"],
        }
    ]
    harper_module._HARPER_CLIENT_CACHE["/bin/harper-ls"] = mock_client
    harper_module._set_state(HarperRuntimeState.READY)

    res = harper_try_lint("this is text", "/tmp")
    assert res is not None
    assert res["errors"][0]["wrong"] == "this"
    assert res["errors"][0]["correct"] == "This"
    mock_bg.assert_not_called()


@patch("plugin.framework.worker_pool.run_in_background")
def test_harper_try_lint_dead_process_kicks_ensure(mock_bg: MagicMock, caplog: pytest.LogCaptureFixture) -> None:
    mock_client = MagicMock()
    mock_client.is_alive.return_value = False
    harper_module._HARPER_CLIENT_CACHE["/bin/harper-ls"] = mock_client
    harper_module._set_state(HarperRuntimeState.READY)

    caplog.set_level(logging.ERROR, logger="writeragent.grammar")
    assert harper_try_lint("Hello.", "/tmp") is None
    assert mock_bg.call_count == 1
    assert harper_module._HARPER_STATE is HarperRuntimeState.RESOLVING
    assert any("harper-ls process is dead" in r.message for r in caplog.records)


@patch("plugin.framework.worker_pool.run_in_background")
def test_harper_try_lint_failed_cooldown_skips_retry(mock_bg: MagicMock) -> None:
    import time

    harper_module._set_state(HarperRuntimeState.FAILED, failed_at=time.monotonic())
    assert harper_try_lint("Hello.", "/tmp") is None
    mock_bg.assert_not_called()


@patch("plugin.framework.worker_pool.run_in_background")
def test_harper_try_lint_failed_after_cooldown_retries(mock_bg: MagicMock) -> None:
    import time

    harper_module._set_state(HarperRuntimeState.FAILED, failed_at=time.monotonic() - harper_module._HARPER_FAIL_COOLDOWN_SEC - 1)
    assert harper_try_lint("Hello.", "/tmp") is None
    assert mock_bg.call_count == 1
    assert harper_module._HARPER_STATE is HarperRuntimeState.RESOLVING


@patch("plugin.framework.worker_pool.run_in_background")
def test_harper_try_lint_eof_returns_none(mock_bg: MagicMock) -> None:
    """EOF during lint is not a clean sentence, so the caller must not cache it."""
    client = _bare_harper_client()
    client.stdout_queue.put(None)
    harper_module._HARPER_CLIENT_CACHE[client.binary_path] = client
    harper_module._set_state(HarperRuntimeState.READY)
    with patch.object(client, "_write", lambda _payload: None):
        assert harper_try_lint("Hello.", "/tmp") is None
    assert mock_bg.call_count == 1
    assert harper_module._HARPER_STATE is HarperRuntimeState.RESOLVING


def test_ensure_replaces_dead_cached_client() -> None:
    """The ensure after cooldown must not raise on the same dead client."""
    dead = MagicMock()
    dead.is_alive.return_value = False
    dead.binary_path = "/bin/harper-ls"
    dead.user_config_dir = "/tmp/cfg"
    harper_module._HARPER_CLIENT_CACHE["/bin/harper-ls"] = dead
    harper_module._set_state(
        HarperRuntimeState.FAILED,
        failed_at=time.monotonic() - harper_module._HARPER_FAIL_COOLDOWN_SEC - 1,
    )
    fresh = MagicMock()
    fresh.is_alive.return_value = True
    held: list[bool] = []

    def _build(*_args: object, **_kwargs: object) -> MagicMock:
        held.append(harper_module._HARPER_LOCK.locked())
        return fresh

    with (
        patch("plugin.writer.locale.harper._get_harper_binary", return_value="/bin/harper-ls"),
        patch("plugin.writer.locale.harper.HarperLSClient", side_effect=_build),
        patch("plugin.writer.locale.harper._schedule_proofread_again") as mock_sched,
    ):
        harper_module._harper_ensure_ready_body("/tmp/cfg", "en-US")

    dead.close.assert_called_once()
    assert held == [False]
    assert harper_module._HARPER_CLIENT_CACHE["/bin/harper-ls"] is fresh
    assert harper_module._HARPER_STATE is HarperRuntimeState.READY
    mock_sched.assert_called_once()
    assert not harper_module._HARPER_LOCK.locked()


def test_ensure_missing_binary_still_fails_and_cools_down() -> None:
    with patch("plugin.writer.locale.harper._get_harper_binary", side_effect=OSError("harper-ls missing")):
        harper_module._harper_ensure_ready_body("/tmp/cfg", "en-US")
    assert harper_module._HARPER_STATE is HarperRuntimeState.FAILED
    assert harper_module._HARPER_CLIENT_CACHE == {}
    with patch("plugin.framework.worker_pool.run_in_background") as mock_bg:
        assert harper_try_lint("Hello.", "/tmp/cfg") is None
        mock_bg.assert_not_called()


def test_ensure_failed_restart_drops_dead_client() -> None:
    """A restart that cannot start must not leave the dead client cached."""
    dead = MagicMock()
    dead.is_alive.return_value = False
    dead.binary_path = "/bin/harper-ls"
    dead.user_config_dir = "/tmp/cfg"
    harper_module._HARPER_CLIENT_CACHE["/bin/harper-ls"] = dead

    with (
        patch("plugin.writer.locale.harper._get_harper_binary", return_value="/bin/harper-ls"),
        patch("plugin.writer.locale.harper.HarperLSClient", side_effect=OSError("binary missing")),
        patch("plugin.writer.locale.harper._schedule_proofread_again") as mock_sched,
    ):
        harper_module._harper_ensure_ready_body("/tmp/cfg", "en-US")
    dead.close.assert_called_once()
    assert harper_module._HARPER_CLIENT_CACHE == {}
    assert harper_module._HARPER_STATE is HarperRuntimeState.FAILED
    mock_sched.assert_not_called()
    assert not harper_module._HARPER_LOCK.locked()

    fresh = MagicMock()
    fresh.is_alive.return_value = True
    with (
        patch("plugin.writer.locale.harper._get_harper_binary", return_value="/bin/harper-ls"),
        patch("plugin.writer.locale.harper.HarperLSClient", return_value=fresh),
        patch("plugin.writer.locale.harper._schedule_proofread_again"),
    ):
        harper_module._harper_ensure_ready_body("/tmp/cfg", "en-US")
    assert dead.close.call_count == 1
    assert harper_module._HARPER_CLIENT_CACHE["/bin/harper-ls"] is fresh
    assert harper_module._HARPER_STATE is HarperRuntimeState.READY


@patch("plugin.framework.worker_pool.run_in_background")
def test_maybe_start_harper_async_libreharper_submits_job(mock_bg: MagicMock) -> None:
    with patch("plugin.framework.uno_context.is_libreharper", return_value=True):
        submitted = maybe_start_harper_async(user_config_dir="/tmp")
        assert submitted is True
        assert mock_bg.call_count == 1
        assert harper_module._HARPER_STATE is HarperRuntimeState.RESOLVING


@patch("plugin.framework.worker_pool.run_in_background")
def test_maybe_start_harper_async_writeragent_harper_submits_job(mock_bg: MagicMock) -> None:
    with patch("plugin.framework.uno_context.is_libreharper", return_value=False), \
         patch("plugin.framework.config.is_grammar_enabled", return_value=True), \
         patch("plugin.framework.config.get_grammar_provider", return_value="harper"):
        submitted = maybe_start_harper_async(user_config_dir="/tmp")
        assert submitted is True
        assert mock_bg.call_count == 1
        assert harper_module._HARPER_STATE is HarperRuntimeState.RESOLVING


@patch("plugin.framework.worker_pool.run_in_background")
def test_maybe_start_harper_async_writeragent_off_skips(mock_bg: MagicMock) -> None:
    with patch("plugin.framework.uno_context.is_libreharper", return_value=False), \
         patch("plugin.framework.config.is_grammar_enabled", return_value=False), \
         patch("plugin.framework.config.get_grammar_provider", return_value="off"):
        submitted = maybe_start_harper_async(user_config_dir="/tmp")
        assert submitted is False
        mock_bg.assert_not_called()
        assert harper_module._HARPER_STATE is HarperRuntimeState.IDLE


@patch("plugin.framework.worker_pool.run_in_background")
def test_maybe_start_harper_async_writeragent_llm_skips(mock_bg: MagicMock) -> None:
    with patch("plugin.framework.uno_context.is_libreharper", return_value=False), \
         patch("plugin.framework.config.is_grammar_enabled", return_value=True), \
         patch("plugin.framework.config.get_grammar_provider", return_value="llm"):
        submitted = maybe_start_harper_async(user_config_dir="/tmp")
        assert submitted is False
        mock_bg.assert_not_called()
        assert harper_module._HARPER_STATE is HarperRuntimeState.IDLE


@patch("plugin.framework.worker_pool.run_in_background")
def test_maybe_start_harper_async_idempotent_when_resolving(mock_bg: MagicMock) -> None:
    harper_module._set_state(HarperRuntimeState.RESOLVING)
    with patch("plugin.framework.uno_context.is_libreharper", return_value=True):
        submitted = maybe_start_harper_async(user_config_dir="/tmp")
        assert submitted is False
        mock_bg.assert_not_called()


@patch("plugin.framework.worker_pool.run_in_background")
def test_maybe_start_harper_async_skips_empty_config_dir(mock_bg: MagicMock) -> None:
    with patch("plugin.framework.uno_context.is_libreharper", return_value=True), \
         patch("plugin.framework.config.user_config_dir", return_value=""):
        submitted = maybe_start_harper_async(user_config_dir="")
        assert submitted is False
        mock_bg.assert_not_called()
        assert harper_module._HARPER_STATE is HarperRuntimeState.IDLE


@patch("plugin.framework.worker_pool.run_in_background")
def test_harper_ensure_ready_async_submit_failure_resets_idle(mock_bg: MagicMock) -> None:
    mock_bg.side_effect = RuntimeError("pool not ready")
    submitted = harper_module.harper_ensure_ready_async("/tmp")
    assert submitted is False
    assert harper_module._HARPER_STATE is HarperRuntimeState.IDLE


def test_harper_ensure_ready_body_schedules_proofread_again() -> None:
    mock_client = MagicMock()
    mock_client.is_alive.return_value = True
    with (
        patch("plugin.writer.locale.harper._get_harper_binary", return_value="/bin/harper-ls"),
        patch("plugin.writer.locale.harper._get_or_create_client", return_value=mock_client),
        patch("plugin.writer.locale.harper._schedule_proofread_again") as mock_sched,
    ):
        harper_module._harper_ensure_ready_body("/tmp", "en-US")
    mock_sched.assert_called_once()
    assert harper_module._HARPER_STATE is HarperRuntimeState.READY


def test_harper_runtime_is_ready_requires_alive_client() -> None:
    assert harper_runtime_is_ready() is False
    harper_module._set_state(HarperRuntimeState.READY)
    assert harper_runtime_is_ready() is False
    mock_client = MagicMock()
    mock_client.is_alive.return_value = True
    harper_module._HARPER_CLIENT_CACHE["/bin/harper-ls"] = mock_client
    assert harper_runtime_is_ready() is True
    mock_client.is_alive.return_value = False
    assert harper_runtime_is_ready() is False


def test_ensure_ready_empty_listeners_recovers_on_first_attach() -> None:
    """READY + PROOFREAD_AGAIN with no listeners must still re-walk once a listener hooks."""
    from plugin.writer.locale.ai_grammar_proofreader import WriterAgentAiGrammarProofreader

    ctx = MagicMock()
    with (
        patch("plugin.framework.logging.init_logging"),
        patch("plugin.writer.locale.grammar_persistence.grammar_registry.register_live_proofreader"),
    ):
        pr = WriterAgentAiGrammarProofreader(ctx)
    pr._provider = "harper"
    from plugin.writer.locale.grammar_persistence import grammar_registry

    grammar_registry.register_live_proofreader(pr)
    mock_client = MagicMock()
    mock_client.is_alive.return_value = True
    listener = MagicMock()
    try:
        with (
            patch("plugin.writer.locale.harper._get_harper_binary", return_value="/bin/harper-ls"),
            patch("plugin.writer.locale.harper._get_or_create_client", return_value=mock_client),
            patch("plugin.framework.queue_executor.post_to_main_thread", side_effect=lambda fn, *a, **k: fn(*a, **k)),
            patch("plugin.writer.locale.grammar_obs.emit_harper_worker_status"),
        ):
            harper_module._harper_ensure_ready_body("/tmp", "en-US")
            assert pr._pending_proofread_again is True
            listener.processLinguServiceEvent.assert_not_called()
            pr.addLinguServiceEventListener(listener)
        listener.processLinguServiceEvent.assert_called_once()
        assert listener.processLinguServiceEvent.call_args[0][0].nEvent == 8
    finally:
        grammar_registry.live_proofreaders.discard(pr)



@patch("plugin.framework.worker_pool.run_in_background")
def test_harper_try_lint_empty_config_dir_does_not_ensure(mock_bg: MagicMock, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.ERROR, logger="writeragent.grammar")
    assert harper_try_lint("Hello.", "") is None
    mock_bg.assert_not_called()
    assert harper_module._HARPER_STATE is HarperRuntimeState.IDLE
    assert any("empty user config dir" in r.message for r in caplog.records)


@patch("plugin.framework.worker_pool.run_in_background")
def test_harper_try_lint_logs_error_on_lint_exception(mock_bg: MagicMock, caplog: pytest.LogCaptureFixture) -> None:
    mock_client = MagicMock()
    mock_client.is_alive.return_value = True
    mock_client.lint.side_effect = RuntimeError("broken pipe")
    harper_module._HARPER_CLIENT_CACHE["/bin/harper-ls"] = mock_client
    harper_module._set_state(HarperRuntimeState.READY)

    caplog.set_level(logging.ERROR, logger="writeragent.grammar")
    assert harper_try_lint("He go to the store.", "/tmp") is None
    assert any("lint failed on ready client" in r.message for r in caplog.records)
    assert mock_bg.call_count == 1


def _ready_harper_client(lint_side_effect: object) -> MagicMock:
    mock_client = MagicMock()
    mock_client.is_alive.return_value = True
    mock_client.lint.side_effect = lint_side_effect
    harper_module._HARPER_CLIENT_CACHE["/bin/harper-ls"] = mock_client
    harper_module._set_state(HarperRuntimeState.READY)
    return mock_client


def test_harper_try_lint_pumps_events_while_lint_outstanding() -> None:
    """Linguistic wait loop must PE2I while a slow lint is still on the worker."""
    lint_started = threading.Event()
    release_lint = threading.Event()

    def _slow_lint(*_a: object, **_k: object) -> list:
        lint_started.set()
        release_lint.wait(timeout=2.0)
        return []

    _ready_harper_client(_slow_lint)
    pumps: list[bool] = []

    def _pe2i(_ctx: object, rounds: int = 1, force: bool = False) -> bool:
        pumps.append(force)
        if lint_started.is_set():
            release_lint.set()
        return True

    ctx = MagicMock()
    with patch("plugin.framework.uno_context.process_events_to_idle", side_effect=_pe2i):
        res = harper_try_lint("Hello.", "/tmp", ctx=ctx)

    assert res == {"errors": []}
    assert pumps
    assert all(force is False for force in pumps)


def test_harper_try_lint_linguistic_thread_posts_pe2i() -> None:
    """doProofreading is Dummy-*; in-loop PE2I there is a UNO thread violation.

    Seed a leftover marshal item and leave it queued. That queue is
    process-wide; pytest-xdist often arrives here with one already sitting.
    The linguistic wait must still post. Draining first would hide the miss
    (stub times out at 2s, ``posts["n"]`` stays 0, lint returns empty errors).
    """
    from plugin.framework.queue_executor import default_executor

    saved: list[object] = []
    while True:
        try:
            saved.append(default_executor._work_queue.get_nowait())
        except queue.Empty:
            break
    default_executor._work_queue.put(object())
    try:
        lint_started = threading.Event()
        release_lint = threading.Event()

        def _slow_lint(*_a: object, **_k: object) -> list:
            lint_started.set()
            release_lint.wait(timeout=2.0)
            return []

        _ready_harper_client(_slow_lint)
        pe2i_threads: list[str] = []
        posts = {"n": 0}
        box: dict[str, object] = {}

        def _pe2i(_ctx: object, rounds: int = 1, force: bool = False) -> bool:
            del rounds, force
            pe2i_threads.append(threading.current_thread().name)
            return True

        def _post(fn: object, *args: object, **kwargs: object) -> None:
            del fn, args, kwargs
            posts["n"] += 1
            if lint_started.is_set():
                release_lint.set()

        def _run() -> None:
            with (
                patch("plugin.framework.uno_context.process_events_to_idle", side_effect=_pe2i),
                patch("plugin.framework.queue_executor.post_to_main_thread", side_effect=_post),
            ):
                box["res"] = harper_try_lint("Hello.", "/tmp", ctx=MagicMock())

        worker = threading.Thread(target=_run, name="Dummy-21")
        worker.start()
        worker.join(timeout=3.0)
        assert not worker.is_alive()
        assert box.get("res") == {"errors": []}
        assert posts["n"] >= 1
        assert pe2i_threads == []
    finally:
        while True:
            try:
                default_executor._work_queue.get_nowait()
            except queue.Empty:
                break
        for item in saved:
            default_executor._work_queue.put(item)


def test_harper_try_lint_reenter_during_wait_logs_and_returns_none(caplog: pytest.LogCaptureFixture) -> None:
    """Nested try_lint while a wait is active fail-softs and logs harper_wait_reenter once."""
    lint_started = threading.Event()
    release_lint = threading.Event()
    nested: dict[str, object] = {}

    def _slow_lint(*_a: object, **_k: object) -> list:
        lint_started.set()
        release_lint.wait(timeout=2.0)
        return []

    def _pe2i(_ctx: object, rounds: int = 1, force: bool = False) -> bool:
        # One nest per wait (not every later PE2I tick) — matches field logging.
        if "res" not in nested:
            nested["res"] = harper_try_lint("Other sentence.", "/tmp", ctx=_ctx)
        if lint_started.is_set():
            release_lint.set()
        return True

    _ready_harper_client(_slow_lint)
    caplog.set_level(logging.DEBUG, logger="writeragent.grammar")
    ctx = MagicMock()
    with patch("plugin.framework.uno_context.process_events_to_idle", side_effect=_pe2i):
        res = harper_try_lint("Hello.", "/tmp", ctx=ctx)

    assert res == {"errors": []}
    assert nested.get("res") is None
    warn_recs = [r for r in caplog.records if r.levelno == logging.WARNING and "harper_wait_reenter" in r.message]
    assert len(warn_recs) == 1
    assert "wait_age_ms=" in warn_recs[0].message
    assert "provider=" in warn_recs[0].message
    # Bugfix: in stripped release bundles, scripts/strip_code.py strips out grammar_obs(...)
    # call sites from plugin code, so the grammar_obs debug record is not emitted.
    # We only assert on the debug record when running against unstripped source.
    if module_source_contains(harper_module, "grammar_obs("):
        assert any(
            r.levelno == logging.DEBUG and "harper_wait_reenter" in r.message
            for r in caplog.records
        )


def test_harper_close_does_not_block_on_stuck_stdin() -> None:
    """Fixture teardown must not hang if harper-ls stops reading stdin (Windows CI)."""
    import time

    client = HarperLSClient.__new__(HarperLSClient)
    client.uri = "file:///tmp/unused.txt"
    client._doc_opened = True
    mock_proc = MagicMock()
    mock_proc.poll.return_value = None
    mock_proc.stdin.write.side_effect = lambda *_a, **_k: time.sleep(30)
    mock_proc.stdout = MagicMock()
    client.proc = mock_proc

    started = time.monotonic()
    client.close()
    assert time.monotonic() - started < 2.0
    mock_proc.stdin.write.assert_not_called()
    mock_proc.stdin.close.assert_called()
    mock_proc.terminate.assert_called()
    assert client.proc is None


def test_shutdown_harper_runtime_closes_cached_clients() -> None:
    mock_client = MagicMock()
    harper_module._HARPER_CLIENT_CACHE["/bin/harper-ls"] = mock_client
    harper_module._set_state(HarperRuntimeState.READY)
    shutdown_harper_runtime()
    mock_client.close.assert_called_once()
    assert harper_module._HARPER_CLIENT_CACHE == {}
    assert harper_module._HARPER_STATE is HarperRuntimeState.IDLE


def test_warn_if_harper_result_slow_silent_at_and_below_threshold(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.WARNING, logger="writeragent.grammar")
    assert HARPER_SLOW_RESULT_MS == 500
    assert warn_if_harper_result_slow(500, text_len=40, error_count=2) is False
    assert warn_if_harper_result_slow(0, text_len=40, error_count=0) is False
    assert not any("slow result" in r.message for r in caplog.records)


def test_warn_if_harper_result_slow_warns_above_threshold(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.WARNING, logger="writeragent.grammar")
    assert warn_if_harper_result_slow(501, text_len=87, error_count=3, cache="miss") is True
    records = [r for r in caplog.records if "slow result" in r.message]
    assert len(records) == 1
    assert records[0].levelno == logging.WARNING
    assert "elapsed_ms=501" in records[0].message
    assert "text_len=87" in records[0].message
    assert "errors=3" in records[0].message
    assert "cache=miss" in records[0].message


def test_lint_with_client_warns_when_lint_exceeds_threshold(caplog: pytest.LogCaptureFixture) -> None:
    mock_client = MagicMock()
    mock_client.lint.return_value = []
    caplog.set_level(logging.WARNING, logger="writeragent.grammar")
    times = iter((10.0, 10.75))
    with patch("plugin.writer.locale.harper.time.monotonic", side_effect=lambda: next(times)):
        out = harper_module._lint_with_client(mock_client, "Hello world.", "en-US")
    assert out == {"errors": []}
    records = [r for r in caplog.records if "slow result" in r.message]
    assert len(records) == 1
    assert records[0].levelno == logging.WARNING
    assert "elapsed_ms=750" in records[0].message
    assert "text_len=12" in records[0].message
    assert "errors=0" in records[0].message
    assert "cache=miss" in records[0].message


def test_lint_with_client_silent_when_lint_is_fast(caplog: pytest.LogCaptureFixture) -> None:
    mock_client = MagicMock()
    mock_client.lint.return_value = []
    caplog.set_level(logging.WARNING, logger="writeragent.grammar")
    times = iter((10.0, 10.1))
    with patch("plugin.writer.locale.harper.time.monotonic", side_effect=lambda: next(times)):
        harper_module._lint_with_client(mock_client, "Hello world.", "en-US")
    assert not any("slow result" in r.message for r in caplog.records)


def test_reader_sentinel_stays_on_captured_queue() -> None:
    """A superseded stdout reader must not put EOF onto the replacement queue."""
    client = HarperLSClient.__new__(HarperLSClient)
    client.proc = MagicMock()
    client.proc.stdout = BytesIO(b"")
    captured: queue.Queue[dict | None] = queue.Queue()
    replacement: queue.Queue[dict | None] = queue.Queue()
    client.stdout_queue = replacement
    client._read_loop(captured)
    assert captured.get_nowait() is None
    assert replacement.empty()


def test_reader_crash_sentinel_is_not_a_clean_sentence() -> None:
    """A crashed reader still fences its sentinel, and that sentinel is not a publish."""
    client = _bare_harper_client()
    client.proc.stdout = BytesIO(b"")
    captured: queue.Queue[dict | None] = queue.Queue()
    replacement: queue.Queue[dict | None] = queue.Queue()
    client.stdout_queue = replacement
    with patch("plugin.writer.locale.harper.json_rpc_framing.read_frame", side_effect=OSError("reader crashed")):
        client._read_loop(captured)
    assert captured.qsize() == 1
    assert replacement.empty()
    client.stdout_queue = captured
    with pytest.raises(RuntimeError, match="closed before publishDiagnostics"):
        client._collect_diagnostics(1, time.monotonic() + 1)


def test_initialize_joins_old_reader_before_new_queue() -> None:
    """Queue swap happens after the previous reader is fenced."""
    client = HarperLSClient.__new__(HarperLSClient)
    client.binary_path = "/bin/harper-ls"
    client._heartbeat_fn = None
    client._doc_version = 3
    client._doc_opened = True
    client._lint_cancel = None
    old_queue: queue.Queue[dict | None] = queue.Queue()
    client.stdout_queue = old_queue
    released = threading.Event()
    order: list[str] = []

    def old_reader(out_queue: queue.Queue[dict | None]) -> None:
        released.wait(2.0)
        out_queue.put(None)

    old_thread = threading.Thread(target=old_reader, args=(old_queue,), daemon=True)
    old_thread.start()
    client.stdout_thread = old_thread

    dead = MagicMock()
    dead.poll.return_value = 0
    dead.stdin = None
    dead.stdout = BytesIO(b"")
    client.proc = dead

    def wrapped_join(thread: threading.Thread | None) -> None:
        order.append("join")
        released.set()
        HarperLSClient._join_stdout_thread(client, thread)

    def fake_read_loop(out_queue: queue.Queue[dict | None]) -> None:
        del out_queue
        order.append("new-reader")

    fake_proc = MagicMock()
    fake_proc.poll.return_value = None
    fake_proc.stdout = BytesIO(b"")
    fake_proc.stdin = BytesIO()
    try:
        with (
            patch.object(client, "_join_stdout_thread", wrapped_join),
            patch.object(client, "_read_loop", fake_read_loop),
            patch.object(client, "_send_request", lambda *_a, **_k: {"id": 1, "result": {}}),
            patch.object(client, "_write", lambda _payload: None),
            patch("plugin.writer.locale.harper.subprocess.Popen", return_value=fake_proc),
        ):
            client._initialize()
        if client.stdout_thread is not None:
            client.stdout_thread.join(timeout=1.0)
        assert order[0] == "join"
        assert order.index("join") < order.index("new-reader")
        assert client.stdout_queue is not old_queue
        assert old_queue.get_nowait() is None
    finally:
        old_thread.join(timeout=1.0)
        if client.stdout_thread is not None and client.stdout_thread.is_alive():
            client.stdout_thread.join(timeout=1.0)


def test_read_cancel_does_not_wait_for_lint_budget() -> None:
    import time

    client = HarperLSClient.__new__(HarperLSClient)
    client.proc = MagicMock()
    client.stdout_queue = queue.Queue()
    client._lint_cancel = threading.Event()

    def cancel_soon() -> None:
        time.sleep(0.05)
        assert client._lint_cancel is not None
        client._lint_cancel.set()

    threading.Thread(target=cancel_soon, daemon=True).start()
    started = time.monotonic()
    with pytest.raises(TimeoutError, match="cancelled"):
        client._read(time.monotonic() + 30.0)
    assert time.monotonic() - started < 2.0


def test_off_caller_timeout_cancels_worker_and_releases_lock() -> None:
    """After the caller budget, the worker must drop ``_HARPER_LOCK`` promptly."""
    import time

    started = threading.Event()
    client = MagicMock()

    def lint(
        text: str,
        bcp47: str = "en-US",
        *,
        heartbeat_fn: object = None,
        deadline: float | None = None,
        cancel_event: threading.Event | None = None,
    ) -> list[object]:
        del text, bcp47, heartbeat_fn, deadline
        started.set()
        limit = time.monotonic() + 5.0
        while cancel_event is None or not cancel_event.is_set():
            if time.monotonic() > limit:
                raise AssertionError("worker was not cancelled")
            time.sleep(0.02)
        raise TimeoutError("Harper LSP operation cancelled")

    client.lint.side_effect = lint

    def fake_wait(done: threading.Event, ctx: object, *, timeout: float) -> bool:
        del done, ctx, timeout
        assert started.wait(2.0)
        return False

    t0 = time.monotonic()
    with patch("plugin.framework.uno_context.wait_while_pumping", fake_wait):
        with pytest.raises(TimeoutError, match="cancelled"):
            harper_module._run_lint_off_caller_thread(client, "Hi.", "en-US", ctx=object(), restart=False)
    assert time.monotonic() - t0 < 3.0
    assert harper_module._HARPER_LOCK.acquire(timeout=0.5)
    harper_module._HARPER_LOCK.release()


def test_lint_restart_drops_lock_during_client_construction() -> None:
    """Popen + LSP initialize must not run while ``_HARPER_LOCK`` is held."""
    held_during: list[bool] = []

    class FakeClient:
        def __init__(
            self,
            binary_path: str,
            user_config_dir: str = "",
            bcp47: str = "en-US",
            *,
            heartbeat_fn: object = None,
        ) -> None:
            del bcp47, heartbeat_fn
            held_during.append(harper_module._HARPER_LOCK.locked())
            self.binary_path = binary_path
            self.user_config_dir = user_config_dir
            self.lint = MagicMock(return_value=[])
            self.close = MagicMock()
            self.is_alive = MagicMock(return_value=True)

    dead = MagicMock()
    dead.binary_path = "/bin/harper-ls"
    dead.user_config_dir = "/tmp"
    dead.lint.side_effect = RuntimeError("lsp dead")
    harper_module._HARPER_CLIENT_CACHE[dead.binary_path] = dead
    with patch.object(harper_module, "HarperLSClient", FakeClient):
        with harper_module._HARPER_LOCK:
            out = harper_module._lint_with_client(dead, "Hi.", "en-US", restart=True)
    assert out == {"errors": []}
    assert held_during == [False]
    assert isinstance(harper_module._HARPER_CLIENT_CACHE[dead.binary_path], FakeClient)

def test_lsp_range_to_offset_unicode_line_separator() -> None:
    text = "x\na\u2028b teh"
    assert lsp_range_to_offset(text, 1, 4) == 6


def test_run_lint_off_caller_thread_closes_client_on_cancel_timeout() -> None:
    """When a cancelled lint wait times out joining the worker, client.close() is called."""
    client = MagicMock()
    unblock = threading.Event()

    def blocked_lint(*args: object, **kwargs: object) -> list[object]:
        del args, kwargs
        unblock.wait(5.0)
        return []

    client.lint.side_effect = blocked_lint

    def fake_wait(done: threading.Event, ctx: object, *, timeout: float) -> bool:
        del done, ctx, timeout
        return False

    with (
        patch("plugin.framework.uno_context.wait_while_pumping", fake_wait),
        patch("plugin.writer.locale.harper._HARPER_CANCEL_JOIN_SEC", 0.05),
    ):
        with pytest.raises(TimeoutError, match="Harper LSP operation timed out"):
            harper_module._run_lint_off_caller_thread(client, "Hi.", "en-US", ctx=object(), restart=False)

    unblock.set()
    client.close.assert_called_once()


def test_lint_with_client_maps_diagnostics_against_normalized_text() -> None:
    """Diagnostics returned by Harper must be mapped against normalized lint_text."""
    client = MagicMock()
    raw_text = "foo\u00a0bar teh"
    normalized_text = "foo bar teh"
    assert harper_module.normalize_spaces_1to1(raw_text) == normalized_text

    mock_diagnostic = {
        "diagnostic": {
            "range": {"start": {"line": 0, "character": 8}, "end": {"line": 0, "character": 11}},
            "message": "Did you mean 'the'?",
            "code": "Typo",
        },
        "suggestions": ["the"],
    }
    client.lint.return_value = [mock_diagnostic]

    with patch("plugin.writer.locale.harper._diagnostics_to_errors", wraps=harper_module._diagnostics_to_errors) as mock_diag:
        with harper_module._HARPER_LOCK:
            out = harper_module._lint_with_client(client, raw_text, "en-US", restart=False)

        mock_diag.assert_called_once_with(normalized_text, [mock_diagnostic])

    assert len(out["errors"]) == 1
    assert out["errors"][0]["wrong"] == "teh"
    assert out["errors"][0]["n_error_start"] == 8
    assert out["errors"][0]["n_error_length"] == 3


def test_ensure_failure_while_resolving_sets_failed_and_cooldown_allows_restart() -> None:
    """An ensure failure while RESOLVING ends in FAILED with failed_at set, allowing restart after cooldown."""
    harper_module._set_state(HarperRuntimeState.RESOLVING)
    assert harper_module._HARPER_STATE is HarperRuntimeState.RESOLVING

    with patch("plugin.writer.locale.harper._get_harper_binary", side_effect=RuntimeError("binary not found")):
        harper_module._harper_ensure_ready_body("/tmp", "en-US")

    assert harper_module._HARPER_STATE is HarperRuntimeState.FAILED
    assert harper_module._HARPER_FAILED_AT > 0.0

    # During cooldown, ensure cannot start
    with harper_module._HARPER_LOCK:
        assert not harper_module._can_start_ensure_locked()

    # After cooldown expires, ensure can start
    harper_module._HARPER_FAILED_AT -= (harper_module._HARPER_FAIL_COOLDOWN_SEC + 1.0)
    with harper_module._HARPER_LOCK:
        assert harper_module._can_start_ensure_locked()
