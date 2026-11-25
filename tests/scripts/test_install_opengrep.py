"""Tests for scripts/install_opengrep.py and resolver parity."""

from __future__ import annotations

import hashlib
import http.server
import io
import threading
import urllib.error
import zipfile
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest

from scripts import install_opengrep, opengrep_path


@contextmanager
def _local_server(
    handler_fn: Callable[[http.server.BaseHTTPRequestHandler], None],
) -> Iterator[str]:
    class _Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            handler_fn(self)

        def log_message(self, format: str, *args: object) -> None:  # noqa: A002
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = server.server_address
        yield f"http://{host}:{port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.mark.parametrize(
    ("system", "machine", "expected_asset"),
    [
        ("Linux", "x86_64", "opengrep_linux_x86"),
        ("Linux", "aarch64", "opengrep_linux_aarch64"),
        ("Darwin", "arm64", "opengrep_osx_arm64"),
        ("Darwin", "x86_64", "opengrep_osx_x86"),
        ("Windows", "AMD64", "opengrep_windows_x86.zip"),
        ("Windows", "ARM64", "opengrep_windows_x86.zip"),
    ],
)
def test_resolve_asset_supported_platforms(
    system: str, machine: str, expected_asset: str
) -> None:
    asset, sha256 = install_opengrep.resolve_asset(system, machine)
    assert asset == expected_asset
    assert len(sha256) == 64


def test_install_opengrep_posix_and_resolver_agree(tmp_path: Path) -> None:
    payload = b"#!/bin/sh\necho opengrep\n"
    digest = hashlib.sha256(payload).hexdigest()
    assets = {("Linux", "x86_64"): ("opengrep_linux_x86", digest)}

    def handle(req: http.server.BaseHTTPRequestHandler) -> None:
        assert req.path == f"/{install_opengrep.OPENGREP_VERSION}/opengrep_linux_x86"
        req.send_response(200)
        req.send_header("Content-Length", str(len(payload)))
        req.end_headers()
        req.wfile.write(payload)

    with _local_server(handle) as base_url:
        exe = install_opengrep.install_opengrep(
            system="Linux",
            machine="x86_64",
            home=tmp_path,
            base_url=base_url,
            assets=assets,
        )

    assert exe == tmp_path / ".local" / "bin" / "opengrep"
    assert exe.read_bytes() == payload
    assert exe.stat().st_mode & 0o111

    # Resolver must find the installed binary even when ~/.local/bin is not on PATH.
    resolved = opengrep_path.resolve_opengrep(
        repo_root=tmp_path / "empty-repo",
        env={"HOME": str(tmp_path), "PATH": ""},
        platform_name="linux",
    )
    assert resolved == exe


def test_install_opengrep_windows_extracts_zip(tmp_path: Path) -> None:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("opengrep.exe", b"MZ-fake")
        zf.writestr("helper.dll", b"DLL-fake")
    archive_bytes = buf.getvalue()
    digest = hashlib.sha256(archive_bytes).hexdigest()
    assets = {("Windows", "x86_64"): ("opengrep_windows_x86.zip", digest)}

    def handle(req: http.server.BaseHTTPRequestHandler) -> None:
        req.send_response(200)
        req.send_header("Content-Length", str(len(archive_bytes)))
        req.end_headers()
        req.wfile.write(archive_bytes)

    with _local_server(handle) as base_url:
        exe = install_opengrep.install_opengrep(
            system="Windows",
            machine="AMD64",
            home=tmp_path,
            base_url=base_url,
            assets=assets,
        )

    assert exe == tmp_path / ".opengrep" / "cli" / "latest" / "opengrep.exe"
    assert exe.read_bytes() == b"MZ-fake"
    assert (exe.parent / "helper.dll").read_bytes() == b"DLL-fake"


def test_install_opengrep_rejects_bad_checksum_and_cleans_up(tmp_path: Path) -> None:
    payload = b"corrupt"
    assets = {("Linux", "x86_64"): ("opengrep_linux_x86", "0" * 64)}

    def handle(req: http.server.BaseHTTPRequestHandler) -> None:
        req.send_response(200)
        req.send_header("Content-Length", str(len(payload)))
        req.end_headers()
        req.wfile.write(payload)

    with _local_server(handle) as base_url, pytest.raises(RuntimeError, match="SHA-256 mismatch"):
        install_opengrep.install_opengrep(
            system="Linux",
            machine="x86_64",
            home=tmp_path,
            base_url=base_url,
            assets=assets,
        )

    bin_dir = tmp_path / ".local" / "bin"
    assert list(bin_dir.iterdir()) == []


def test_install_opengrep_retries_short_body(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    payload = b"complete-binary-payload"
    digest = hashlib.sha256(payload).hexdigest()
    assets = {("Linux", "x86_64"): ("opengrep_linux_x86", digest)}
    monkeypatch.setattr(install_opengrep.time, "sleep", lambda _s: None)

    attempts = 0

    def handle(req: http.server.BaseHTTPRequestHandler) -> None:
        nonlocal attempts
        attempts += 1
        req.send_response(200)
        req.send_header("Content-Length", str(len(payload)))
        req.end_headers()
        if attempts == 1:
            req.wfile.write(payload[:5])
            req.close_connection = True
            return
        req.wfile.write(payload)

    with _local_server(handle) as base_url:
        exe = install_opengrep.install_opengrep(
            system="Linux",
            machine="x86_64",
            home=tmp_path,
            base_url=base_url,
            assets=assets,
        )

    assert attempts == 2
    assert exe.read_bytes() == payload


def test_install_opengrep_retries_slow_stream(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    payload = b"0123456789"
    digest = hashlib.sha256(payload).hexdigest()
    assets = {("Linux", "x86_64"): ("opengrep_linux_x86", digest)}
    monkeypatch.setattr(install_opengrep.time, "sleep", lambda _s: None)

    # First attempt: clock jumps 25s on the first chunk read so 10 B / 25s < 1 MB/s.
    times = iter([0.0, 25.0, 25.0, 25.0])
    monkeypatch.setattr(install_opengrep.time, "monotonic", lambda: next(times))

    attempts = 0

    def handle(req: http.server.BaseHTTPRequestHandler) -> None:
        nonlocal attempts
        attempts += 1
        req.send_response(200)
        req.send_header("Content-Length", str(len(payload)))
        req.end_headers()
        req.wfile.write(payload)

    with _local_server(handle) as base_url:
        exe = install_opengrep.install_opengrep(
            system="Linux",
            machine="x86_64",
            home=tmp_path,
            base_url=base_url,
            assets=assets,
        )

    assert attempts == 2
    assert exe.read_bytes() == payload


def test_install_opengrep_fails_fast_on_404(tmp_path: Path) -> None:
    assets = {("Linux", "x86_64"): ("opengrep_linux_x86", "0" * 64)}
    attempts = 0

    def handle(req: http.server.BaseHTTPRequestHandler) -> None:
        nonlocal attempts
        attempts += 1
        req.send_response(404)
        req.end_headers()

    with _local_server(handle) as base_url, pytest.raises(urllib.error.HTTPError):
        install_opengrep.install_opengrep(
            system="Linux",
            machine="x86_64",
            home=tmp_path,
            base_url=base_url,
            assets=assets,
        )

    assert attempts == 1
