# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Harper grammar: persistent harper-ls LSP client for in-process linting.

Runs in-process on LibreOffice's grammar / linguistic threads (not the warm venv
worker / trusted RPC path used by LanguageTool and Vale).
"""

from __future__ import annotations

import enum
import logging
import os
import queue
import re
import subprocess
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any, BinaryIO, cast

if TYPE_CHECKING:
    from collections.abc import Callable

from plugin.framework.managed_subprocess import ManagedSubprocess
from plugin.framework.errors import best_effort
from plugin.scripting.sandbox import wrap_command_for_sandbox

from plugin.contrib.lsp import json_rpc_framing
from plugin.contrib.lsp.position_codec import ClientPosition, PositionCodec
from plugin.writer.locale import grammar_settings
from plugin.writer.locale.harper_binary import emit_progress, get_harper_binary
from plugin.framework.grammar_rule_ids import HARPER_RULE_PREFIX, make_rule_identifier

log = logging.getLogger("writeragent.grammar")

_JSONRPC = "2.0"

_LINT_BUDGET_SEC = 15.0
_DIALOG_LINT_BUDGET_SEC: float = 2.0
_INIT_BUDGET_SEC = 5.0
_UI_STATE_LOCK_WAIT_SEC: float = 0.05
# Cancel is polled on this cadence so linguistic wait timeouts release
# runtime.lock instead of blocking the next Harper caller for 15 seconds.
_HARPER_CANCEL_POLL_SEC = 0.25
_HARPER_CANCEL_JOIN_SEC = 1.0
# close() unblocks read(); the reader should exit well inside this.
_STDOUT_READER_JOIN_SEC = 0.5

_LSP_POSITION_CODEC = PositionCodec("utf-16")


def _lsp_notification(method: str, params: dict[str, Any] | None) -> dict[str, Any]:
    return {"jsonrpc": _JSONRPC, "method": method, "params": params}


def _lsp_request(req_id: int, method: str, params: dict[str, Any] | None) -> dict[str, Any]:
    return {"jsonrpc": _JSONRPC, "id": req_id, "method": method, "params": params}


def _lsp_response(req_id: int, result: Any) -> dict[str, Any]:
    return {"jsonrpc": _JSONRPC, "id": req_id, "result": result}


def _deadline_remaining(deadline: float) -> float:
    return max(0.0, deadline - time.monotonic())


# XCU Locales order: en-US en-GB en-AU en-CA en-IN
HARPER_DIALECTS: dict[str, str] = {
    "en-US": "American",
    "en-GB": "British",
    "en-AU": "Australian",
    "en-CA": "Canadian",
    "en-IN": "Indian",
}

HARPER_LOCALE_TAGS: tuple[str, ...] = tuple(HARPER_DIALECTS)


def _harper_lsp_settings(bcp47: str, user_config_dir: str) -> dict[str, Any]:
    dialect = HARPER_DIALECTS.get(bcp47, "American")
    settings: dict[str, Any] = {"dialect": dialect}
    if user_config_dir:
        settings["userDictPath"] = str(Path(user_config_dir) / "harper-dictionary.txt")
    return {"harper-ls": settings}


_HARPER_FAIL_COOLDOWN_SEC = 30.0


class HarperRuntimeState(enum.Enum):
    # Enum members cannot take PEP 526 annotations (typing spec / ty / basedpyright).
    IDLE = "idle"
    RESOLVING = "resolving"
    READY = "ready"
    FAILED = "failed"


class HarperLockTimeoutError(TimeoutError):
    """Acquiring runtime.lock exceeded the available deadline or budget."""


class HarperRuntime:
    """Encapsulates in-process harper-ls client state and concurrency controls.

    One LSP client per binary path. Lock serializes brief client critical
    sections (cache/state + ``client.lint`` on the worker). Never hold it
    across ``process_events_to_idle`` or a long LSP wait on the linguistic
    thread: PE2I can re-enter ``doProofreading`` and a non-reentrant mutex
    would deadlock.
    """

    lock: threading.Lock
    clients: dict[str, HarperLSClient]
    state: HarperRuntimeState
    failed_at: float
    linting_client: HarperLSClient | None

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.clients = {}
        self.state = HarperRuntimeState.IDLE
        self.failed_at = 0.0
        self.linting_client = None

    def shutdown(self) -> None:
        """Close every cached harper-ls client. Safe from tests and extension teardown."""
        with self.lock:
            clients = list(self.clients.values())
            self.clients.clear()
            self.state = HarperRuntimeState.IDLE
            self.failed_at = 0.0
        for client in clients:
            client.close()

    def close_clients(self) -> None:
        """Close and discard all cached clients so ensure spawns a fresh process."""
        if not self._acquire_state_lock():
            log.debug("[harper] State lock busy; skipped closing clients on main thread")
            return
        try:
            clients = list(self.clients.values())
            self.clients.clear()
        finally:
            self.lock.release()
        for client in clients:
            client.close()

    def _acquire_state_lock(self) -> bool:
        # The lint worker holds this lock across client.lint (and _initialize / close).
        # On the UI thread an untimed wait sits under SolarMutex after wait_while_pumping
        # has returned, so the spelling dialog would freeze until the walk released.
        # A timed acquire on the main thread keeps the dialog responsive; a missed
        # acquire writes nothing and the holder keeps the state it already set.
        from plugin.framework.queue_executor import main

        if main.is_current():
            return self.lock.acquire(timeout=_UI_STATE_LOCK_WAIT_SEC)
        self.lock.acquire()
        return True

    def is_ready(self) -> bool:
        """True when harper-ls is in-process and the runtime state is READY."""
        if not self._acquire_state_lock():
            return False
        try:
            return self.state is HarperRuntimeState.READY and any(c.is_alive() for c in self.clients.values())
        finally:
            self.lock.release()

    def mark_idle(self) -> None:
        if not self._acquire_state_lock():
            log.debug("[harper] State lock busy; skipped setting IDLE on main thread")
            return
        try:
            self.state = HarperRuntimeState.IDLE
        finally:
            self.lock.release()

    def install_client(
        self,
        binary_path: str,
        user_config_dir: str,
        bcp47: str = "en-US",
        heartbeat_fn: Callable[[dict[str, str]], None] | None = None,
    ) -> HarperLSClient:
        """Construct a client outside self.lock and compare-and-swap under the lock.

        HarperLSClient does Popen plus LSP initialize (up to _INIT_BUDGET_SEC).
        Running construction outside the lock prevents blocking callers, while
        the compare-and-swap under the lock keeps two concurrent ensure_body calls
        from both leaving a live process installed.
        """
        built = HarperLSClient(
            binary_path,
            user_config_dir=user_config_dir,
            bcp47=bcp47,
            heartbeat_fn=heartbeat_fn,
        )
        with self.lock:
            current = self.clients.get(binary_path)
            if current is not None and current is not built and current.is_alive():
                built.close()
                if heartbeat_fn is not None:
                    current._heartbeat_fn = heartbeat_fn
                return current
            if current is not None and current is not built:
                current.close()
            self.clients[binary_path] = built
            return built

    def ensure_body(self, user_config_dir: str, bcp47: str = "en-US") -> None:
        from plugin.writer.locale.grammar_obs import get_grammar_status

        try:
            def _on_progress(payload: dict[str, str]) -> None:
                message = str(payload.get("message") or "").strip()
                if message:
                    get_grammar_status().request("Harper", result=message)

            harper_bin = get_harper_binary(user_config_dir, heartbeat_fn=_on_progress)
            dead_to_close: HarperLSClient | None = None
            with self.lock:
                cached = self.clients.get(harper_bin)
                if cached is not None and cached.is_alive():
                    self.state = HarperRuntimeState.READY
                    client = cached
                else:
                    if cached is not None:
                        # Drop dead client so ensure does not keep returning an exited process and stay silent until restart.
                        del self.clients[harper_bin]
                        dead_to_close = cached
                    client = None
            if dead_to_close is not None:
                dead_to_close.close()
            if client is None:
                client = self.install_client(harper_bin, user_config_dir, bcp47, heartbeat_fn=_on_progress)
                if not client.is_alive():
                    raise RuntimeError("harper-ls process not running after start")
                with self.lock:
                    self.state = HarperRuntimeState.READY
            get_grammar_status().request("Harper", result="Harper ready")
            _schedule_proofread_again()
        except Exception as exc:
            log.exception("[harper] Background ensure failed")
            get_grammar_status().failed("Harper", result=str(exc))
            # An ensure failure always sets FAILED with failed_at. Guarding
            # the transition on "state is not RESOLVING" never records it:
            # harper_ensure_ready_async sets RESOLVING before submitting the
            # job, so ensure_async refuses while RESOLVING.
            with self.lock:
                self.state = HarperRuntimeState.FAILED
                self.failed_at = time.monotonic()

    def ensure_async(self, user_config_dir: str, bcp47: str = "en-US") -> bool:
        from plugin.framework.worker_pool import run_in_background

        if not self._acquire_state_lock():
            log.debug("[harper] State lock busy; skipped ensure_async on main thread")
            return False
        try:
            if self.state is HarperRuntimeState.RESOLVING:
                return False
            if self.state is HarperRuntimeState.READY and any(c.is_alive() for c in self.clients.values()):
                return False
            if self.state is HarperRuntimeState.FAILED:
                if time.monotonic() - self.failed_at < _HARPER_FAIL_COOLDOWN_SEC:
                    return False
            self.state = HarperRuntimeState.RESOLVING
            try:
                run_in_background(
                    self.ensure_body,
                    user_config_dir,
                    bcp47,
                    name="harper-ensure-ready",
                    dedicated=True,
                )
            except Exception as exc:
                log.exception("[harper] Could not submit ensure job: %s", exc)
                # The rollback is this same hold; a second timed acquire that fails would leave RESOLVING with no job, and a lint holder does not clear that bit.
                if self.state is HarperRuntimeState.RESOLVING:
                    self.state = HarperRuntimeState.IDLE
                return False
            return True
        finally:
            self.lock.release()

    def _lint_with_client(
        self,
        client: HarperLSClient,
        text: str,
        bcp47: str,
        *,
        heartbeat_fn: Callable[[dict[str, str]], None] | None = None,
        deadline: float | None = None,
        cancel_event: threading.Event | None = None,
    ) -> dict[str, Any]:
        """Caller holds ``self.lock``."""
        started = time.monotonic()
        lint_text = normalize_spaces_1to1(text)
        error_count = 0
        try:
            results = client.lint(
                lint_text,
                bcp47=bcp47,
                heartbeat_fn=heartbeat_fn,
                deadline=deadline,
                cancel_event=cancel_event,
            )
            # Harper diagnostics map against `lint_text`, the text actually
            # sent to Harper. Mapping against the original `text` misaligns
            # offsets and line numbers.
            out = _diagnostics_to_errors(text, results, lint_text)
            error_count = len(out["errors"])
            return out
        finally:
            # Wall time of lint + normalize into errors (worker or caller thread).
            elapsed_ms = int((time.monotonic() - started) * 1000)
            if elapsed_ms > 500:
                log.warning(
                    "[harper] slow result elapsed_ms=%s text_len=%s errors=%s cache=%s",
                    elapsed_ms,
                    len(text),
                    error_count,
                    "miss",
                )

    def lint_now(
        self,
        text: str,
        bcp47: str = "en-US",
        *,
        deadline: float | None = None,
        cancel_event: threading.Event | None = None,
    ) -> tuple[dict[str, Any] | None, str | None]:
        """Lint text under self.lock; does not construct clients."""
        if deadline is not None:
            remaining = _deadline_remaining(deadline)
            if remaining <= 0:
                raise HarperLockTimeoutError("Harper LSP lock acquire timeout")
            acquired = self.lock.acquire(timeout=remaining)
        else:
            acquired = self.lock.acquire(timeout=_LINT_BUDGET_SEC)
        if not acquired:
            raise HarperLockTimeoutError("Harper LSP lock acquire timeout")
        try:
            client: HarperLSClient | None = None
            for c in self.clients.values():
                if c.is_alive():
                    client = c
                    break
            if client is None:
                if self.state is HarperRuntimeState.READY:
                    log.error("[harper] lint missed: state READY but harper-ls process is dead")
                    self.state = HarperRuntimeState.IDLE
                    return None, "dead_client"
                return None, f"state_{self.state.value}"
            self.state = HarperRuntimeState.READY
            self.linting_client = client
            try:
                res = self._lint_with_client(
                    client,
                    text,
                    bcp47=bcp47,
                    deadline=deadline,
                    cancel_event=cancel_event,
                )
                return res, None
            finally:
                self.linting_client = None
        finally:
            self.lock.release()

    def abort_lint(self) -> None:
        """Close the in-flight lint client without taking ``self.lock``.

        ``harper_try_lint`` calls this when the lint worker is still not
        done after cancel and ``handle.join``. A worker blocked in
        ``HarperLSClient._write`` holds ``self.lock``. ``cancel_event`` is
        polled in ``_read``, not during that write, so cancel plus join
        leaves the lock held and later Harper calls deadlock. Closing the
        client aborts the blocked pipe I/O. ``lint_now`` clears
        ``linting_client`` in its own ``finally`` after the write unblocks.
        This method must not acquire ``self.lock`` (it would wait on that
        same worker) and must not assign ``linting_client`` (a newer lint
        may already have stored another client).
        """
        client = self.linting_client
        if client is not None:
            client.close()


runtime = HarperRuntime()


class HarperLSClient:
    binary_path: str
    user_config_dir: str
    _bcp47: str
    _heartbeat_fn: Callable[[dict[str, str]], None] | None
    _lsp_settings: dict[str, Any]
    request_id: int
    uri: str
    _doc_version: int
    _doc_opened: bool
    _lint_cancel: threading.Event | None

    def __init__(self, binary_path: str, user_config_dir: str = "", bcp47: str = "en-US", *, heartbeat_fn: Callable[[dict[str, str]], None] | None = None) -> None:
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
        self.stdout_queue: queue.Queue[dict[str, Any] | None] = queue.Queue()
        self.stdout_thread: threading.Thread | None = None
        self._lint_cancel = None
        self._initialize()

    def _join_stdout_thread(self, thread: threading.Thread | None) -> None:
        """Wait out the previous reader before publishing a new queue.

        ``_read_loop`` does not put a sentinel on ``stdout_queue`` in ``finally``
        because ``_initialize`` may replace the queue while the prior daemon exits;
        a stray None would make the next lint treat EOF as an empty diagnostic list.
        """
        if thread is None or not thread.is_alive():
            return
        if thread is threading.current_thread():
            return
        thread.join(timeout=_STDOUT_READER_JOIN_SEC)

    def _initialize(self) -> None:
        try:
            old_thread = self.stdout_thread
            if self.proc is not None:
                self.close()
            # Fence the old reader before the swap. It also writes only to the
            # queue captured when it started, so a late finally cannot poison
            # the replacement if this join times out.
            self._join_stdout_thread(old_thread)
            self._doc_version = 0
            self._doc_opened = False
            out_queue: queue.Queue[dict[str, Any] | None] = queue.Queue()
            self.stdout_queue = out_queue
            emit_progress(self._heartbeat_fn, "Starting harper-ls…")
            # Explanations for non-default flags:
            # - inherit_env: Popen env=None and the sandbox wrapper sees env=None, so Flatpak does not pass --clear-env (host session, as wrap_command_for_sandbox documents for harper).
            # - new_session=False: child is not a session leader; close signals that process with terminate / kill.
            # - stderr=DEVNULL: no drain thread.
            # - register=False: shutdown_all must not kill_tree this child.
            # - log_start_failure=False: the handler below must see the original exception.
            managed = ManagedSubprocess(
                [self.binary_path, "--stdio"],
                command_wrapper=wrap_command_for_sandbox,
                inherit_env=True,
                new_session=False,
                stderr=subprocess.DEVNULL,
                register=False,
                log_start_failure=False,
                label="harper-ls",
            )
            managed.start()
            assert managed.popen is not None
            self.proc = managed.popen
            self.stdout_thread = threading.Thread(target=self._read_loop, args=(out_queue, managed.popen.stdout), daemon=True)  # nosemgrep: raw-uno-thread-ban
            self.stdout_thread.start()

            deadline = time.monotonic() + _INIT_BUDGET_SEC
            emit_progress(self._heartbeat_fn, "Initializing Harper LSP…")
            self._send_request(
                "initialize",
                {
                    "processId": os.getpid(),
                    "rootUri": "file:///tmp",
                    "capabilities": {
                        "textDocument": {
                            "publishDiagnostics": {"relatedInformation": False},
                            "codeAction": {
                                "dynamicRegistration": False,
                                "codeActionLiteralSupport": {"codeActionKind": {"valueSet": ["quickfix"]}},
                            },
                        },
                    },
                },
                deadline=deadline,
            )
            self._write(_lsp_notification("initialized", {}))
        except Exception as e:
            self.close()
            log.exception("[harper] Failed to start/initialize harper-ls")
            raise RuntimeError(f"Failed to start/initialize harper-ls: {e}") from e

    def _read_loop(self, out_queue: queue.Queue[dict[str, Any] | None], stdout: Any) -> None:
        """Read LSP frames from the stream this thread was started with.

        Binding the queue and stream here keeps a superseded reader from
        pushing EOF onto the new client or consuming frames from the replacement.
        """
        try:
            while stdout:
                msg = json_rpc_framing.read_frame(cast("BinaryIO", stdout))
                if msg is None:
                    break
                out_queue.put(msg)
        except Exception:
            log.exception("[harper] LSP reader failed")
        finally:
            out_queue.put(None)

    def is_alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def _write(self, payload: dict[str, Any]) -> None:
        if not self.proc or self.proc.stdin is None:
            raise RuntimeError("harper-ls process not running")
        json_rpc_framing.write_frame(cast("BinaryIO", self.proc.stdin), payload)

    def _read(self, deadline: float) -> dict[str, Any] | None:
        if not self.proc:
            raise RuntimeError("harper-ls process not running")
        # Snapshot the event for this lint. ``None`` keeps the old one-shot
        # get so a plain timeout still fails on the first empty read.
        cancel = self._lint_cancel
        while True:
            if cancel is not None and cancel.is_set():
                raise TimeoutError("Harper LSP operation cancelled")
            remaining = _deadline_remaining(deadline)
            if remaining <= 0:
                raise TimeoutError("Harper LSP operation timed out")
            timeout = remaining if cancel is None else min(remaining, _HARPER_CANCEL_POLL_SEC)
            try:
                return self.stdout_queue.get(timeout=timeout)
            except queue.Empty:
                if cancel is None or _deadline_remaining(deadline) <= 0:
                    raise TimeoutError("Harper LSP operation timed out")

    def _read_and_handle(self, deadline: float) -> dict[str, Any] | None:
        while True:
            msg = self._read(deadline)
            if not msg:
                return None
            if "id" not in msg or "method" not in msg:
                return msg
            if msg["method"] == "workspace/configuration":
                self._write(_lsp_response(msg["id"], [self._lsp_settings]))
            else:
                self._write(_lsp_response(msg["id"], None))

    def _send_request(self, method: str, params: dict[str, Any], *, deadline: float) -> dict[str, Any] | None:
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

    def _collect_diagnostics(self, version: int, deadline: float) -> list[Any]:
        """Wait for ``publishDiagnostics`` for this document version.

        ``_read_loop`` enqueues ``None`` on stdout EOF and on a reader
        crash. Treating that falsy result as the end of diagnostics and
        returning ``[]`` makes ``lint`` look successful, and the fast
        path caches the sentence with no errors so Writer does not walk
        it again. A real publish with ``diagnostics: []`` is still a
        clean sentence. The sentinel, a dead process, and a timeout are
        not.
        """
        while _deadline_remaining(deadline) > 0:
            # If harper-ls dies with an empty queue before publishDiagnostics,
            # raise instead of returning an empty list that looks like a clean lint.
            if not self.is_alive() and self.stdout_queue.empty():
                raise RuntimeError("harper-ls process died before publishDiagnostics")
            msg = self._read_and_handle(deadline)
            if msg is None:
                raise RuntimeError("harper-ls closed before publishDiagnostics")

            if msg.get("method") == "textDocument/publishDiagnostics":
                params = msg.get("params", {})
                if params.get("uri") == self.uri:
                    msg_version = params.get("version")
                    if msg_version is not None and msg_version < version:
                        continue
                    diagnostics = params.get("diagnostics", [])
                    if not isinstance(diagnostics, list):
                        raise RuntimeError("harper-ls publishDiagnostics payload was not a list")
                    return diagnostics
        raise TimeoutError("Harper LSP operation timed out")

    def _suggestions_for_diagnostic(self, diag: dict[str, Any], deadline: float) -> list[str]:
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

    def _drain_stdout_queue(self) -> None:
        # Rotating the document URI is the primary protection against stale
        # diagnostic bleed; late responses may still arrive on stdout after this
        # drain. This drain is best-effort cleanup of already-enqueued frames.
        try:
            while True:
                self.stdout_queue.get_nowait()
        except queue.Empty:
            pass

    def _reset_document_uri(self) -> None:
        # Rotating the document URI prevents late diagnostic responses for the
        # timed-out sentence from matching future lints. Do not send didClose:
        # if the server's stdin pipe is full, a blocking _write under runtime.lock
        # would wedge the caller (and the main thread in dialog lint).
        self.uri = f"file:///tmp/writeragent_harper_lint_{time.time_ns()}.txt"
        self._doc_opened = False
        self._doc_version = 0

    def lint(
        self,
        text: str,
        bcp47: str = "en-US",
        *,
        heartbeat_fn: Callable[[dict[str, str]], None] | None = None,
        deadline: float | None = None,
        cancel_event: threading.Event | None = None,
    ) -> list[Any]:
        # One lint at a time: venv worker IPC is serialized; grammar uses a single drain thread for Harper.
        self._heartbeat_fn = heartbeat_fn
        if deadline is None:
            deadline = time.monotonic() + _LINT_BUDGET_SEC
        previous_cancel = self._lint_cancel
        self._lint_cancel = cancel_event
        try:
            if cancel_event is not None and cancel_event.is_set():
                raise TimeoutError("Harper LSP operation cancelled")
            if not self.is_alive():
                self._initialize()

            emit_progress(self._heartbeat_fn, "Linting…")

            self._apply_bcp47(bcp47)
            self._doc_version += 1
            version = self._doc_version

            try:
                self._sync_document(text, version)
                diagnostics = self._collect_diagnostics(version, deadline)
                return [{"diagnostic": diag, "suggestions": self._suggestions_for_diagnostic(diag, deadline)} for diag in diagnostics]
            except TimeoutError:
                # A main-thread dialog budget expiration or cancellation leaves the
                # server process healthy. Re-raise so the caller can decide whether
                # to keep or close the client. Because harper-ls publishes diagnostics
                # without version, a timed-out lint drains pending messages and rotates
                # the document URI so stale diagnostic responses cannot bleed into a later lint.
                self._drain_stdout_queue()
                self._reset_document_uri()
                raise
            except Exception:
                log.exception("[harper] Exception during linting, closing client")
                self.close()
                raise
        finally:
            self._lint_cancel = previous_cancel

    def close(self) -> None:
        """Tear down harper-ls without blocking on stuck pipes or the reader.

        The previous path wrote LSP shutdown/exit then waited. On Windows a
        hung harper-ls fills the stdin pipe; ``stdin.write`` blocks forever
        and xdist workers never exit (CI sat ~19 min after pytest 99%).
        Closing **stdout** while ``_read_loop`` is blocked in ``readline`` also
        deadlocks on Windows (Release Verify fixture timeout via
        ``shutdown_harper_runtime`` → ``close``). Close stdin only, terminate/kill
        so process death unblocks the reader, join the stdout thread with a short
        timeout, then close stdout. ManagedSubprocess.close() and kill_tree() are
        a different shutdown and are not used here.
        """
        proc = self.proc
        self.proc = None
        # __new__ without __init__ (unit tests) may omit stdout_thread; real clients set it in __init__.
        thread = getattr(self, "stdout_thread", None)
        self.stdout_thread = None
        self._doc_opened = False
        if proc is None:
            self._join_stdout_thread(thread)
            return
        if proc.stdin is not None:
            best_effort(proc.stdin.close, "harper stdin close failed")
        if proc.poll() is None:
            def _kill() -> None:
                proc.kill()
                proc.wait(timeout=0.5)

            try:
                proc.terminate()
                proc.wait(timeout=0.5)
            except Exception:
                best_effort(_kill, "[harper] kill after terminate failed")
        # Process death unblocks readline; join before closing stdout.
        self._join_stdout_thread(thread)
        if proc.stdout is not None:
            best_effort(proc.stdout.close, "harper stdout close failed")


def lsp_range_to_offset(text: str, line: int, character: int) -> int:
    """Convert LSP 0-indexed line/character (UTF-16 code units) to a Python string offset."""
    # Line endings are (\r\n|\r|\n) only. str.splitlines() also
    # splits on Unicode separators (\u2028, \x0b, \x0c, \x1c-\x1e,
    # \x85), so character offsets diverge from the LSP server. LSP
    # ends lines only on \r\n, \r, and \n.
    if "\n" not in text and "\r" not in text:
        lines = [text] if text else []
    else:
        parts = re.split(r"(\r\n|\r|\n)", text)
        lines = [parts[i] + parts[i + 1] for i in range(0, len(parts) - 1, 2)]
        if parts[-1]:
            lines.append(parts[-1])
    if line >= len(lines):
        return len(text)
    pos = _LSP_POSITION_CODEC.position_from_client_units(lines, ClientPosition(line=line, character=character))
    offset = sum(len(lines[i]) for i in range(pos.line))
    return min(offset + pos.character, len(text))


def shutdown_harper_runtime() -> None:
    """Close every cached harper-ls client. Safe from tests and extension teardown."""
    runtime.shutdown()


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
        from plugin.framework.queue_executor import main

        main.post(_broadcast_proofread_again)
    except Exception as e:
        log.debug("[harper] Could not schedule PROOFREAD_AGAIN: %s", e)


def harper_ensure_ready_async(user_config_dir: str, bcp47: str = "en-US") -> bool:
    """Start at most one download/start job. Returns True if a job was submitted."""
    return runtime.ensure_async(user_config_dir, bcp47)


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
    from plugin.framework.config import user_config_dir as get_ucd

    if grammar_settings.provider() != "harper":
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


def harper_try_lint(text: str, user_config_dir: str, bcp47: str = "en-US", *, ctx: Any = None) -> dict[str, Any] | None:
    """Lint now if harper-ls is already in-process; else kick one ensure and return None.

    Never downloads or ``Popen``s on the caller thread (UNO ``doProofreading``).
    A ``None`` return is never silent: failures log ERROR, not-ready walks emit obs.

    When ``ctx`` is set (``doProofreading``), the blocking LSP wait runs on a
    dedicated worker and the caller uses ``wait_while_pumping`` so typing stays
    alive. Writer runs ``doProofreading`` on a linguistic worker (``Dummy-*``),
    not VCL: the helper posts PE2I to the main thread instead of pumping on
    this stack (in-loop PE2I there was a thread-violation dialog). Missing ``ctx`` falls
    back to a blocking wait (no pump).
    """
    from plugin.writer.locale.grammar_obs import grammar_obs

    if not user_config_dir:
        log.error("[harper] lint skipped: empty user config dir")
        return None

    try:
        if ctx is None:
            res, none_reason = runtime.lint_now(text, bcp47=bcp47)
        else:
            box: dict[str, Any] = {}
            done = threading.Event()
            cancel_event = threading.Event()
            from plugin.framework.queue_executor import main

            # VCL proofreading has no Stop, so the dialog budget is 2 s; Dummy-* stays 15 s.
            budget = _DIALOG_LINT_BUDGET_SEC if main.is_current() else _LINT_BUDGET_SEC
            # One clock for the waiter and the worker. A late worker must not start
            # a fresh budget after the caller has already timed out.
            deadline = time.monotonic() + budget

            def _worker() -> None:
                try:
                    box["result"], box["none_reason"] = runtime.lint_now(
                        text,
                        bcp47=bcp47,
                        deadline=deadline,
                        cancel_event=cancel_event,
                    )
                except Exception as exc:
                    box["exc"] = exc
                finally:
                    done.set()

            from plugin.framework.worker_pool import run_in_background

            handle = run_in_background(_worker, name="harper-lint-wait", dedicated=True)
            from plugin.framework.uno_context import wait_while_pumping

            wait_while_pumping(done, ctx, timeout=_deadline_remaining(deadline))
            if not done.is_set():
                # Cancel unblocks the worker's queue poll so join finishes within
                # _HARPER_CANCEL_JOIN_SEC instead of holding runtime.lock for ~30s.
                cancel_event.set()
                handle.join(timeout=_HARPER_CANCEL_JOIN_SEC)
                if not done.is_set():
                    runtime.abort_lint()
            if "exc" in box:
                raise box["exc"]
            if not done.is_set():
                raise TimeoutError("Harper LSP operation timed out")
            res = box.get("result")
            none_reason = box.get("none_reason")
    except HarperLockTimeoutError:
        grammar_obs("harper_try_lint_none", reason="lock_timeout", ensure_submitted=False)
        return None
    except Exception as exc:
        from plugin.framework.queue_executor import main

        if isinstance(exc, TimeoutError) and main.is_current():
            # Main-thread proofreading bounds dialog responsiveness to a short budget.
            # When this budget expires, return an empty walk without cycling the client
            # (unless abort_lint already closed it).
            log.debug("[harper] main-thread dialog lint timed out; returning empty walk")
            grammar_obs("harper_try_lint_none", reason="dialog_budget", ensure_submitted=False)
            return None

        if isinstance(exc, (TimeoutError, OSError)):
            # Off-main timeout or broken pipe: close the stuck client so background
            # ensure starts a fresh server rather than reusing a wedged process.
            runtime.close_clients()

        # restart=False: do not Popen on the linguistic thread. The
        # walk returns empty; background ensure restarts harper-ls.
        # Leave the state IDLE. FAILED starts the 30s cooldown, so the
        # following harper_ensure_ready_async call no-ops instead of
        # restarting.
        log.exception("[harper] lint failed on ready client; empty aErrors this walk")
        runtime.mark_idle()
        harper_ensure_ready_async(user_config_dir, bcp47)
        return None

    if res is None:
        submitted = harper_ensure_ready_async(user_config_dir, bcp47)
        if none_reason != "dead_client":
            grammar_obs("harper_try_lint_none", reason=none_reason or "unknown", ensure_submitted=submitted)
        return None
    return res


def normalize_spaces_1to1(text: str) -> str:
    """Normalize non-standard Unicode spaces (NBSP, CJK spaces, etc.) to ASCII ' '.

    Preserves exact 1:1 character length and offsets for LSP coordinate mapping.
    Leaves newlines ('\\n', '\\r') untouched.
    """
    if not text:
        return ""
    return "".join(" " if ch.isspace() and ch not in "\r\n" else ch for ch in text)


def _diagnostics_to_errors(text: str, results: list[Any], lint_text: str) -> dict[str, Any]:
    """Map LSP ranges against *lint_text* (what Harper saw); slice "wrong" from *text*.

    normalize_spaces_1to1 keeps offsets 1:1, so the original text shows the user's characters.
    """
    errors = []
    for item in results:
        diag = item["diagnostic"]
        suggestions = item["suggestions"]

        msg = diag.get("message", "")
        code = diag.get("code", "Grammar")

        diag_range = diag.get("range", {})
        start_pos = diag_range.get("start", {})
        end_pos = diag_range.get("end", {})

        start_offset = lsp_range_to_offset(lint_text, start_pos.get("line", 0), start_pos.get("character", 0))
        end_offset = lsp_range_to_offset(lint_text, end_pos.get("line", 0), end_pos.get("character", 0))
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
