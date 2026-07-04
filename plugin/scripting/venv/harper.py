# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Trusted Harper Rust grammar linter helper executing inside the user's virtual environment."""

from __future__ import annotations

import logging
import os
import queue
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import BinaryIO, cast

from plugin.framework.worker_pool import get_subprocess_creationflags
from plugin.scripting.sandbox import wrap_command_for_sandbox

from plugin.contrib.lsp import json_rpc_framing
from plugin.contrib.lsp.position_codec import ClientPosition, PositionCodec
from plugin.scripting.venv.harper_binary import _get_harper_binary
from plugin.writer.locale.grammar_ignore_rules import HARPER_RULE_PREFIX, make_rule_identifier

log = logging.getLogger("writeragent.grammar")

_JSONRPC = "2.0"
_INIT_PARAMS = {"processId": os.getpid(), "rootUri": "file:///tmp", "capabilities": {"textDocument": {"publishDiagnostics": {"relatedInformation": False}, "codeAction": {"dynamicRegistration": False, "codeActionLiteralSupport": {"codeActionKind": {"valueSet": ["quickfix"]}}}}}}

_LINT_BUDGET_SEC = 15.0
_INIT_BUDGET_SEC = 5.0

_LSP_POSITION_CODEC = PositionCodec("utf-16")

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
            self.proc = cast(
                "subprocess.Popen[bytes]",
                subprocess.Popen(
                    wrap_command_for_sandbox([self.binary_path, "--stdio"]),
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                    bufsize=0,
                    **get_subprocess_creationflags(),
                ),
            )
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
                "rule_identifier": make_rule_identifier(HARPER_RULE_PREFIX, code),
                "suggestions": suggestions[:5],
                "reason": msg,
                "type": code,
            }
        )

    return {"errors": errors}
