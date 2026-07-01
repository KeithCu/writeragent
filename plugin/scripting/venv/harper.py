# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Trusted Harper Rust grammar linter helper executing inside the user's virtual environment."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import platform
import queue
import shutil
import subprocess
import tarfile
import threading
import time
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path

from plugin.framework.constants import USER_AGENT


log = logging.getLogger("writeragent.grammar")

_JSONRPC = "2.0"
_INIT_PARAMS = {"processId": os.getpid(), "rootUri": "file:///tmp", "capabilities": {"textDocument": {"publishDiagnostics": {"relatedInformation": False}, "codeAction": {"dynamicRegistration": False, "codeActionLiteralSupport": {"codeActionKind": {"valueSet": ["quickfix"]}}}}}}

_HARPER_RELEASES_API = "https://api.github.com/repos/Automattic/harper/releases/latest"
_LSP_MAX_FRAME_BYTES = 10 * 1024 * 1024
_DOWNLOAD_MAX_BYTES = 20 * 1024 * 1024
_DOWNLOAD_TIMEOUT_SEC = 120
_RELEASE_CHECK_INTERVAL_SEC = 7 * 24 * 3600
_RELEASE_CACHE_FILENAME = "harper-ls.release.json"
_LINT_BUDGET_SEC = 15.0
_INIT_BUDGET_SEC = 5.0

_release_cache: dict[str, tuple[float, HarperReleaseAsset]] = {}


@dataclass(frozen=True)
class HarperReleaseAsset:
    version: str
    asset_name: str
    download_url: str
    sha256: str


_BCP47_TO_DIALECT: dict[str, str] = {"en-GB": "British", "en-AU": "Australian", "en-CA": "Canadian"}


def _lsp_notification(method: str, params: dict) -> dict:
    return {"jsonrpc": _JSONRPC, "method": method, "params": params}


def _lsp_request(req_id: int, method: str, params: dict) -> dict:
    return {"jsonrpc": _JSONRPC, "id": req_id, "method": method, "params": params}


def _lsp_response(req_id: int, result) -> dict:
    return {"jsonrpc": _JSONRPC, "id": req_id, "result": result}


def _deadline_remaining(deadline: float) -> float:
    return max(0.0, deadline - time.monotonic())


def _read_exactly(stream, nbytes: int) -> bytes:
    chunks: list[bytes] = []
    remaining = nbytes
    while remaining > 0:
        chunk = stream.read(remaining)
        if not chunk:
            raise RuntimeError("Unexpected EOF while reading LSP frame body")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


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
    return name.endswith("harper-ls.exe") or name.endswith("harper-ls")


def _extract_harper_member(archive_path: Path, dest_path: Path) -> None:
    """Extract harper-ls binary from a release tarball or zip into dest_path."""
    if archive_path.suffix == ".gz" or str(archive_path).endswith(".tar.gz"):
        with tarfile.open(archive_path, "r:gz") as tar:
            for member in tar.getmembers():
                if _is_harper_member(member.name):
                    extracted = tar.extractfile(member)
                    if extracted:
                        dest_path.write_bytes(extracted.read())
                        return
        raise RuntimeError("harper-ls file not found inside tarball")

    with zipfile.ZipFile(archive_path, "r") as zip_ref:
        for file_info in zip_ref.infolist():
            if _is_harper_member(file_info.filename):
                dest_path.write_bytes(zip_ref.read(file_info))
                return
    raise RuntimeError("harper-ls file not found inside zip archive")


def _verify_sha256(path: Path, expected: str) -> None:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    if digest.hexdigest() != expected.lower():
        raise RuntimeError(f"Harper download failed SHA256 check for {path.name}")


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


def _release_cache_path(bin_dir: Path) -> Path:
    return bin_dir / _RELEASE_CACHE_FILENAME


def _read_persisted_release(bin_dir: Path, asset_name: str) -> HarperReleaseAsset | None:
    path = _release_cache_path(bin_dir)
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


def _write_persisted_release(bin_dir: Path, release: HarperReleaseAsset) -> None:
    bin_dir.mkdir(parents=True, exist_ok=True)
    payload = {"checked_at": time.time(), "version": release.version, "asset_name": release.asset_name, "download_url": release.download_url, "sha256": release.sha256}
    _release_cache_path(bin_dir).write_text(json.dumps(payload), encoding="utf-8")


def _fetch_latest_release_asset(system: str, machine: str, bin_dir: Path) -> HarperReleaseAsset:
    """Resolve the latest harper-ls asset for this platform (checked at most once per week)."""
    asset_name = _resolve_harper_asset(system, machine)

    persisted = _read_persisted_release(bin_dir, asset_name)
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
        _write_persisted_release(bin_dir, info)
        _release_cache[asset_name] = (time.time(), info)
        return info

    raise RuntimeError(f"Harper asset {asset_name} not found in latest release {tag_name}")


def _read_installed_version(bin_dir: Path) -> str | None:
    sidecar = bin_dir / "harper-ls.version"
    if not sidecar.is_file():
        return None
    try:
        version = sidecar.read_text(encoding="utf-8").strip()
        return version or None
    except Exception:
        return None


def _download_harper_binary(dest_path: Path, release: HarperReleaseAsset) -> None:
    """Download harper-ls from GitHub with timeout, size cap, and SHA256 check."""
    log.info("[harper] Downloading v%s binary (%s) from %s", release.version, release.asset_name, release.download_url)

    dest_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_archive = dest_path.parent / f".{release.asset_name}.download"
    tmp_binary = dest_path.parent / f".harper-ls{'.exe' if os.name == 'nt' else ''}.download"

    try:
        request = urllib.request.Request(release.download_url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(request, timeout=_DOWNLOAD_TIMEOUT_SEC) as response:
            total = 0
            with tmp_archive.open("wb") as handle:
                while True:
                    chunk = response.read(1024 * 1024)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > _DOWNLOAD_MAX_BYTES:
                        raise RuntimeError(f"Harper download exceeded {_DOWNLOAD_MAX_BYTES} bytes")
                    handle.write(chunk)

        _verify_sha256(tmp_archive, release.sha256)
        _extract_harper_member(tmp_archive, tmp_binary)
        os.chmod(tmp_binary, 0o755)  # nosec B103  # nosemgrep: insecure-file-permissions
        os.replace(tmp_binary, dest_path)
        (dest_path.parent / "harper-ls.version").write_text(release.version, encoding="utf-8")
        log.info("[harper] Binary v%s installed at %s", release.version, dest_path)
    except Exception as e:
        log.error("[harper] Failed to download and extract binary: %s", e)
        raise RuntimeError(f"Failed to auto-download Harper binary: {e}")
    finally:
        for tmp in (tmp_archive, tmp_binary):
            try:
                if tmp.exists():
                    tmp.unlink()
            except Exception:
                pass


def _get_harper_binary(user_config_dir: str) -> str:
    """Resolve path to harper-ls binary, auto-downloading if missing or outdated."""
    sys_path = shutil.which("harper-ls")
    if sys_path:
        return sys_path

    bin_dir = Path(user_config_dir) / "bin"
    suffix = ".exe" if os.name == "nt" else ""
    binary_path = bin_dir / f"harper-ls{suffix}"

    system = platform.system().lower()
    machine = platform.machine().lower()
    release = _fetch_latest_release_asset(system, machine, bin_dir)
    installed = _read_installed_version(bin_dir)

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
        self.proc = None
        self.request_id = 0
        self.uri = f"file:///tmp/writeragent_harper_lint_{time.time_ns()}.txt"
        self._doc_version = 0
        self._doc_opened = False
        self.stdout_queue: queue.Queue = queue.Queue()
        self.stdout_thread = None
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
                headers: dict[str, str] = {}
                while True:
                    line = self.proc.stdout.readline()
                    if not line:
                        break
                    line_str = line.decode("utf-8").strip()
                    if not line_str:
                        break
                    if ":" in line_str:
                        key, value = line_str.split(":", 1)
                        headers[key.strip().lower()] = value.strip()

                if not line:
                    break

                raw_length = headers.get("content-length")
                if raw_length is None:
                    log.warning("[harper] LSP frame missing Content-Length header")
                    continue
                try:
                    content_length = int(raw_length)
                except ValueError:
                    log.warning("[harper] Invalid Content-Length header: %r", raw_length)
                    continue
                if content_length <= 0 or content_length > _LSP_MAX_FRAME_BYTES:
                    log.warning("[harper] Rejecting LSP frame with Content-Length=%s", content_length)
                    continue

                body = _read_exactly(self.proc.stdout, content_length).decode("utf-8")
                self.stdout_queue.put(json.loads(body))
        except Exception:
            log.exception("[harper] LSP reader failed")
        finally:
            self.stdout_queue.put(None)

    def is_alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def _write(self, payload: dict) -> None:
        if not self.proc or self.proc.stdin is None:
            raise RuntimeError("harper-ls process not running")
        body = json.dumps(payload)
        header = f"Content-Length: {len(body.encode('utf-8'))}\r\n\r\n"
        self.proc.stdin.write((header + body).encode("utf-8"))
        self.proc.stdin.flush()

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
    """Convert LSP 0-indexed line/character to a 0-indexed offset into *text*.

    Harper reports diagnostics in LSP form; the grammar queue stores ``n_error_start``
    as an offset into the checked sentence string. Grammar work is sentence-scoped,
    but a sentence can still contain embedded newlines (e.g. Writer soft line breaks),
    so line > 0 is valid and uses the multiline path below.
    """
    if "\n" not in text and "\r" not in text:
        if line == 0:
            return min(character, len(text))
        return len(text)

    lines = text.splitlines(keepends=True)
    if line >= len(lines):
        return len(text)
    offset = sum(len(line_text) for line_text in lines[:line])
    return min(offset + character, len(text))


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
