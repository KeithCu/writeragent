# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Harper-ls binary resolution, download, and install for the venv worker."""

from __future__ import annotations

import json
import logging
import os
import platform
import shutil
import tempfile
import time
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from plugin.contrib.pooch import HTTPDownloader, Untar, Unzip, retrieve
from plugin.framework.constants import USER_AGENT

log = logging.getLogger("writeragent.grammar")

_HARPER_RELEASES_API = "https://api.github.com/repos/Automattic/harper/releases/latest"
_DOWNLOAD_MAX_BYTES = 20 * 1024 * 1024
_DOWNLOAD_TIMEOUT_SEC = 120
_RELEASE_CHECK_INTERVAL_SEC = 7 * 24 * 3600
_RELEASE_CACHE_FILENAME = "harper-ls.release.json"
_release_cache: dict[str, tuple[float, HarperReleaseAsset]] = {}


def _emit_progress(heartbeat_fn: Callable[[dict[str, str]], None] | None, message: str) -> None:
    if heartbeat_fn is not None:
        heartbeat_fn({"message": message})


@dataclass(frozen=True)
class HarperReleaseAsset:
    version: str
    asset_name: str
    download_url: str
    sha256: str


def _resolve_harper_asset(system: str, machine: str) -> str:
    is_arm = "arm" in machine or "aarch64" in machine
    if system == "linux":
        return "harper-ls-aarch64-unknown-linux-gnu.tar.gz" if is_arm else "harper-ls-x86_64-unknown-linux-gnu.tar.gz"
    if system == "darwin":
        return "harper-ls-aarch64-apple-darwin.tar.gz" if is_arm else "harper-ls-x86_64-apple-darwin.tar.gz"
    if system == "windows":
        return "harper-ls-x86_64-pc-windows-msvc.zip"
    raise RuntimeError(f"Unsupported OS: {system}")


def _is_harper_member(name: str) -> bool:
    normalized = name.replace("\\", "/")
    return normalized.endswith("harper-ls.exe") or normalized.endswith("/harper-ls") or normalized == "harper-ls"


def _pick_harper_member(paths: list[str]) -> str:
    for path in paths:
        if _is_harper_member(path):
            return path
    raise RuntimeError("harper-ls file not found inside archive")


def _parse_github_digest(digest: str | None) -> str:
    if not digest or not digest.startswith("sha256:"):
        raise RuntimeError("Harper release asset is missing a sha256 digest")
    return digest.removeprefix("sha256:")


def _github_api_request(url: str) -> dict:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/vnd.github+json"})
    with urllib.request.urlopen(request, timeout=30) as response:
        body = response.read(1024 * 1024)
    payload = json.loads(body.decode("utf-8"))
    if not isinstance(payload, dict):
        raise RuntimeError("Unexpected Harper releases API response")
    return payload


def _harper_install_dir(user_config_dir: str) -> Path:
    return Path(user_config_dir) / "harper"


def _release_cache_path(harper_dir: Path) -> Path:
    return harper_dir / _RELEASE_CACHE_FILENAME


def _read_persisted_release(harper_dir: Path, asset_name: str) -> HarperReleaseAsset | None:
    path = _release_cache_path(harper_dir)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or data.get("asset_name") != asset_name:
            return None
        checked_at = float(data.get("checked_at", 0))
        if (time.time() - checked_at) >= _RELEASE_CHECK_INTERVAL_SEC:
            return None
        return HarperReleaseAsset(version=str(data["version"]), asset_name=asset_name, download_url=str(data["download_url"]), sha256=str(data["sha256"]))
    except Exception:
        return None


def _write_persisted_release(harper_dir: Path, release: HarperReleaseAsset) -> None:
    harper_dir.mkdir(parents=True, exist_ok=True)
    payload = {"checked_at": time.time(), "version": release.version, "asset_name": release.asset_name, "download_url": release.download_url, "sha256": release.sha256}
    _release_cache_path(harper_dir).write_text(json.dumps(payload), encoding="utf-8")


def _fetch_latest_release_asset(system: str, machine: str, harper_dir: Path, *, heartbeat_fn: Callable[[dict[str, str]], None] | None = None) -> HarperReleaseAsset:
    """Resolve the latest harper-ls asset for this platform (checked at most once per week)."""
    asset_name = _resolve_harper_asset(system, machine)

    persisted = _read_persisted_release(harper_dir, asset_name)
    if persisted is not None:
        return persisted

    cached = _release_cache.get(asset_name)
    if cached is not None and (time.time() - cached[0]) < _RELEASE_CHECK_INTERVAL_SEC:
        return cached[1]

    _emit_progress(heartbeat_fn, "Checking Harper release…")
    release = _github_api_request(_HARPER_RELEASES_API)
    tag_name = str(release.get("tag_name") or "").strip()
    if not tag_name:
        raise RuntimeError("Harper releases API returned no tag_name")
    version = tag_name.lstrip("vV")

    assets = release.get("assets")
    if not isinstance(assets, list):
        raise RuntimeError("Harper releases API returned no assets")

    for asset in assets:
        if not isinstance(asset, dict) or asset.get("name") != asset_name:
            continue
        download_url = str(asset.get("browser_download_url") or "").strip()
        if not download_url:
            raise RuntimeError(f"Harper asset {asset_name} has no download URL")
        info = HarperReleaseAsset(version=version, asset_name=asset_name, download_url=download_url, sha256=_parse_github_digest(asset.get("digest")))
        _write_persisted_release(harper_dir, info)
        _release_cache[asset_name] = (time.time(), info)
        return info

    raise RuntimeError(f"Harper asset {asset_name} not found in latest release {tag_name}")


def _read_installed_version(harper_dir: Path) -> str | None:
    sidecar = harper_dir / "harper-ls.version"
    if not sidecar.is_file():
        return None
    try:
        version = sidecar.read_text(encoding="utf-8").strip()
        return version or None
    except Exception:
        return None


def _download_harper_binary(dest_path: Path, release: HarperReleaseAsset, *, heartbeat_fn: Callable[[dict[str, str]], None] | None = None) -> None:
    """Download harper-ls via vendored Pooch retrieve (hash verify, retry, archive extract)."""
    log.info("[harper] Downloading v%s binary (%s) from %s", release.version, release.asset_name, release.download_url)
    _emit_progress(heartbeat_fn, f"Downloading harper-ls v{release.version}…")

    dest_path.parent.mkdir(parents=True, exist_ok=True)
    extract_suffix = ".untar" if release.asset_name.endswith((".tar.gz", ".tgz")) else ".unzip"
    extract_root = dest_path.parent / f"{release.asset_name}{extract_suffix}"
    processor = Untar(extract_dir=str(extract_root)) if release.asset_name.endswith((".tar.gz", ".tgz")) else Unzip(extract_dir=str(extract_root))
    downloader = HTTPDownloader(headers={"User-Agent": USER_AGENT}, timeout=_DOWNLOAD_TIMEOUT_SEC, max_bytes=_DOWNLOAD_MAX_BYTES)
    tmp_binary = dest_path.parent / f".harper-ls{'.exe' if os.name == 'nt' else ''}.download"

    try:
        with tempfile.TemporaryDirectory(prefix="writeragent-harper-") as tmp_dir:
            archive_path = Path(tmp_dir) / release.asset_name
            retrieve(
                url=release.download_url,
                known_hash=f"sha256:{release.sha256}",
                path=tmp_dir,
                fname=release.asset_name,
                processor=None,
                downloader=downloader,
                retry_if_failed=2,
            )
            _emit_progress(heartbeat_fn, "Extracting harper-ls…")
            extracted = processor(str(archive_path), "download", None)
            if not isinstance(extracted, list):
                raise RuntimeError("Harper archive extraction did not return file paths")
            source = Path(_pick_harper_member(extracted))
            shutil.copy2(source, tmp_binary)
            os.chmod(tmp_binary, 0o755)  # nosec B103  # nosemgrep: insecure-file-permissions
            os.replace(tmp_binary, dest_path)
            (dest_path.parent / "harper-ls.version").write_text(release.version, encoding="utf-8")
            log.info("[harper] Binary v%s installed at %s", release.version, dest_path)
            try:
                if archive_path.is_file():
                    archive_path.unlink()
                    log.info("[harper] Removed downloaded archive %s", archive_path)
            except Exception as cleanup_err:
                log.warning("[harper] Could not remove downloaded archive %s: %s", archive_path, cleanup_err)
    except Exception as e:
        log.error("[harper] Failed to download and extract binary: %s", e)
        raise RuntimeError(f"Failed to auto-download Harper binary: {e}")
    finally:
        try:
            if tmp_binary.exists():
                tmp_binary.unlink()
        except Exception:
            pass


# TEMP(2026-07): Remove after ~2026-09 — migrates Harper from profile bin/ to harper/.
def _migrate_legacy_bin_install(user_config_dir: str, harper_dir: Path) -> None:
    suffix = ".exe" if os.name == "nt" else ""
    binary_name = f"harper-ls{suffix}"
    if (harper_dir / binary_name).exists():
        return

    legacy_dir = Path(user_config_dir) / "bin"
    legacy_binary = legacy_dir / binary_name
    if not legacy_binary.is_file():
        return

    harper_dir.mkdir(parents=True, exist_ok=True)
    shutil.move(str(legacy_binary), str(harper_dir / binary_name))
    for sidecar_name in ("harper-ls.version", _RELEASE_CACHE_FILENAME):
        legacy_sidecar = legacy_dir / sidecar_name
        if legacy_sidecar.is_file():
            shutil.move(str(legacy_sidecar), str(harper_dir / sidecar_name))

    try:
        for entry in legacy_dir.iterdir():
            name = entry.name
            if name.startswith("harper-ls-") and (name.endswith((".tar.gz", ".zip", ".tgz")) or name.endswith((".untar", ".unzip"))):
                if entry.is_dir():
                    shutil.rmtree(entry, ignore_errors=True)
                else:
                    entry.unlink(missing_ok=True)
        tmp_download = legacy_dir / f".harper-ls{suffix}.download"
        if tmp_download.is_file():
            tmp_download.unlink()
        if legacy_dir.is_dir() and not any(legacy_dir.iterdir()):
            legacy_dir.rmdir()
    except Exception as cleanup_err:
        log.warning("[harper] Legacy bin/ cleanup incomplete: %s", cleanup_err)


def _get_harper_binary(user_config_dir: str, *, heartbeat_fn: Callable[[dict[str, str]], None] | None = None) -> str:
    """Resolve path to harper-ls binary, auto-downloading if missing or outdated."""
    _emit_progress(heartbeat_fn, "Resolving harper-ls binary…")
    sys_path = shutil.which("harper-ls")
    if sys_path:
        _emit_progress(heartbeat_fn, "Using system harper-ls")
        return sys_path

    harper_dir = _harper_install_dir(user_config_dir)
    # TEMP(2026-07): Remove after ~2026-09 — migrates Harper from profile bin/ to harper/.
    _migrate_legacy_bin_install(user_config_dir, harper_dir)
    suffix = ".exe" if os.name == "nt" else ""
    binary_path = harper_dir / f"harper-ls{suffix}"

    system = platform.system().lower()
    machine = platform.machine().lower()
    release = _fetch_latest_release_asset(system, machine, harper_dir, heartbeat_fn=heartbeat_fn)
    installed = _read_installed_version(harper_dir)

    if not binary_path.exists() or installed != release.version:
        try:
            _download_harper_binary(binary_path, release, heartbeat_fn=heartbeat_fn)
        except Exception as e:
            if binary_path.exists() and installed:
                log.warning("[harper] Update to v%s failed; continuing with installed v%s: %s", release.version, installed, e)
            else:
                raise
    elif installed:
        _emit_progress(heartbeat_fn, f"Using installed harper-ls v{installed}")

    return str(binary_path)
