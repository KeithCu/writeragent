# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Trusted Harper Rust grammar linter helper executing inside the user's virtual environment."""

from __future__ import annotations

import json
import logging
import os
import platform
import queue
import shutil
import subprocess
import tempfile
import threading
import time
import urllib.request
from dataclasses import dataclass
from typing import BinaryIO, cast
from pathlib import Path

from plugin.contrib.lsp import json_rpc_framing
from plugin.contrib.lsp.position_codec import ClientPosition, PositionCodec
from plugin.contrib.pooch import HTTPDownloader, Untar, Unzip, retrieve
from plugin.framework.constants import USER_AGENT


log = logging.getLogger("writeragent.grammar")

_JSONRPC = "2.0"
_INIT_PARAMS = {"processId": os.getpid(), "rootUri": "file:///tmp", "capabilities": {"textDocument": {"publishDiagnostics": {"relatedInformation": False}, "codeAction": {"dynamicRegistration": False, "codeActionLiteralSupport": {"codeActionKind": {"valueSet": ["quickfix"]}}}}}}

_HARPER_RELEASES_API = "https://api.github.com/repos/Automattic/harper/releases/latest"
_DOWNLOAD_MAX_BYTES = 20 * 1024 * 1024
_DOWNLOAD_TIMEOUT_SEC = 120
_RELEASE_CHECK_INTERVAL_SEC = 7 * 24 * 3600
_RELEASE_CACHE_FILENAME = "harper-ls.release.json"
_LINT_BUDGET_SEC = 15.0
_INIT_BUDGET_SEC = 5.0

_LSP_POSITION_CODEC = PositionCodec("utf-16")
_release_cache: dict[str, tuple[float, "HarperReleaseAsset"]] = {}


@dataclass(frozen=True)
class HarperReleaseAsset:
    version: str
    asset_name: str
    download_url: str
    sha256: str


_BCP47_TO_DIALECT: dict[str, str] = {"en-GB": "British", "en-AU": "Australian", "en-CA": "Canadian"}


def _lsp_notification(method: str, params: dict | None) -> dict:
    return {"jsonrpc": _JSONRPC, "method": method, "params": params}


def _lsp_request(req_id: int, method: str, params: dict | None) -> dict:
    return {"jsonrpc": _JSONRPC, "id": req_id, "method": method, "params": params}


def _lsp_response(req_id: int, result) -> dict:
    return {"jsonrpc": _JSONRPC, "id": req_id, "result": result}


def _deadline_remaining(deadline: float) -> float:
    return max(0.0, deadline - time.monotonic())


# Re-export for unit tests that exercise partial stream reads.
_read_exactly = json_rpc_framing.read_exactly


def _harper_lsp_settings(bcp47: str, user_config_dir: str) -> dict:
    dialect = _BCP47_TO_DIALECT.get(bcp47, "American")
    settings: dict = {"dialect": dialect}
    if user_config_dir:
        settings["userDictPath"] = str(Path(user_config_dir) / "harper-dictionary.txt")
    return {"harper-ls": settings}


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


def _fetch_latest_release_asset(system: str, machine: str, harper_dir: Path) -> HarperReleaseAsset:
    """Resolve the latest harper-ls asset for this platform (checked at most once per week)."""
    asset_name = _resolve_harper_asset(system, machine)

    persisted = _read_persisted_release(harper_dir, asset_name)
    if persisted is not None:
        return persisted

    cached = _release_cache.get(asset_name)
    if cached is not None and (time.time() - cached[0]) < _RELEASE_CHECK_INTERVAL_SEC:
        return cached[1]

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


def _download_harper_binary(dest_path: Path, release: HarperReleaseAsset) -> None:
    """Download harper-ls via vendored Pooch retrieve (hash verify, retry, archive extract)."""
    log.info("[harper] Downloading v%s binary (%s) from %s", release.version, release.asset_name, release.download_url)

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


def _get_harper_binary(user_config_dir: str) -> str:
    """Resolve path to harper-ls binary, auto-downloading if missing or outdated."""
    sys_path = shutil.which("harper-ls")
    if sys_path:
        return sys_path

    harper_dir = _harper_install_dir(user_config_dir)
    # TEMP(2026-07): Remove after ~2026-09 — migrates Harper from profile bin/ to harper/.
    _migrate_legacy_bin_install(user_config_dir, harper_dir)
    suffix = ".exe" if os.name == "nt" else ""
    binary_path = harper_dir / f"harper-ls{suffix}"

    system = platform.system().lower()
    machine = platform.machine().lower()
    release = _fetch_latest_release_asset(system, machine, harper_dir)
    installed = _read_installed_version(harper_dir)

    if not binary_path.exists() or installed != release.version:
        try:
            _download_harper_binary(binary_path, release)
        except Exception as e:
            if binary_path.exists() and installed:
                log.warning("[harper] Update to v%s failed; continuing with installed v%s: %s", release.version, installed, e)
            else:
                raise

    return str(binary_path)


_HARPER_CLIENT_CACHE: dict[str, HarperLSClient] = {}


class HarperLSClient:
    def __init__(self, binary_path: str, user_config_dir: str = "", bcp47: str = "en-US"):
        self.binary_path = binary_path
        self.user_config_dir = user_config_dir
        self._bcp47 = bcp47
        self._lsp_settings = _harper_lsp_settings(bcp47, user_config_dir)
        self.proc: subprocess.Popen[bytes] | None = None
        self.request_id = 0
        self.uri = f"file:///tmp/writeragent_harper_lint_{time.time_ns()}.txt"
        self._doc_version = 0
        self._doc_opened = False
        self.stdout_queue: queue.Queue = queue.Queue()
        self.stdout_thread: threading.Thread | None = None
        self._lint_lock = threading.Lock()
        self._initialize()

    def _initialize(self) -> None:
        try:
            if self.proc is not None:
                self.close()
            self._doc_version = 0
            self._doc_opened = False
            self.stdout_queue = queue.Queue()
            self.proc = subprocess.Popen([self.binary_path, "--stdio"], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=0)
            self.stdout_thread = threading.Thread(target=self._read_loop, daemon=True)  # nosemgrep: raw-uno-thread-ban
            self.stdout_thread.start()

            init_params = dict(_INIT_PARAMS)
            init_params["processId"] = os.getpid()
            deadline = time.monotonic() + _INIT_BUDGET_SEC
            self._send_request("initialize", init_params, deadline=deadline)
            self._write(_lsp_notification("initialized", {}))
        except Exception as e:
            self.close()
            raise RuntimeError(f"Failed to start/initialize harper-ls: {e}")

    def _read_loop(self) -> None:
        try:
            while self.proc and self.proc.stdout:
                msg = json_rpc_framing.read_frame(cast(BinaryIO, self.proc.stdout))
                if msg is None:
                    break
                self.stdout_queue.put(msg)
        except Exception:
            log.exception("[harper] LSP reader failed")
        finally:
            self.stdout_queue.put(None)

    def is_alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def _write(self, payload: dict) -> None:
        if not self.proc or self.proc.stdin is None:
            raise RuntimeError("harper-ls process not running")
        json_rpc_framing.write_frame(cast(BinaryIO, self.proc.stdin), payload)

    def _read(self, deadline: float) -> dict | None:
        if not self.proc:
            raise RuntimeError("harper-ls process not running")
        remaining = _deadline_remaining(deadline)
        if remaining <= 0:
            raise TimeoutError("Harper LSP operation timed out")
        try:
            return self.stdout_queue.get(timeout=remaining)
        except queue.Empty:
            raise TimeoutError("Harper LSP operation timed out")

    def _reply_workspace_configuration(self, req_id: int) -> None:
        self._write(_lsp_response(req_id, [self._lsp_settings]))

    def _read_and_handle(self, deadline: float) -> dict | None:
        msg = self._read(deadline)
        if not msg:
            return None

        if "id" in msg and "method" in msg:
            method = msg["method"]
            if method == "workspace/configuration":
                self._reply_workspace_configuration(msg["id"])
            else:
                self._write(_lsp_response(msg["id"], None))
            return self._read_and_handle(deadline)

        return msg

    def _send_request(self, method: str, params: dict, *, deadline: float) -> dict | None:
        self.request_id += 1
        req_id = self.request_id
        self._write(_lsp_request(req_id, method, params))

        while _deadline_remaining(deadline) > 0:
            msg = self._read_and_handle(deadline)
            if not msg:
                break
            if msg.get("id") == req_id:
                return msg
        return None

    def _sync_document(self, text: str, version: int) -> None:
        if not self._doc_opened:
            self._write(_lsp_notification("textDocument/didOpen", {"textDocument": {"uri": self.uri, "languageId": "markdown", "version": version, "text": text}}))
            self._doc_opened = True
        else:
            self._write(_lsp_notification("textDocument/didChange", {"textDocument": {"uri": self.uri, "version": version}, "contentChanges": [{"text": text}]}))

    def _apply_bcp47(self, bcp47: str) -> None:
        if bcp47 == self._bcp47:
            return
        self._bcp47 = bcp47
        self._lsp_settings = _harper_lsp_settings(bcp47, self.user_config_dir)
        self._write(_lsp_notification("workspace/didChangeConfiguration", {"settings": self._lsp_settings}))

    def _collect_diagnostics(self, version: int, deadline: float) -> list:
        while _deadline_remaining(deadline) > 0:
            msg = self._read_and_handle(deadline)
            if not msg:
                break

            if msg.get("method") == "textDocument/publishDiagnostics":
                params = msg.get("params", {})
                if params.get("uri") == self.uri:
                    msg_version = params.get("version")
                    if msg_version is not None and msg_version < version:
                        continue
                    return params.get("diagnostics", [])
        return []

    def _suggestions_for_diagnostic(self, diag: dict, deadline: float) -> list:
        suggestions: list[str] = []
        try:
            res = self._send_request("textDocument/codeAction", {"textDocument": {"uri": self.uri}, "range": diag["range"], "context": {"diagnostics": [diag]}}, deadline=deadline)
            if res and isinstance(res.get("result"), list):
                for action in res["result"]:
                    if action.get("kind") == "quickfix":
                        edit = action.get("edit", {})
                        changes = edit.get("changes", {})
                        for change_list in changes.values():
                            for chg in change_list:
                                new_text = chg.get("newText")
                                if new_text is not None and new_text not in suggestions:
                                    suggestions.append(new_text)
        except Exception as e:
            log.error("[harper] Failed to fetch codeActions: %s", e)
        return suggestions

    def lint(self, text: str, bcp47: str = "en-US") -> list:
        with self._lint_lock:
            if not self.is_alive():
                self._initialize()

            self._apply_bcp47(bcp47)
            self._doc_version += 1
            version = self._doc_version
            deadline = time.monotonic() + _LINT_BUDGET_SEC

            try:
                self._sync_document(text, version)
                diagnostics = self._collect_diagnostics(version, deadline)
                return [{"diagnostic": diag, "suggestions": self._suggestions_for_diagnostic(diag, deadline)} for diag in diagnostics]
            except Exception as e:
                log.error("[harper] Exception during linting, closing client: %s", e)
                self.close()
                raise

    def close(self) -> None:
        if self.proc:
            if self._doc_opened:
                try:
                    self._write(_lsp_notification("textDocument/didClose", {"textDocument": {"uri": self.uri}}))
                except Exception:
                    pass
                self._doc_opened = False
            try:
                self._write(_lsp_request(self.request_id + 1, "shutdown", None))
                self._write(_lsp_notification("exit", None))
                self.proc.wait(timeout=0.2)
            except Exception:
                try:
                    self.proc.terminate()
                    self.proc.wait(timeout=0.2)
                except Exception:
                    try:
                        self.proc.kill()
                    except Exception:
                        pass
            self.proc = None


def lsp_range_to_offset(text: str, line: int, character: int) -> int:
    """Convert LSP 0-indexed line/character (UTF-16 code units) to a Python string offset."""
    lines = [text] if ("\n" not in text and "\r" not in text) else text.splitlines(keepends=True)
    if line >= len(lines):
        return len(text)
    pos = _LSP_POSITION_CODEC.position_from_client_units(lines, ClientPosition(line=line, character=character))
    offset = sum(len(lines[i]) for i in range(pos.line))
    return min(offset + pos.character, len(text))


def _get_or_create_client(harper_bin: str, user_config_dir: str, bcp47: str) -> HarperLSClient:
    client = _HARPER_CLIENT_CACHE.get(harper_bin)
    if client is None:
        client = HarperLSClient(harper_bin, user_config_dir=user_config_dir, bcp47=bcp47)
        _HARPER_CLIENT_CACHE[harper_bin] = client
    return client


def run_harper_check(text: str, user_config_dir: str, bcp47: str = "en-US") -> dict:
    """Run harper-ls on text segment and return parsed errors."""
    try:
        harper_bin = _get_harper_binary(user_config_dir)
    except Exception as e:
        raise RuntimeError(str(e))

    client = _get_or_create_client(harper_bin, user_config_dir, bcp47)
    try:
        results = client.lint(text, bcp47=bcp47)
    except Exception as e:
        log.error("[harper] Linting error or connection lost, restarting client: %s", e)
        client.close()
        _HARPER_CLIENT_CACHE[harper_bin] = HarperLSClient(harper_bin, user_config_dir=user_config_dir, bcp47=bcp47)
        results = _HARPER_CLIENT_CACHE[harper_bin].lint(text, bcp47=bcp47)

    errors = []
    for item in results:
        diag = item["diagnostic"]
        suggestions = item["suggestions"]

        msg = diag.get("message", "")
        code = diag.get("code", "Grammar")

        diag_range = diag.get("range", {})
        start_pos = diag_range.get("start", {})
        end_pos = diag_range.get("end", {})

        start_offset = lsp_range_to_offset(text, start_pos.get("line", 0), start_pos.get("character", 0))
        end_offset = lsp_range_to_offset(text, end_pos.get("line", 0), end_pos.get("character", 0))
        length = max(0, end_offset - start_offset)

        errors.append(
            {
                "wrong": text[start_offset:end_offset] if length else "",
                "correct": suggestions[0] if suggestions else "",
                "n_error_start": start_offset,
                "n_error_length": length,
                "short_comment": msg,
                "full_comment": msg,
                "rule_identifier": f"harper||{code}",
                "suggestions": suggestions[:5],
                "reason": msg,
                "type": code,
            }
        )

    return {"errors": errors}
