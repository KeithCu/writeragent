"""Tests for scripts/install_opengrep.py."""

from __future__ import annotations

import hashlib
import io
import zipfile
from pathlib import Path
from unittest import mock

import pytest

from scripts import install_opengrep


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


def test_install_opengrep_posix_verifies_checksum_and_sets_executable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    payload = b"#!/bin/sh\necho opengrep\n"
    digest = hashlib.sha256(payload).hexdigest()
    monkeypatch.setitem(install_opengrep._ASSETS, ("Linux", "x86_64"), ("opengrep_linux_x86", digest))

    def fake_download(url: str, dest: Path) -> str:
        assert url.endswith(f"/{install_opengrep.OPENGREP_VERSION}/opengrep_linux_x86")
        dest.write_bytes(payload)
        return hashlib.sha256(payload).hexdigest()

    monkeypatch.setattr(install_opengrep, "_download_with_sha256", fake_download)
    exe = install_opengrep.install_opengrep(system="Linux", machine="x86_64", home=tmp_path)
    assert exe == tmp_path / ".local" / "bin" / "opengrep"
    assert exe.read_bytes() == payload
    assert exe.stat().st_mode & 0o111


def test_install_opengrep_windows_extracts_zip_and_verifies_checksum(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("opengrep.exe", b"MZ-fake")
        zf.writestr("helper.dll", b"DLL-fake")
    archive_bytes = buf.getvalue()
    digest = hashlib.sha256(archive_bytes).hexdigest()
    monkeypatch.setitem(
        install_opengrep._ASSETS, ("Windows", "x86_64"), ("opengrep_windows_x86.zip", digest)
    )

    def fake_download(_url: str, dest: Path) -> str:
        dest.write_bytes(archive_bytes)
        return hashlib.sha256(archive_bytes).hexdigest()

    monkeypatch.setattr(install_opengrep, "_download_with_sha256", fake_download)
    exe = install_opengrep.install_opengrep(system="Windows", machine="AMD64", home=tmp_path)
    assert exe == tmp_path / ".opengrep" / "cli" / "latest" / "opengrep.exe"
    assert exe.read_bytes() == b"MZ-fake"
    assert (exe.parent / "helper.dll").read_bytes() == b"DLL-fake"


def test_install_opengrep_rejects_bad_checksum(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        install_opengrep,
        "_download_with_sha256",
        lambda _url, dest: dest.write_bytes(b"corrupt") and "0" * 64 or "0" * 64,
    )
    with pytest.raises(RuntimeError, match="SHA-256 mismatch"):
        install_opengrep.install_opengrep(system="Linux", machine="x86_64", home=tmp_path)


def test_install_opengrep_retries_transient_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    payload = b"ok"
    digest = hashlib.sha256(payload).hexdigest()
    monkeypatch.setitem(install_opengrep._ASSETS, ("Linux", "x86_64"), ("opengrep_linux_x86", digest))
    monkeypatch.setattr(install_opengrep.time, "sleep", lambda _s: None)

    calls = 0

    def flaky_download(_url: str, dest: Path) -> str:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise TimeoutError("timed out")
        dest.write_bytes(payload)
        return digest

    monkeypatch.setattr(install_opengrep, "_download_with_sha256", flaky_download)
    exe = install_opengrep.install_opengrep(system="Linux", machine="x86_64", home=tmp_path)
    assert calls == 2
    assert exe.is_file()


def test_github_output_writes_metadata_and_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    out_file = tmp_path / "gh_output"
    path_file = tmp_path / "gh_path"
    monkeypatch.setenv("GITHUB_OUTPUT", str(out_file))
    monkeypatch.setenv("GITHUB_PATH", str(path_file))
    with mock.patch("platform.system", return_value="Darwin"), mock.patch(
        "platform.machine", return_value="arm64"
    ):
        assert install_opengrep.main(["--github-output"]) == 0

    output_text = out_file.read_text(encoding="utf-8")
    assert f"version={install_opengrep.OPENGREP_VERSION}" in output_text
    assert "asset=opengrep_osx_arm64" in output_text
    assert "sha256=" in output_text
    assert path_file.read_text(encoding="utf-8").strip().endswith(".local/bin")
