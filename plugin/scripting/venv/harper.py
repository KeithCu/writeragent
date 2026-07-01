# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Trusted Harper Rust grammar linter helper executing inside the user's virtual environment."""

import json
import logging
import os
import platform
import queue
import shutil
import subprocess
import tarfile
import tempfile
import threading
import time
import urllib.request
import zipfile
from pathlib import Path


log = logging.getLogger("writeragent.grammar")

_JSONRPC = "2.0"
_INIT_PARAMS = {"processId": os.getpid(), "rootUri": "file:///tmp", "capabilities": {"textDocument": {"publishDiagnostics": {"relatedInformation": False}, "codeAction": {"dynamicRegistration": False, "codeActionLiteralSupport": {"codeActionKind": {"valueSet": ["quickfix"]}}}}}}


def _lsp_notification(method: str, params: dict) -> dict:
    return {"jsonrpc": _JSONRPC, "method": method, "params": params}


def _lsp_request(req_id: int, method: str, params: dict) -> dict:
    return {"jsonrpc": _JSONRPC, "id": req_id, "method": method, "params": params}


def _lsp_response(req_id: int, result) -> dict:
    return {"jsonrpc": _JSONRPC, "id": req_id, "result": result}


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


def _download_harper_binary(dest_path: Path):
    """Download precompiled harper-ls binary from Automattic/harper releases."""
    system = platform.system().lower()
    machine = platform.machine().lower()
    asset = _resolve_harper_asset(system, machine)

    url = f"https://github.com/Automattic/harper/releases/latest/download/{asset}"
    log.info(f"[harper] Downloading precompiled binary for {system}/{machine} from {url}")

    dest_path.parent.mkdir(parents=True, exist_ok=True)

    with tempfile.NamedTemporaryFile(suffix=Path(asset).suffix, delete=False) as tmp_file:
        tmp_name = tmp_file.name

    try:
        urllib.request.urlretrieve(url, tmp_name)
        _extract_harper_member(Path(tmp_name), dest_path)
        if dest_path.exists():
            os.chmod(dest_path, 0o755)  # nosec B103  # nosemgrep: insecure-file-permissions  # executable bit on downloaded harper-ls binary
            log.info(f"[harper] Binary installed successfully at {dest_path}")
    except Exception as e:
        log.error(f"[harper] Failed to download and extract binary: {e}")
        raise RuntimeError(f"Failed to auto-download Harper binary: {e}")
    finally:
        if os.path.exists(tmp_name):
            try:
                os.remove(tmp_name)
            except Exception:
                pass


def _get_harper_binary(user_config_dir: str) -> str:
    """Resolve path to harper-ls binary, auto-downloading if missing."""
    # 1. Check if harper-ls is installed globally on the system PATH
    sys_path = shutil.which("harper-ls")
    if sys_path:
        return sys_path

    # 2. Otherwise, check/download to the user profile bin directory
    bin_dir = Path(user_config_dir) / "bin"
    suffix = ".exe" if os.name == "nt" else ""
    binary_path = bin_dir / f"harper-ls{suffix}"

    if not binary_path.exists():
        _download_harper_binary(binary_path)

    return str(binary_path)


_HARPER_CLIENT_CACHE = {}


class HarperLSClient:
    def __init__(self, binary_path: str):
        self.binary_path = binary_path
        self.proc = None
        self.request_id = 0
        self.uri = f"file:///tmp/writeragent_harper_lint_{time.time_ns()}.txt"
        self._doc_version = 0
        self._doc_opened = False
        self.stdout_queue: queue.Queue = queue.Queue()
        self.stdout_thread = None
        self._initialize()

    def _initialize(self):
        try:
            self._doc_version = 0
            self._doc_opened = False
            self.stdout_queue = queue.Queue()
            self.proc = subprocess.Popen([self.binary_path, "--stdio"], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=0)
            self.stdout_thread = threading.Thread(target=self._read_loop, daemon=True)  # nosemgrep: raw-uno-thread-ban
            self.stdout_thread.start()

            init_params = dict(_INIT_PARAMS)
            init_params["processId"] = os.getpid()
            self._send_request("initialize", init_params)
            self._write(_lsp_notification("initialized", {}))
        except Exception as e:
            self.close()
            raise RuntimeError(f"Failed to start/initialize harper-ls: {e}")

    def _read_loop(self):
        try:
            while self.proc and self.proc.stdout:
                headers = {}
                while True:
                    line = self.proc.stdout.readline()
                    if not line:
                        break
                    line_str = line.decode("utf-8").strip()
                    if not line_str:
                        break
                    if ":" in line_str:
                        k, v = line_str.split(":", 1)
                        headers[k.strip().lower()] = v.strip()

                if not line:
                    break

                content_length = int(headers.get("content-length", 0))
                if content_length == 0:
                    continue

                body = self.proc.stdout.read(content_length).decode("utf-8")
                self.stdout_queue.put(json.loads(body))
        except Exception:
            pass
        finally:
            self.stdout_queue.put(None)

    def is_alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def _write(self, payload: dict):
        if not self.proc or self.proc.stdin is None:
            raise RuntimeError("harper-ls process not running")
        body = json.dumps(payload)
        header = f"Content-Length: {len(body.encode('utf-8'))}\r\n\r\n"
        self.proc.stdin.write((header + body).encode("utf-8"))
        self.proc.stdin.flush()

    def _read(self, timeout: float = 5.0) -> dict | None:
        if not self.proc:
            raise RuntimeError("harper-ls process not running")
        try:
            msg = self.stdout_queue.get(timeout=timeout)
            return msg
        except queue.Empty:
            raise TimeoutError("Timeout waiting for LSP response")

    def _read_and_handle(self, timeout: float = 5.0) -> dict | None:
        msg = self._read(timeout=timeout)
        if not msg:
            return None

        # If it is a request from the server, reply to keep it happy
        if "id" in msg and "method" in msg:
            method = msg["method"]
            if method == "workspace/configuration":
                self._write(_lsp_response(msg["id"], [{}]))
            else:
                self._write(_lsp_response(msg["id"], None))
            return self._read_and_handle(timeout=timeout)

        return msg

    def _send_request(self, method: str, params: dict, timeout: float = 5.0) -> dict | None:
        self.request_id += 1
        req_id = self.request_id
        self._write(_lsp_request(req_id, method, params))

        for _ in range(50):
            msg = self._read_and_handle(timeout=timeout)
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

    def _collect_diagnostics(self, version: int, timeout: float = 5.0) -> list:
        for _ in range(50):
            msg = self._read_and_handle(timeout=timeout)
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

    def _suggestions_for_diagnostic(self, diag: dict) -> list:
        suggestions = []
        try:
            res = self._send_request("textDocument/codeAction", {"textDocument": {"uri": self.uri}, "range": diag["range"], "context": {"diagnostics": [diag]}}, timeout=5.0)
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
            log.error(f"[harper] Failed to fetch codeActions: {e}")
        return suggestions

    def lint(self, text: str) -> list:
        if not self.is_alive():
            self._initialize()

        self._doc_version += 1
        version = self._doc_version

        try:
            self._sync_document(text, version)
            diagnostics = self._collect_diagnostics(version)
            return [{"diagnostic": diag, "suggestions": self._suggestions_for_diagnostic(diag)} for diag in diagnostics]
        except Exception as e:
            log.error(f"[harper] Exception during linting, closing client: {e}")
            self.close()
            raise

    def close(self):
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
    """Convert LSP 0-indexed line and character to absolute 0-indexed character offset."""
    lines = text.splitlines(keepends=True)
    if line >= len(lines):
        return len(text)
    offset = sum(len(line_text) for line_text in lines[:line])
    return min(offset + character, len(text))


def run_harper_check(text: str, user_config_dir: str) -> dict:
    """Run harper-ls on text segment and return parsed errors."""
    try:
        harper_bin = _get_harper_binary(user_config_dir)
    except Exception as e:
        raise RuntimeError(str(e))

    if harper_bin not in _HARPER_CLIENT_CACHE:
        _HARPER_CLIENT_CACHE[harper_bin] = HarperLSClient(harper_bin)

    client = _HARPER_CLIENT_CACHE[harper_bin]
    try:
        results = client.lint(text)
    except Exception as e:
        log.error(f"[harper] Linting error or connection lost, restarting client: {e}")
        client.close()
        # Retry once with a fresh client
        _HARPER_CLIENT_CACHE[harper_bin] = HarperLSClient(harper_bin)
        results = _HARPER_CLIENT_CACHE[harper_bin].lint(text)

    errors = []
    for item in results:
        diag = item["diagnostic"]
        suggestions = item["suggestions"]

        msg = diag.get("message", "")
        code = diag.get("code", "Grammar")

        # Translate LSP range to start and end character offsets
        diag_range = diag.get("range", {})
        start_pos = diag_range.get("start", {})
        end_pos = diag_range.get("end", {})

        start_line = start_pos.get("line", 0)
        start_char = start_pos.get("character", 0)
        end_line = end_pos.get("line", 0)
        end_char = end_pos.get("character", 0)

        start_offset = lsp_range_to_offset(text, start_line, start_char)
        end_offset = lsp_range_to_offset(text, end_line, end_char)
        length = max(1, end_offset - start_offset)

        correct = suggestions[0] if suggestions else ""

        errors.append(
            {
                "wrong": text[start_offset : start_offset + length] if start_offset + length <= len(text) else "",
                "correct": correct,
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
