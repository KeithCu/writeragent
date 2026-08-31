# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Unit tests for Harper LSP client, binary install, and grammar-queue host entry."""

from io import BytesIO
from pathlib import Path
from unittest.mock import MagicMock, patch
import json
import os
import queue
import pytest

from plugin.contrib.lsp.json_rpc_framing import read_exactly
from plugin.writer.locale.harper import (
    HarperLSClient,
    HarperRuntimeState,
    _harper_lsp_settings,
    _pump_grammar_status_ui,
    harper_try_lint,
    lsp_range_to_offset,
    maybe_start_harper_async,
    run_harper_check,
    run_harper_lint,
)
from plugin.writer.locale.harper_binary import (
    HarperReleaseAsset,
    _fetch_latest_release_asset,
    _read_installed_version,
)
import plugin.writer.locale.harper as harper_module
import plugin.writer.locale.harper_binary as harper_binary_module


@pytest.fixture(autouse=True)
def _reset_harper_client_cache() -> None:
    """Each run_harper_lint test owns a fresh LSP client and mocked stdout stream."""
    for client in harper_module._HARPER_CLIENT_CACHE.values():
        client.close()
    harper_module._HARPER_CLIENT_CACHE.clear()
    harper_module._set_state(HarperRuntimeState.IDLE, failed_at=0.0)
    harper_binary_module._release_cache.clear()


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
def test_harper_try_lint_dead_process_kicks_ensure(mock_bg: MagicMock) -> None:
    mock_client = MagicMock()
    mock_client.is_alive.return_value = False
    harper_module._HARPER_CLIENT_CACHE["/bin/harper-ls"] = mock_client
    harper_module._set_state(HarperRuntimeState.READY)

    assert harper_try_lint("Hello.", "/tmp") is None
    assert mock_bg.call_count == 1
    assert harper_module._HARPER_STATE is HarperRuntimeState.RESOLVING


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



@patch("plugin.framework.worker_pool.run_in_background")
def test_harper_try_lint_empty_config_dir_does_not_ensure(mock_bg: MagicMock) -> None:
    assert harper_try_lint("Hello.", "") is None
    mock_bg.assert_not_called()
    assert harper_module._HARPER_STATE is HarperRuntimeState.IDLE
