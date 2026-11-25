#!/usr/bin/env python3
"""Download and verify the pinned Opengrep CLI release for CI and local dev."""

from __future__ import annotations

import argparse
import hashlib
import http.client
import os
import platform
import shutil
import tempfile
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

OPENGREP_VERSION = "v2.0.0"

# Change version and digests together; digests come from the GitHub release assets.
_ASSETS: dict[tuple[str, str], tuple[str, str]] = {
    ("Linux", "x86_64"): (
        "opengrep_linux_x86",
        "37e93f4da5fd04a18c5c1febbdd12f7e98dfccd4d0c9cf2593bcb25eeb462da0",
    ),
    ("Linux", "arm64"): (
        "opengrep_linux_aarch64",
        "6257b65423a744b704af2ff9beeb8f222c2f6925236af386b56b031d4c06d6d8",
    ),
    ("Darwin", "arm64"): (
        "opengrep_osx_arm64",
        "b51b3d83f165a2e0934497b8f1b08abb1c3041ef20ef911c97e04cb39af1ad55",
    ),
    ("Darwin", "x86_64"): (
        "opengrep_osx_x86",
        "51d2f918d7aad0afc1426bb4fbf56e32fc70c3ffd5ddb78e32e7d61e51c029a2",
    ),
    ("Windows", "x86_64"): (
        "opengrep_windows_x86.zip",
        "899a17e2b15863367cfd15032c93cdf482fd4e7ec8719c69de46f4f38b227864",
    ),
    # Upstream ships only x86_64 for Windows; it runs under emulation on ARM64.
    ("Windows", "arm64"): (
        "opengrep_windows_x86.zip",
        "899a17e2b15863367cfd15032c93cdf482fd4e7ec8719c69de46f4f38b227864",
    ),
}

_ARCH_ALIASES = {
    "x86_64": "x86_64",
    "amd64": "x86_64",
    "aarch64": "arm64",
    "arm64": "arm64",
}

_MAX_ATTEMPTS = 6
_DOWNLOAD_TIMEOUT_S = 30
_CHUNK_SIZE = 1024 * 1024


def resolve_asset(
    system: str | None = None,
    machine: str | None = None,
) -> tuple[str, str]:
    """Return ``(asset_filename, sha256)`` for the host OS and architecture."""
    os_name = system or platform.system()
    raw_arch = (machine or platform.machine()).lower()
    arch = _ARCH_ALIASES.get(raw_arch, raw_arch)
    key = (os_name, arch)
    if key not in _ASSETS:
        raise RuntimeError(f"Unsupported platform for pinned Opengrep: {os_name} {raw_arch}")
    return _ASSETS[key]


def install_paths(
    *,
    system: str | None = None,
    home: Path | None = None,
) -> tuple[Path, Path]:
    """Return ``(bin_dir, cache_path)`` for the given OS and home directory."""
    os_name = system or platform.system()
    base = home if home is not None else Path.home()
    if os_name == "Windows":
        # Windows v2.0.0+ ships opengrep.exe alongside DLLs; keep the directory together.
        bin_dir = base / ".opengrep" / "cli" / "latest"
        return bin_dir, bin_dir
    bin_dir = base / ".local" / "bin"
    return bin_dir, bin_dir / "opengrep"


def _download_with_sha256(url: str, dest: Path) -> str:
    """Stream *url* into *dest* and return its lowercase hex SHA-256 digest."""
    hasher = hashlib.sha256()
    req = urllib.request.Request(url, headers={"User-Agent": "writeragent-ci"})
    with urllib.request.urlopen(req, timeout=_DOWNLOAD_TIMEOUT_S) as resp, dest.open("wb") as fh:
        while True:
            chunk = resp.read(_CHUNK_SIZE)
            if not chunk:
                break
            hasher.update(chunk)
            fh.write(chunk)
    return hasher.hexdigest()


def _extract_windows_zip(archive_path: Path, bin_dir: Path) -> None:
    """Safely extract *archive_path* into *bin_dir*."""
    with zipfile.ZipFile(archive_path) as zf:
        for info in zf.infolist():
            member = Path(info.filename)
            if member.is_absolute() or ".." in member.parts:
                raise RuntimeError(f"Refusing unsafe zip member path: {info.filename}")
        with tempfile.TemporaryDirectory(dir=bin_dir) as stage_raw:
            stage = Path(stage_raw)
            zf.extractall(stage)
            for item in stage.iterdir():
                target = bin_dir / item.name
                if target.is_dir():
                    shutil.rmtree(target)
                elif target.exists():
                    target.unlink()
                shutil.move(str(item), str(target))


def install_opengrep(
    *,
    system: str | None = None,
    machine: str | None = None,
    home: Path | None = None,
    version: str = OPENGREP_VERSION,
) -> Path:
    """Download, verify, and install Opengrep, returning the executable path."""
    os_name = system or platform.system()
    asset, expected_sha256 = resolve_asset(os_name, machine)
    bin_dir, _ = install_paths(system=os_name, home=home)
    bin_dir.mkdir(parents=True, exist_ok=True)
    url = f"https://github.com/opengrep/opengrep/releases/download/{version}/{asset}"

    last_err: Exception | None = None
    tmp_path: Path | None = None
    for attempt in range(1, _MAX_ATTEMPTS + 1):
        fd, raw_tmp = tempfile.mkstemp(dir=bin_dir, prefix=".opengrep-dl-")
        os.close(fd)
        tmp_path = Path(raw_tmp)
        try:
            actual_sha256 = _download_with_sha256(url, tmp_path)
            if actual_sha256 != expected_sha256:
                raise RuntimeError(
                    f"SHA-256 mismatch for {asset}: expected {expected_sha256}, got {actual_sha256}"
                )
            if asset.endswith(".zip"):
                _extract_windows_zip(tmp_path, bin_dir)
                tmp_path.unlink(missing_ok=True)
                exe_path = bin_dir / "opengrep.exe"
                if not exe_path.is_file():
                    raise RuntimeError(f"Expected {exe_path} after extracting {asset}")
                return exe_path
            tmp_path.chmod(0o755)
            exe_path = bin_dir / "opengrep"
            tmp_path.replace(exe_path)
            return exe_path
        except (
            urllib.error.URLError,
            urllib.error.HTTPError,
            http.client.HTTPException,
            TimeoutError,
            OSError,
        ) as exc:
            tmp_path.unlink(missing_ok=True)
            last_err = exc
            if attempt == _MAX_ATTEMPTS:
                break
            time.sleep(min(10 * attempt, 30))
        except Exception:
            tmp_path.unlink(missing_ok=True)
            raise

    raise RuntimeError(f"Failed to download {url} after {_MAX_ATTEMPTS} attempts: {last_err}")


def _emit_github_output() -> None:
    asset, sha256 = resolve_asset()
    bin_dir, cache_path = install_paths()
    lines = [
        f"version={OPENGREP_VERSION}",
        f"asset={asset}",
        f"sha256={sha256}",
        f"bin-dir={bin_dir}",
        f"cache-path={cache_path}",
    ]
    gh_output = os.environ.get("GITHUB_OUTPUT")
    if gh_output:
        with Path(gh_output).open("a", encoding="utf-8") as fh:
            fh.write("\n".join(lines) + "\n")
    else:
        print("\n".join(lines))

    gh_path = os.environ.get("GITHUB_PATH")
    if gh_path:
        with Path(gh_path).open("a", encoding="utf-8") as fh:
            fh.write(f"{bin_dir}\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Install pinned Opengrep CLI.")
    parser.add_argument(
        "--github-output",
        action="store_true",
        help="Write asset/sha256/path metadata to GITHUB_OUTPUT and add bin-dir to GITHUB_PATH.",
    )
    args = parser.parse_args(argv)
    if args.github_output:
        _emit_github_output()
        return 0
    exe_path = install_opengrep()
    print(f"Installed Opengrep {OPENGREP_VERSION} to {exe_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
