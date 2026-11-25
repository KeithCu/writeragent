#!/usr/bin/env python3
"""Download and verify the pinned Opengrep CLI release for CI and local dev."""

from __future__ import annotations

import hashlib
import http.client
import os
import platform
import shutil
import ssl
import sys
import tempfile
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from scripts.opengrep_path import install_dir  # noqa: E402

OPENGREP_VERSION = "v2.0.0"
_DEFAULT_BASE_URL = "https://github.com/opengrep/opengrep/releases/download"

# Change version and digests together; digests come from the GitHub release assets.
OPENGREP_ASSETS: dict[tuple[str, str], tuple[str, str]] = {
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
# CI's 5-min step timeout would kill a slow ~300 kB/s stream without a retry
# because urlopen(timeout=30) only bounds each socket read; local installs skip it.
_SPEED_LIMIT_BPS = 1_000_000
_SPEED_TIME_S = 20.0
_CHUNK_SIZE = 64 * 1024
_RETRYABLE_HTTP_CODES = frozenset({429, 500, 502, 503, 504})


def resolve_asset(
    system: str | None = None,
    machine: str | None = None,
    *,
    assets: dict[tuple[str, str], tuple[str, str]] | None = None,
) -> tuple[str, str]:
    """Return ``(asset_filename, sha256)`` for the host OS and architecture."""
    os_name = system or platform.system()
    raw_arch = (machine or platform.machine()).lower()
    arch = _ARCH_ALIASES.get(raw_arch, raw_arch)
    table = OPENGREP_ASSETS if assets is None else assets
    key = (os_name, arch)
    if key not in table:
        raise RuntimeError(f"Unsupported platform for pinned Opengrep: {os_name} {raw_arch}")
    return table[key]


def _download_with_sha256(
    url: str,
    dest: Path,
    *,
    speed_limit_bps: int = _SPEED_LIMIT_BPS,
    speed_time_s: float = _SPEED_TIME_S,
) -> str:
    """Stream *url* into *dest* and return its lowercase hex SHA-256 digest."""
    hasher = hashlib.sha256()
    req = urllib.request.Request(url, headers={"User-Agent": "writeragent-ci"})
    bytes_read = 0
    window_start = time.monotonic()
    window_bytes = 0
    with urllib.request.urlopen(req, timeout=_DOWNLOAD_TIMEOUT_S) as resp, dest.open("wb") as fh:
        raw_len = resp.headers.get("Content-Length")
        expected_len = int(raw_len) if raw_len is not None else None
        while True:
            chunk = resp.read1(_CHUNK_SIZE)
            if not chunk:
                break
            hasher.update(chunk)
            fh.write(chunk)
            bytes_read += len(chunk)
            window_bytes += len(chunk)
            elapsed = time.monotonic() - window_start
            if speed_limit_bps > 0 and elapsed >= speed_time_s:
                if window_bytes / elapsed < speed_limit_bps:
                    raise TimeoutError(
                        f"Download below {speed_limit_bps} B/s for {speed_time_s}s"
                    )
                window_start = time.monotonic()
                window_bytes = 0
    # HTTPResponse.read() returns b"" on a mid-stream disconnect instead of raising.
    if expected_len is not None and bytes_read != expected_len:
        raise OSError(f"Incomplete download: got {bytes_read} of {expected_len} bytes")
    return hasher.hexdigest()


def _extract_windows_zip(archive_path: Path, bin_dir: Path) -> None:
    """Extract *archive_path* into *bin_dir*, replacing each entry."""
    with zipfile.ZipFile(archive_path) as zf, tempfile.TemporaryDirectory(dir=bin_dir) as stage_raw:
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
    base_url: str = _DEFAULT_BASE_URL,
    assets: dict[tuple[str, str], tuple[str, str]] | None = None,
    speed_limit_bps: int = _SPEED_LIMIT_BPS,
    speed_time_s: float = _SPEED_TIME_S,
) -> Path:
    """Download, verify, and install Opengrep, returning the executable path.

    Downloads directly to a temp file instead of piping upstream ``install.sh``
    so retries restart cleanly without duplicating bytes and every binary is
    verified against its pinned SHA-256 before replacing the target.
    """
    os_name = system or platform.system()
    asset, expected_sha256 = resolve_asset(os_name, machine, assets=assets)
    base_home = home if home is not None else Path.home()
    bin_dir = install_dir(base_home, is_windows=(os_name == "Windows"))
    bin_dir.mkdir(parents=True, exist_ok=True)
    url = f"{base_url.rstrip('/')}/{version}/{asset}"

    last_err: Exception | None = None
    for attempt in range(1, _MAX_ATTEMPTS + 1):
        fd, raw_tmp = tempfile.mkstemp(dir=bin_dir, prefix=".opengrep-dl-")
        os.close(fd)
        tmp_path = Path(raw_tmp)
        try:
            actual_sha256 = _download_with_sha256(
                url,
                tmp_path,
                speed_limit_bps=speed_limit_bps,
                speed_time_s=speed_time_s,
            )
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
        except urllib.error.HTTPError as exc:
            tmp_path.unlink(missing_ok=True)
            if exc.code not in _RETRYABLE_HTTP_CODES:
                raise
            last_err = exc
        except urllib.error.URLError as exc:
            tmp_path.unlink(missing_ok=True)
            if isinstance(exc.reason, ssl.SSLCertVerificationError):
                raise
            last_err = exc
        except (http.client.HTTPException, OSError) as exc:
            tmp_path.unlink(missing_ok=True)
            last_err = exc
        except Exception:
            tmp_path.unlink(missing_ok=True)
            raise

        if attempt == _MAX_ATTEMPTS:
            break
        time.sleep(min(10 * attempt, 30))

    raise RuntimeError(f"Failed to download {url} after {_MAX_ATTEMPTS} attempts: {last_err}")


def main() -> int:
    gh_path = os.environ.get("GITHUB_PATH")
    # Apply the 1 MB/s floor only in CI; local developers on slower links have no 5-min cap.
    speed_limit_bps = _SPEED_LIMIT_BPS if gh_path else 0
    exe_path = install_opengrep(speed_limit_bps=speed_limit_bps)
    if gh_path:
        with Path(gh_path).open("a", encoding="utf-8") as fh:
            fh.write(f"{exe_path.parent}\n")
    print(f"Installed Opengrep {OPENGREP_VERSION} to {exe_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
