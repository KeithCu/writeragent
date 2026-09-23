# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Harper grammar: persistent harper-ls LSP client plus host entry for the grammar queue.

Runs in-process on LibreOffice's grammar drain thread (not the warm venv worker / trusted
RPC path used by LanguageTool and Vale). Status UI refresh during progress is best-effort
(``post_to_main_thread``); a busy main thread must not abort the check.
"""

from __future__ import annotations

import enum
import logging
import os
import queue
import subprocess
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any, BinaryIO, cast

if TYPE_CHECKING:
    from collections.abc import Callable

from plugin.framework.worker_pool import get_subprocess_creationflags
from plugin.scripting.sandbox import wrap_command_for_sandbox

from plugin.contrib.lsp import json_rpc_framing
from plugin.contrib.lsp.position_codec import ClientPosition, PositionCodec
from plugin.writer.locale.harper_binary import _get_harper_binary
from plugin.writer.locale.grammar_ignore_rules import HARPER_RULE_PREFIX, make_rule_identifier

log = logging.getLogger("writeragent.grammar")

_JSONRPC = "2.0"
_INIT_PARAMS = {"processId": os.getpid(), "rootUri": "file:///tmp", "capabilities": {"textDocument": {"publishDiagnostics": {"relatedInformation": False}, "codeAction": {"dynamicRegistration": False, "codeActionLiteralSupport": {"codeActionKind": {"valueSet": ["quickfix"]}}}}}}

_LINT_BUDGET_SEC = 15.0
_INIT_BUDGET_SEC = 5.0

# LibreHarper often logs at WARN only. One line when lint+normalize exceeds
# this budget so slowness shows up in writeragent_debug.log; faster calls stay quiet.
HARPER_SLOW_RESULT_MS = 500

_LSP_POSITION_CODEC = PositionCodec("utf-16")

_BCP47_TO_DIALECT: dict[str, str] = {"en-GB": "British", "en-AU": "Australian", "en-CA": "Canadian", "en-IN": "Indian"}


def _lsp_notification(method: str, params: dict | None) -> dict:
    return {"jsonrpc": _JSONRPC, "method": method, "params": params}


def _lsp_request(req_id: int, method: str, params: dict | None) -> dict:
    return {"jsonrpc": _JSONRPC, "id": req_id, "method": method, "params": params}


def _lsp_response(req_id: int, result: Any) -> dict:
    return {"jsonrpc": _JSONRPC, "id": req_id, "result": result}


def _deadline_remaining(deadline: float) -> float:
    return max(0.0, deadline - time.monotonic())


def _harper_lsp_settings(bcp47: str, user_config_dir: str) -> dict:
    dialect = _BCP47_TO_DIALECT.get(bcp47, "American")
    settings: dict = {"dialect": dialect}
    if user_config_dir:
        settings["userDictPath"] = str(Path(user_config_dir) / "harper-dictionary.txt")
    return {"harper-ls": settings}


# One LSP client per binary path. Lock serializes brief client critical
# sections (cache/state + ``client.lint`` on the worker). Never hold it
# across ``process_events_to_idle`` or a long LSP wait on the linguistic
# thread: PE2I can re-enter ``doProofreading`` and a non-reentrant mutex
# would deadlock.
_HARPER_CLIENT_CACHE: dict[str, HarperLSClient] = {}
_HARPER_LOCK = threading.Lock()
# Wait-active is a separate flag so a nested walk can fail soft without
# touching ``_HARPER_LOCK``. One WARN/obs line per nest, not per PE2I tick.
_HARPER_WAIT_META = threading.Lock()
_HARPER_WAIT_STARTED: float | None = None
_HARPER_WAIT_THREAD = ""
_HARPER_FAIL_COOLDOWN_SEC = 30.0


class HarperRuntimeState(enum.Enum):
    IDLE = "idle"
    RESOLVING = "resolving"
    READY = "ready"
    FAILED = "failed"


_HARPER_STATE = HarperRuntimeState.IDLE
_HARPER_FAILED_AT = 0.0


def _emit_progress(heartbeat_fn: Callable[[dict[str, str]], None] | None, message: str) -> None:
    if heartbeat_fn is not None:
        heartbeat_fn({"message": message})


class HarperLSClient:
    def __init__(self, binary_path: str, user_config_dir: str = "", bcp47: str = "en-US", *, heartbeat_fn: Callable[[dict[str, str]], None] | None = None):
        self.binary_path = binary_path
        self.user_config_dir = user_config_dir
        self._bcp47 = bcp47
        self._heartbeat_fn = heartbeat_fn
        self._lsp_settings = _harper_lsp_settings(bcp47, user_config_dir)
        self.proc: subprocess.Popen[bytes] | None = None
        self.request_id = 0
        self.uri = f"file:///tmp/writeragent_harper_lint_{time.time_ns()}.txt"
        self._doc_version = 0
        self._doc_opened = False
        self.stdout_queue: queue.Queue = queue.Queue()
        self.stdout_thread: threading.Thread | None = None
        self._initialize()

    def _initialize(self) -> None:
        try:
            if self.proc is not None:
                self.close()
            self._doc_version = 0
            self._doc_opened = False
            self.stdout_queue = queue.Queue()
            _emit_progress(self._heartbeat_fn, "Starting harper-ls…")
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
            _emit_progress(self._heartbeat_fn, "Initializing Harper LSP…")
            self._send_request("initialize", init_params, deadline=deadline)
            self._write(_lsp_notification("initialized", {}))
        except Exception as e:
            self.close()
            log.exception("[harper] Failed to start/initialize harper-ls")
            raise RuntimeError(f"Failed to start/initialize harper-ls: {e}") from e

    def _read_loop(self) -> None:
        try:
            while self.proc and self.proc.stdout:
                msg = json_rpc_framing.read_frame(cast("BinaryIO", self.proc.stdout))
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
        json_rpc_framing.write_frame(cast("BinaryIO", self.proc.stdin), payload)

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
        except Exception:
            log.exception("[harper] Failed to fetch codeActions")
        return suggestions

    def lint(self, text: str, bcp47: str = "en-US", *, heartbeat_fn: Callable[[dict[str, str]], None] | None = None) -> list:
        # One lint at a time: venv worker IPC is serialized; grammar uses a single drain thread for Harper.
        if heartbeat_fn is not None:
            self._heartbeat_fn = heartbeat_fn
        if not self.is_alive():
            self._initialize()

        _emit_progress(self._heartbeat_fn, "Linting…")

        self._apply_bcp47(bcp47)
        self._doc_version += 1
        version = self._doc_version
        deadline = time.monotonic() + _LINT_BUDGET_SEC

        try:
            self._sync_document(text, version)
            diagnostics = self._collect_diagnostics(version, deadline)
            return [{"diagnostic": diag, "suggestions": self._suggestions_for_diagnostic(diag, deadline)} for diag in diagnostics]
        except Exception:
            log.exception("[harper] Exception during linting, closing client")
            self.close()
            raise

    def close(self) -> None:
        """Tear down harper-ls without blocking on a stuck stdin pipe.

        The previous path wrote LSP shutdown/exit then waited. On Windows a
        hung harper-ls fills the stdin pipe; ``stdin.write`` blocks forever
        and xdist workers never exit (CI sat ~19 min after pytest 99%).
        Close the pipes first, then terminate/kill with short waits.
        """
        proc = self.proc
        self.proc = None
        self._doc_opened = False
        if proc is None:
            return
        for stream in (proc.stdin, proc.stdout):
            if stream is not None:
                try:
                    stream.close()
                except Exception:
                    pass
        if proc.poll() is not None:
            return
        try:
            proc.terminate()
            proc.wait(timeout=0.5)
        except Exception:
            try:
                proc.kill()
                proc.wait(timeout=0.5)
            except Exception:
                pass


def lsp_range_to_offset(text: str, line: int, character: int) -> int:
    """Convert LSP 0-indexed line/character (UTF-16 code units) to a Python string offset."""
    lines = [text] if ("\n" not in text and "\r" not in text) else text.splitlines(keepends=True)
    if line >= len(lines):
        return len(text)
    pos = _LSP_POSITION_CODEC.position_from_client_units(lines, ClientPosition(line=line, character=character))
    offset = sum(len(lines[i]) for i in range(pos.line))
    return min(offset + pos.character, len(text))


def _try_begin_harper_wait() -> bool:
    """Mark a Harper lint wait in flight. False if one is already active."""
    global _HARPER_WAIT_STARTED, _HARPER_WAIT_THREAD
    with _HARPER_WAIT_META:
        if _HARPER_WAIT_STARTED is not None:
            return False
        _HARPER_WAIT_STARTED = time.monotonic()
        _HARPER_WAIT_THREAD = threading.current_thread().name
        return True


def _end_harper_wait() -> None:
    global _HARPER_WAIT_STARTED, _HARPER_WAIT_THREAD
    with _HARPER_WAIT_META:
        _HARPER_WAIT_STARTED = None
        _HARPER_WAIT_THREAD = ""


def _harper_wait_snapshot() -> tuple[float | None, str]:
    with _HARPER_WAIT_META:
        return _HARPER_WAIT_STARTED, _HARPER_WAIT_THREAD


def _log_harper_wait_reenter() -> None:
    """One WARN + obs when PE2I / another walk nests during an active Harper wait."""
    from plugin.writer.locale.grammar_obs import grammar_obs

    started, wait_thread = _harper_wait_snapshot()
    wait_age_ms = int((time.monotonic() - started) * 1000) if started is not None else 0
    provider = "harper"
    try:
        from plugin.framework.config import get_grammar_provider

        provider = get_grammar_provider() or "harper"
    except Exception:
        pass
    thread_name = threading.current_thread().name
    log.warning(
        "[harper] harper_wait_reenter thread=%s wait_thread=%s wait_age_ms=%s provider=%s",
        thread_name,
        wait_thread,
        wait_age_ms,
        provider,
    )
    grammar_obs(
        "harper_wait_reenter",
        thread=thread_name,
        wait_thread=wait_thread,
        wait_age_ms=wait_age_ms,
        provider=provider,
    )


def shutdown_harper_runtime() -> None:
    """Close every cached harper-ls client. Safe from tests and extension teardown."""
    _end_harper_wait()
    with _HARPER_LOCK:
        clients = list(_HARPER_CLIENT_CACHE.values())
        _HARPER_CLIENT_CACHE.clear()
        _set_state(HarperRuntimeState.IDLE, failed_at=0.0)
    for client in clients:
        try:
            client.close()
        except Exception:
            log.debug("[harper] shutdown close failed", exc_info=True)


def _get_or_create_client(harper_bin: str, user_config_dir: str, bcp47: str, *, heartbeat_fn: Callable[[dict[str, str]], None] | None = None) -> HarperLSClient:
    client = _HARPER_CLIENT_CACHE.get(harper_bin)
    if client is None:
        client = HarperLSClient(harper_bin, user_config_dir=user_config_dir, bcp47=bcp47, heartbeat_fn=heartbeat_fn)
        _HARPER_CLIENT_CACHE[harper_bin] = client
    elif heartbeat_fn is not None:
        client._heartbeat_fn = heartbeat_fn
    return client


def _alive_client() -> HarperLSClient | None:
    for client in _HARPER_CLIENT_CACHE.values():
        if client.is_alive():
            return client
    return None


def _set_state(state: HarperRuntimeState, *, failed_at: float | None = None) -> None:
    global _HARPER_STATE, _HARPER_FAILED_AT
    _HARPER_STATE = state
    if failed_at is not None:
        _HARPER_FAILED_AT = failed_at


def _can_start_ensure_locked() -> bool:
    """Caller holds ``_HARPER_LOCK``."""
    if _HARPER_STATE is HarperRuntimeState.RESOLVING:
        return False
    if _HARPER_STATE is HarperRuntimeState.READY and _alive_client() is not None:
        return False
    if _HARPER_STATE is HarperRuntimeState.FAILED:
        if time.monotonic() - _HARPER_FAILED_AT < _HARPER_FAIL_COOLDOWN_SEC:
            return False
    return True


def harper_runtime_is_ready() -> bool:
    """True when harper-ls is in-process and the runtime state is READY."""
    with _HARPER_LOCK:
        return _HARPER_STATE is HarperRuntimeState.READY and _alive_client() is not None


def _broadcast_proofread_again() -> None:
    """Writer may have already walked the document while harper-ls was starting."""
    from plugin.writer.locale.grammar_persistence import grammar_registry

    for pr in list(grammar_registry.live_proofreaders):
        fn = getattr(pr, "broadcast_proofread_again", None)
        if not callable(fn):
            continue
        try:
            fn()
        except Exception:
            log.debug("[harper] PROOFREAD_AGAIN failed", exc_info=True)


def _schedule_proofread_again() -> None:
    try:
        from plugin.framework.queue_executor import post_to_main_thread

        post_to_main_thread(_broadcast_proofread_again)
    except Exception as e:
        log.debug("[harper] Could not schedule PROOFREAD_AGAIN: %s", e)


def _harper_ensure_ready_body(user_config_dir: str, bcp47: str) -> None:
    try:
        from plugin.writer.locale.grammar_obs import emit_harper_worker_status

        def _on_progress(payload: dict[str, str]) -> None:
            message = str(payload.get("message") or "").strip()
            if message:
                emit_harper_worker_status("Harper", message)

        harper_bin = _get_harper_binary(user_config_dir, heartbeat_fn=_on_progress)
        with _HARPER_LOCK:
            client = _get_or_create_client(harper_bin, user_config_dir, bcp47)
            if not client.is_alive():
                raise RuntimeError("harper-ls process not running after start")
            _set_state(HarperRuntimeState.READY)
        emit_harper_worker_status("Harper", "Harper ready")
        _schedule_proofread_again()
    except Exception:
        log.exception("[harper] Background ensure failed")
        with _HARPER_LOCK:
            _set_state(HarperRuntimeState.FAILED, failed_at=time.monotonic())


def harper_ensure_ready_async(user_config_dir: str, bcp47: str = "en-US") -> bool:
    """Start at most one download/start job. Returns True if a job was submitted."""
    with _HARPER_LOCK:
        if not _can_start_ensure_locked():
            return False
        _set_state(HarperRuntimeState.RESOLVING)
    from plugin.framework.worker_pool import run_in_background

    try:
        run_in_background(
            _harper_ensure_ready_body,
            user_config_dir,
            bcp47,
            name="harper-ensure-ready",
        )
    except Exception:
        log.exception("[harper] Could not submit ensure job")
        with _HARPER_LOCK:
            if _HARPER_STATE is HarperRuntimeState.RESOLVING:
                _set_state(HarperRuntimeState.IDLE)
        return False
    return True


def maybe_start_harper_async(
    ctx: Any = None,
    *,
    user_config_dir: str | None = None,
    bcp47: str = "en-US",
) -> bool:
    """Start background warmup of harper-ls if Harper is the active/enabled grammar engine.

    Call after ``init_config`` so ``user_config_dir`` is the LibreOffice profile folder
    (parent of writeragent.json). Empty path is a no-op. WriterAgent starts from OnStartApp;
    LibreHarper from HarperProofreader after init_config (no Jobs.xcu). Returns True if submitted.
    """
    from plugin.framework.config import get_grammar_provider, is_grammar_enabled, user_config_dir as get_ucd
    from plugin.framework.uno_context import is_libreharper

    if not is_libreharper():
        if not is_grammar_enabled() or get_grammar_provider() != "harper":
            return False

    ucd = user_config_dir
    if not ucd:
        try:
            if ctx is not None:
                from plugin.framework.config import init_config

                init_config(ctx)
            ucd = get_ucd() or ""
        except Exception:
            ucd = ""

    # Empty profile path would resolve harper/ relative to soffice cwd and FAIL,
    # which blocks doProofreading for the fail cooldown (Writer will not walk again).
    if not ucd:
        log.debug("[harper] skip warmup: user config dir not ready")
        return False

    return harper_ensure_ready_async(ucd, bcp47=bcp47)


def harper_try_lint(text: str, user_config_dir: str, bcp47: str = "en-US", *, ctx: Any = None) -> dict | None:
    """Lint now if harper-ls is already in-process; else kick one ensure and return None.

    Never downloads or ``Popen``s on the caller thread (UNO ``doProofreading``).
    A ``None`` return is never silent: failures log ERROR, not-ready walks emit obs.

    When ``ctx`` is set (``doProofreading``), the blocking LSP wait runs on a
    dedicated worker and the caller uses ``wait_while_pumping`` so typing stays
    alive. Writer runs ``doProofreading`` on a linguistic worker (``Dummy-*``),
    not VCL: the helper posts PE2I to the main thread instead of pumping on
    this stack (in-loop PE2I there was a thread-violation dialog). Nested
    ``doProofreading`` / ``harper_try_lint`` while that wait is active fail
    soft (``None``) and log ``harper_wait_reenter``. Missing ``ctx`` falls
    back to a blocking wait (no pump). Grammar-queue ``run_harper_check``
    does not pass ``ctx``: that thread is already a worker and paints status
    via ``_pump_grammar_status_ui``.
    """
    from plugin.writer.locale.grammar_obs import grammar_obs

    if not user_config_dir:
        log.error("[harper] lint skipped: empty user config dir")
        return None
    # Fail soft before ``_HARPER_LOCK``: PE2I can re-enter this function on
    # the linguistic thread; waiting on a lock the parent still needs deadlocks.
    if _harper_wait_snapshot()[0] is not None:
        _log_harper_wait_reenter()
        return None
    none_reason = "not_ready"
    client: HarperLSClient | None = None
    with _HARPER_LOCK:
        client = _alive_client()
        if client is not None:
            _set_state(HarperRuntimeState.READY)
        elif _HARPER_STATE is HarperRuntimeState.READY:
            log.error("[harper] lint missed: state READY but harper-ls process is dead")
            _set_state(HarperRuntimeState.IDLE)
            none_reason = "dead_client"
        else:
            none_reason = f"state_{_HARPER_STATE.value}"
    if client is None:
        submitted = harper_ensure_ready_async(user_config_dir, bcp47)
        if none_reason in ("lint_exception", "dead_client"):
            return None
        grammar_obs("harper_try_lint_none", reason=none_reason, ensure_submitted=submitted)
        return None
    if not _try_begin_harper_wait():
        _log_harper_wait_reenter()
        return None
    try:
        return _lint_ready_client(client, text, bcp47=bcp47, ctx=ctx)
    except Exception:
        # restart=False: do not Popen on the linguistic thread. The
        # walk returns empty; background ensure restarts harper-ls.
        log.exception("[harper] lint failed on ready client; empty aErrors this walk")
        with _HARPER_LOCK:
            _set_state(HarperRuntimeState.IDLE)
        harper_ensure_ready_async(user_config_dir, bcp47)
        return None
    finally:
        _end_harper_wait()


def normalize_spaces_1to1(text: str) -> str:
    """Normalize non-standard Unicode spaces (NBSP, CJK spaces, etc.) to ASCII ' '.

    Preserves exact 1:1 character length and offsets for LSP coordinate mapping.
    Leaves newlines ('\\n', '\\r') untouched.
    """
    if not text:
        return ""
    return "".join(" " if ch.isspace() and ch not in "\r\n" else ch for ch in text)


def warn_if_harper_result_slow(
    elapsed_ms: int,
    *,
    text_len: int,
    error_count: int,
    cache: str = "miss",
) -> bool:
    """Log one WARN when Harper lint+normalize took more than 500ms.

    Returns True if a warning was emitted. Sub-threshold calls are silent so
    the linguistic hot path does not spam. ``cache`` is ``miss`` when we
    actually linted (the usual path) or ``hit`` if a caller timed a cache
    return — cheap to pass when known.
    """
    if elapsed_ms <= HARPER_SLOW_RESULT_MS:
        return False
    log.warning(
        "[harper] slow result elapsed_ms=%s text_len=%s errors=%s cache=%s",
        elapsed_ms,
        text_len,
        error_count,
        cache,
    )
    return True


def _diagnostics_to_errors(text: str, results: list) -> dict:
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


def _lint_ready_client(
    client: HarperLSClient,
    text: str,
    bcp47: str,
    *,
    ctx: Any,
) -> dict:
    """Lint a READY client. With ``ctx``, worker waits; caller pumps VCL.

    Without ``ctx`` there is nothing to pump: block on the caller thread
    (tests / scripts). The wait-active flag is already set by ``harper_try_lint``.
    """
    if ctx is None:
        with _HARPER_LOCK:
            return _lint_with_client(client, text, bcp47=bcp47, restart=False)
    return _run_lint_off_caller_thread(client, text, bcp47=bcp47, ctx=ctx, restart=False)


def _run_lint_off_caller_thread(
    client: HarperLSClient,
    text: str,
    bcp47: str,
    ctx: Any,
    *,
    restart: bool,
    heartbeat_fn: Callable[[dict[str, str]], None] | None = None,
) -> dict:
    """Worker owns blocking ``client.lint`` + ``_HARPER_LOCK``; caller pumps or joins.

    Linguistic thread must not hold ``_HARPER_LOCK`` here: PE2I can nest
    ``doProofreading`` and that walk fail-softs via wait-active, not this mutex.
    """
    box: dict[str, Any] = {}
    done = threading.Event()

    def _worker() -> None:
        try:
            with _HARPER_LOCK:
                box["result"] = _lint_with_client(
                    client, text, bcp47=bcp47, heartbeat_fn=heartbeat_fn, restart=restart
                )
        except Exception as exc:
            box["exc"] = exc
        finally:
            done.set()

    from plugin.framework.worker_pool import run_in_background

    handle = run_in_background(_worker, name="harper-lint-wait", dedicated=True)
    deadline = time.monotonic() + _LINT_BUDGET_SEC
    from plugin.framework.uno_context import wait_while_pumping

    wait_while_pumping(done, ctx, timeout=_deadline_remaining(deadline))
    if not done.is_set():
        # Budget ended while lint still ran (its own ``_LINT_BUDGET_SEC``).
        # Join leftover without PE2I so wait-active is not cleared under an
        # in-flight LSP that still holds ``_HARPER_LOCK``.
        handle.join(timeout=max(1.0, _LINT_BUDGET_SEC))
    if "exc" in box:
        raise box["exc"]
    result = box.get("result")
    if result is None:
        raise TimeoutError("Harper LSP operation timed out")
    return result


def _lint_with_client(
    client: HarperLSClient,
    text: str,
    bcp47: str,
    *,
    heartbeat_fn: Callable[[dict[str, str]], None] | None = None,
    restart: bool = True,
) -> dict:
    """Caller holds ``_HARPER_LOCK``. ``restart=False`` avoids ``Popen`` on the UNO thread."""
    started = time.monotonic()
    lint_text = normalize_spaces_1to1(text)
    error_count = 0
    try:
        try:
            results = client.lint(lint_text, bcp47=bcp47, heartbeat_fn=heartbeat_fn)
        except Exception:
            log.exception("[harper] Linting error or connection lost, restarting client")
            client.close()
            if not restart:
                raise
            restarted = HarperLSClient(client.binary_path, user_config_dir=client.user_config_dir, bcp47=bcp47, heartbeat_fn=heartbeat_fn)
            _HARPER_CLIENT_CACHE[client.binary_path] = restarted
            results = restarted.lint(lint_text, bcp47=bcp47, heartbeat_fn=heartbeat_fn)
        out = _diagnostics_to_errors(text, results)
        error_count = len(out.get("errors") or [])
        return out
    finally:
        # Wall time of lint + normalize into errors (worker or caller thread).
        elapsed_ms = int((time.monotonic() - started) * 1000)
        warn_if_harper_result_slow(
            elapsed_ms,
            text_len=len(text),
            error_count=error_count,
            cache="miss",
        )


def run_harper_lint(
    text: str,
    user_config_dir: str,
    bcp47: str = "en-US",
    *,
    heartbeat_fn: Callable[[dict[str, str]], None] | None = None,
) -> dict:
    """Run harper-ls on a text segment and return parsed errors (no LibreOffice UI).

    Grammar-queue entry: already a worker thread, so lint under ``_HARPER_LOCK``
    on the caller. Linguistic ``doProofreading`` pumping lives in
    ``harper_try_lint`` (``ctx``), not here.
    """
    try:
        harper_bin = _get_harper_binary(user_config_dir, heartbeat_fn=heartbeat_fn)
    except Exception as e:
        log.exception("[harper] Failed to resolve harper-ls binary")
        raise RuntimeError(str(e)) from e

    with _HARPER_LOCK:
        client = _get_or_create_client(harper_bin, user_config_dir, bcp47, heartbeat_fn=heartbeat_fn)
        _set_state(HarperRuntimeState.READY)
        return _lint_with_client(client, text, bcp47=bcp47, heartbeat_fn=heartbeat_fn)


def _pump_grammar_status_ui(ctx: Any) -> None:
    """Best-effort drain of grammar status UI on the LO main thread.

    Must never block or fail the Harper check: a busy VCL / delayed AsyncCallback
    used to raise TimeoutError from execute_on_main_thread(timeout=2.0) and abort
    linting even though status painting is optional.
    """
    from plugin.framework.queue_executor import post_to_main_thread, pump_main_thread_work_queue
    from plugin.framework.uno_context import process_events_to_idle

    def _pump() -> None:
        pump_main_thread_work_queue(max_items=8)
        # Chokepoint: no-ops while a stream drain owns VCL pumping.
        process_events_to_idle(ctx)

    try:
        post_to_main_thread(_pump)
    except Exception as e:
        log.warning("[grammar] Harper status UI pump skipped: %s", e)


def run_harper_check(ctx: Any, text: str, config_dir: str, *, bcp47: str = "en-US") -> dict[str, Any]:
    """Grammar-queue entry: status UI + in-process harper-ls lint (no venv worker)."""
    from plugin.writer.locale.grammar_obs import emit_harper_worker_status

    emit_harper_worker_status(text, "Starting Harper…")
    _pump_grammar_status_ui(ctx)

    def _on_progress(payload: dict[str, Any]) -> None:
        message = str(payload.get("message") or "").strip()
        if message:
            emit_harper_worker_status(text, message)
            _pump_grammar_status_ui(ctx)

    return run_harper_lint(text, config_dir, bcp47=bcp47, heartbeat_fn=_on_progress)
