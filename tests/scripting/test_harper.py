# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Unit tests for the Harper Rust linter helper."""

from io import BytesIO
from pathlib import Path
from unittest.mock import MagicMock, patch
import json
import queue
import pytest

from plugin.scripting.venv.harper import (
    HarperLSClient,
    HarperReleaseAsset,
    _fetch_latest_release_asset,
    _harper_lsp_settings,
    _read_installed_version,
    _read_exactly,
    lsp_range_to_offset,
    run_harper_check,
)
import plugin.scripting.venv.harper as harper_module


@pytest.fixture(autouse=True)
def _reset_harper_client_cache() -> None:
    """Each run_harper_check test owns a fresh LSP client and mocked stdout stream."""
    for client in harper_module._HARPER_CLIENT_CACHE.values():
        client.close()
    harper_module._HARPER_CLIENT_CACHE.clear()
    harper_module._release_cache.clear()


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
    assert _harper_lsp_settings("en-US", "/tmp")["harper-ls"]["dialect"] == "American"
    assert _harper_lsp_settings("en-GB", "/tmp")["harper-ls"]["userDictPath"] == str(Path("/tmp") / "harper-dictionary.txt")

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

    with patch.object(client.stdout_queue, "get", side_effect=queue.Empty):
        with pytest.raises(TimeoutError):
            client.lint("test text")


@patch("plugin.scripting.venv.harper._get_harper_binary")
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
        res = run_harper_check("clean sentence.", "/tmp")

    assert res == {"errors": []}


@patch("plugin.scripting.venv.harper._get_harper_binary")
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
        res = run_harper_check("hello world", "/tmp")

    assert len(res["errors"]) == 1
    err = res["errors"][0]
    assert err["wrong"] == ""
    assert err["n_error_start"] == 5
    assert err["n_error_length"] == 0


@patch("plugin.scripting.venv.harper._get_harper_binary")
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
        run_harper_check("colour is fine.", "/tmp", bcp47="en-GB")

    written = b"".join(call.args[0] for call in mock_proc.stdin.write.call_args_list if call.args)
    assert b'"dialect": "British"' in written


@patch("plugin.scripting.venv.harper._get_harper_binary")
def test_harper_run_harper_check_retries_after_failure(mock_get_bin: MagicMock) -> None:
    mock_get_bin.return_value = "/bin/harper-ls"
    broken_client = MagicMock()
    broken_client.lint.side_effect = TimeoutError("Harper LSP operation timed out")
    fresh_client = MagicMock()
    fresh_client.lint.return_value = []

    with patch("plugin.scripting.venv.harper._get_or_create_client", return_value=broken_client), \
         patch("plugin.scripting.venv.harper.HarperLSClient", return_value=fresh_client) as mock_ctor:
        res = run_harper_check("retry me.", "/tmp")

    assert res == {"errors": []}
    broken_client.close.assert_called_once()
    mock_ctor.assert_called_once()
    fresh_client.lint.assert_called_once_with("retry me.", bcp47="en-US")


def test_read_exactly_handles_partial_reads() -> None:
    payload = b"abcdefghij"

    class PartialReader:
        def __init__(self) -> None:
            self._parts = iter([payload[:3], payload[3:7], payload[7:]])

        def read(self, n: int) -> bytes:
            return next(self._parts, b"")

    assert _read_exactly(PartialReader(), len(payload)) == payload


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


@patch("plugin.scripting.venv.harper._download_harper_binary")
@patch("plugin.scripting.venv.harper._fetch_latest_release_asset")
def test_get_harper_binary_redownloads_when_latest_changes(
    mock_fetch: MagicMock,
    mock_download: MagicMock,
    tmp_path: Path,
) -> None:
    mock_fetch.return_value = _sample_release("2.7.0")
    harper_dir = tmp_path / "harper"
    harper_dir.mkdir()
    binary_path = harper_dir / "harper-ls"
    binary_path.write_bytes(b"old")
    (harper_dir / "harper-ls.version").write_text("2.6.0", encoding="utf-8")

    with patch("plugin.scripting.venv.harper.shutil.which", return_value=None):
        path = harper_module._get_harper_binary(str(tmp_path))

    mock_download.assert_called_once_with(binary_path, mock_fetch.return_value)
    assert path == str(binary_path)


@patch("plugin.scripting.venv.harper._download_harper_binary")
@patch("plugin.scripting.venv.harper._fetch_latest_release_asset")
def test_get_harper_binary_skips_download_when_up_to_date(
    mock_fetch: MagicMock,
    mock_download: MagicMock,
    tmp_path: Path,
) -> None:
    mock_fetch.return_value = _sample_release("2.6.0")
    harper_dir = tmp_path / "harper"
    harper_dir.mkdir()
    binary_path = harper_dir / "harper-ls"
    binary_path.write_bytes(b"current")
    (harper_dir / "harper-ls.version").write_text("2.6.0", encoding="utf-8")

    with patch("plugin.scripting.venv.harper.shutil.which", return_value=None):
        path = harper_module._get_harper_binary(str(tmp_path))

    mock_download.assert_not_called()
    assert path == str(binary_path)


@patch("plugin.scripting.venv.harper._download_harper_binary")
@patch("plugin.scripting.venv.harper._fetch_latest_release_asset")
def test_migrate_legacy_bin_install_moves_binary(
    mock_fetch: MagicMock,
    mock_download: MagicMock,
    tmp_path: Path,
) -> None:
    mock_fetch.return_value = _sample_release("2.6.0")
    legacy_dir = tmp_path / "bin"
    legacy_dir.mkdir()
    legacy_binary = legacy_dir / "harper-ls"
    legacy_binary.write_bytes(b"legacy-binary")
    (legacy_dir / "harper-ls.version").write_text("2.6.0", encoding="utf-8")
    (legacy_dir / "harper-ls.release.json").write_text("{}", encoding="utf-8")

    with patch("plugin.scripting.venv.harper.shutil.which", return_value=None):
        path = harper_module._get_harper_binary(str(tmp_path))

    harper_dir = tmp_path / "harper"
    assert path == str(harper_dir / "harper-ls")
    assert (harper_dir / "harper-ls").read_bytes() == b"legacy-binary"
    assert (harper_dir / "harper-ls.version").read_text(encoding="utf-8") == "2.6.0"
    assert (harper_dir / "harper-ls.release.json").is_file()
    assert not legacy_binary.exists()
    assert not legacy_dir.exists()
    mock_download.assert_not_called()


@patch("plugin.scripting.venv.harper.retrieve")
def test_download_harper_binary_installs_binary(mock_retrieve: MagicMock, tmp_path: Path) -> None:
    release = HarperReleaseAsset(
        version="2.6.0",
        asset_name="harper-ls-x86_64-unknown-linux-gnu.tar.gz",
        download_url="https://example.com/harper.tar.gz",
        sha256="abc123",
    )
    harper_dir = tmp_path / "harper"
    extracted = harper_dir / "harper-ls-x86_64-unknown-linux-gnu.tar.gz.untar" / "harper-ls"
    extracted.parent.mkdir(parents=True)
    extracted.write_bytes(b"fake-binary")
    mock_retrieve.return_value = str(harper_dir / release.asset_name)
    mock_processor = MagicMock(return_value=[str(extracted)])

    dest = harper_dir / "harper-ls"
    dest.parent.mkdir(parents=True, exist_ok=True)
    with patch("plugin.scripting.venv.harper.Untar", return_value=mock_processor):
        harper_module._download_harper_binary(dest, release)

    mock_retrieve.assert_called_once()
    mock_processor.assert_called_once()
    assert dest.read_bytes() == b"fake-binary"
    assert (dest.parent / "harper-ls.version").read_text(encoding="utf-8") == "2.6.0"
    assert extracted.is_file()


@patch("plugin.scripting.venv.harper.retrieve")
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
    extracted = harper_dir / f"{release.asset_name}.untar" / "harper-ls"
    extracted.parent.mkdir(parents=True)
    extracted.write_bytes(b"fake-binary")
    captured: dict[str, Path] = {}

    def fake_retrieve(*, path: str, fname: str, processor=None, **kwargs) -> str:
        del kwargs
        assert processor is None
        download_dir = Path(path)
        archive_path = download_dir / fname
        archive_path.write_bytes(b"fake-archive")
        captured["download_dir"] = download_dir
        captured["archive_path"] = archive_path
        return str(archive_path)

    mock_retrieve.side_effect = fake_retrieve
    mock_processor = MagicMock(return_value=[str(extracted)])
    with patch("plugin.scripting.venv.harper.Untar", return_value=mock_processor):
        harper_module._download_harper_binary(dest, release)

    assert dest.read_bytes() == b"fake-binary"
    assert (harper_dir / "harper-ls.version").read_text(encoding="utf-8") == "2.6.0"
    assert captured["download_dir"] != harper_dir
    assert not captured["archive_path"].exists()
    assert not (harper_dir / release.asset_name).exists()
    assert extracted.is_file()


@patch("plugin.scripting.venv.harper.retrieve")
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
        harper_module._download_harper_binary(dest, release)

    assert not (harper_dir / release.asset_name).exists()


def test_fetch_latest_release_asset_uses_github_api(tmp_path: Path) -> None:
    harper_module._release_cache.clear()
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

    with patch("plugin.scripting.venv.harper._github_api_request", return_value=api_payload), \
         patch("plugin.scripting.venv.harper.platform.system", return_value="Linux"), \
         patch("plugin.scripting.venv.harper.platform.machine", return_value="x86_64"):
        release = _fetch_latest_release_asset("linux", "x86_64", tmp_path / "harper")

    assert release.version == "2.7.0"
    assert release.sha256 == "abc123"

