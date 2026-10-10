#!/usr/bin/env python3
# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
"""Long-lived venv worker: length-prefixed pickle requests on stdin, responses on stdout.

Each execute request runs user code in LocalPythonExecutor. Without ``session_id`` the
namespace is fresh per call; with ``session_id`` the same executor is reused (shared kernel).
"""
from __future__ import annotations

import logging
import os
import sys
import traceback
from typing import Any

# Standalone entry (venv python worker_harness.py): repo root must be on sys.path for plugin.* imports.
_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.abspath(os.path.join(_SCRIPT_DIR, "..", "..", ".."))
if _SCRIPT_DIR in sys.path:
    sys.path.remove(_SCRIPT_DIR)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from plugin.framework.uno_bootstrap import register_alias_importer
register_alias_importer()

from plugin.scripting.ipc import (
    DEFAULT_MAX_PAYLOAD_BYTES,
    EXEC_STARTED,
    IpcFrameError,
    IpcPayloadSizeError,
    UserStopped,
    claim_ipc_channel,
    read_pickle_frame,
    write_pickle_frame,
)
from plugin.scripting.venv.venv_sandbox import reset_sandbox_session, run_sandboxed_code

log = logging.getLogger("worker_harness")


def _error_response(exc: BaseException | Exception) -> dict[str, Any]:
    return {
        "status": "error",
        "message": str(exc),
        "traceback": traceback.format_exc(),
    }


def _clean_str(val: Any) -> str | None:
    return val if isinstance(val, str) and val.strip() else None


def _handle_trusted_action(
    request: dict[str, Any],
    data: dict[str, Any],
    *,
    stdout: Any | None = None,
) -> dict[str, Any] | None:
    """Dispatch run_trusted_action via the declarative registry."""
    from plugin.scripting.trusted_action_registry import get_trusted_action_wiring
    from plugin.scripting.venv.worker_heartbeat import HeartbeatEmitter, write_result_frame

    domain = str(data.get("domain") or "")
    use_heartbeat = False
    # Resolve wiring inside the try. A bad domain or a contract error must be
    # an error frame, not a dead worker.
    try:
        wiring = get_trusted_action_wiring(domain)
        if wiring is None:
            resp = {"status": "error", "message": f"Unknown trusted action domain: {domain}"}
        else:
            use_heartbeat = bool(request.get("allow_heartbeat")) and wiring.supports_heartbeat and stdout is not None
            heartbeat_fn = None
            if use_heartbeat:
                emitter = HeartbeatEmitter(stdout)
                heartbeat_fn = emitter.emit
            result = wiring.dispatch(data, heartbeat_fn=heartbeat_fn)
            resp = {"status": "ok", "result": result}
    except Exception as exc:
        use_heartbeat = bool(request.get("allow_heartbeat")) and stdout is not None
        resp = _error_response(exc)

    if use_heartbeat and stdout is not None:
        req_id = str(request.get("id", ""))
        payload = {"id": req_id, **resp}
        # Packing fails before any byte hits stdout, so the pipe is still in
        # sync. An oversized result is a small error frame. Letting
        # IpcFrameError escape after EXEC_STARTED ends the worker with no
        # terminal frame, and the host restarts it.
        try:
            write_result_frame(stdout, payload)
        except IpcPayloadSizeError as exc:
            err_payload = {
                "id": req_id,
                "status": "error",
                "message": f"Result exceeds maximum payload size: {exc}",
            }
            write_result_frame(stdout, err_payload)
        return None

    return resp


def _action_reset_session(request: dict[str, Any], *, stdout: Any | None = None) -> dict[str, Any] | None:
    session_id = _clean_str(request.get("session_id"))
    if not session_id:
        return {"status": "error", "message": "No session_id provided."}
    return reset_sandbox_session(session_id)


def _action_ppt_master_turn(request: dict[str, Any], *, stdout: Any | None = None) -> dict[str, Any] | None:
    from plugin.ppt_master.venv.runner import run_turn

    data = request.get("data")
    if not isinstance(data, dict):
        return {"status": "error", "message": "ppt_master_turn requires data dict."}
    try:
        result = run_turn(data)
        return {"status": "ok", "result": result}
    except Exception as exc:
        return _error_response(exc)


def _action_run_trusted_action(request: dict[str, Any], *, stdout: Any | None = None) -> dict[str, Any] | None:
    data = request.get("data")
    if not isinstance(data, dict):
        return {"status": "error", "message": "run_trusted_action requires data dict."}
    return _handle_trusted_action(request, data, stdout=stdout)


def _action_execute(request: dict[str, Any], *, stdout: Any | None = None) -> dict[str, Any] | None:
    code = request.get("code")
    if not isinstance(code, str) or not code.strip():
        return {"status": "error", "message": "No code provided."}
    bindings = request.get("bindings")
    bindings_dict = bindings if isinstance(bindings, dict) else None
    init_script = request.get("init_script")
    return run_sandboxed_code(
        code,
        request.get("data"),
        bindings=bindings_dict,
        session_id=_clean_str(request.get("session_id")),
        init_script=init_script if isinstance(init_script, str) else None,
        init_session_id=_clean_str(request.get("init_session_id")),
        init_script_hash=_clean_str(request.get("init_script_hash")),
        timeout_sec=request.get("timeout_sec"),
    )


_ACTION_HANDLERS = {
    "reset_session": _action_reset_session,
    "ppt_master_turn": _action_ppt_master_turn,
    "run_trusted_action": _action_run_trusted_action,
}


def _handle_request(request: dict[str, Any], *, stdout: Any | None = None) -> dict[str, Any] | None:
    action = request.get("action")
    # Omitted, blank, and ``"execute"`` run user code. Any other action must
    # be in _ACTION_HANDLERS. A typo that also carries ``code`` must not run.
    if action is None or action == "" or action == "execute":
        return _action_execute(request, stdout=stdout)
    if not isinstance(action, str) or action not in _ACTION_HANDLERS:
        return {"status": "error", "message": f"Unknown action: {action!r}"}
    return _ACTION_HANDLERS[action](request, stdout=stdout)


def _init_logging() -> None:
    log_path = os.environ.get("WRITERAGENT_DEBUG_LOG_PATH")
    if not log_path:
        return
    try:
        handler = logging.FileHandler(log_path, encoding="utf-8")
        formatter = logging.Formatter("%(asctime)s | %(name)s[Worker] | %(levelname)s | %(message)s")
        handler.setFormatter(formatter)
        root = logging.getLogger()
        root.setLevel(logging.DEBUG)
        root.addHandler(handler)
    except Exception as exc:
        sys.stderr.write(f"Failed to initialize worker logging: {exc}\n")


def _die_with_parent() -> None:
    """Kill this child if the LibreOffice process dies while we are in native code.

    stdin EOF already exits an idle harness. A C extension that never returns
    to ``read`` used to keep the venv process (and the microphone, or a BLAS
    thread) after soffice quit. Linux ``PR_SET_PDEATHSIG`` is the parent-death
    signal; the ppid check covers the parent dying before prctl runs.
    """
    if sys.platform != "linux":
        return
    try:
        import ctypes

        # Compare against the ppid captured before prctl. After the parent
        # dies, getppid() is the subreaper (systemd --user), which is not 1.
        ppid = os.getppid()
        libc = ctypes.CDLL("libc.so.6", use_errno=True)
        # PR_SET_PDEATHSIG = 1, SIGKILL = 9. Arguments after the signal are unused.
        if libc.prctl(1, 9, 0, 0, 0) != 0:
            return
        if os.getppid() != ppid:
            os.kill(os.getpid(), 9)
    except Exception as exc:
        log.debug("Failed to configure PR_SET_PDEATHSIG: %s", exc)


def main() -> None:
    _die_with_parent()
    _init_logging()
    log.info("Worker process %d starting up with python %s", os.getpid(), sys.version)

    stdin = sys.stdin.buffer
    # claim_ipc_channel() dups fd 1 into a private binary stream and points
    # fd 1 at stderr. A library print() must not land in a protocol frame.
    stdout = claim_ipc_channel()

    while True:
        req_id = ""
        # Read and handle are separate. IpcFrameError subclasses ValueError,
        # so one handler cannot tell them apart. A bad frame on the way in
        # desyncs the stream and ends the loop. A ValueError from unpickling
        # is a bad-request frame, and the next request is still readable.
        try:
            request = read_pickle_frame(
                stdin, require_dict=True, max_payload_bytes=DEFAULT_MAX_PAYLOAD_BYTES
            )
            if request is None:
                break
        except IpcFrameError as e:
            # A bad length prefix leaves unread bytes on the pipe. Writing an
            # error frame here desynchronizes the next request, and the host
            # then kills the shared kernel for every workbook.
            log.warning("IPC frame error reading request: %s", e)
            break
        except ValueError as e:
            log.warning("Invalid pickle request: %s", e)
            bad_req_frame = {"id": "", "status": "error", "message": f"Invalid pickle request: {e}"}
            try:
                write_pickle_frame(stdout, bad_req_frame, max_payload_bytes=DEFAULT_MAX_PAYLOAD_BYTES)
            except Exception:
                break
            continue

        req_id = str(request.get("id", ""))
        log.debug("Received request id=%s action=%s", req_id, request.get("action") or "execute")
        # Flushed before _handle_request. The host retries a death only when
        # it never sees this frame. A crash after DuckDB, or any other
        # in-process side effect that never sent tool_call, must not run twice.
        # A crash while reading the request has not run it, and that retry is
        # how a one-shot child death recovers.
        try:
            write_pickle_frame(
                stdout,
                {"type": EXEC_STARTED, "id": request.get("id")},
                max_payload_bytes=DEFAULT_MAX_PAYLOAD_BYTES,
            )
        except Exception:
            log.exception("Failed to write exec_started for request id=%s", req_id)
            break

        response: dict[str, Any] | None = None
        # UserStopped is a USER_STOPPED frame. A pack-size error or any other
        # exception is a terminal error frame with a traceback. Only a read
        # that desyncs the stream (exchange_tool_call reading a host reply)
        # ends the loop.
        try:
            response = _handle_request(request, stdout=stdout)
            log.debug(
                "Finished request id=%s, response status=%s",
                req_id,
                response.get("status") if response else "none",
            )
        except UserStopped as e:
            # Sandbox returns this as a dict. This is the backstop when Stop
            # is raised outside that path, so the process still writes a
            # terminal frame instead of dying without one.
            response = {
                "status": "error",
                "code": "USER_STOPPED",
                "message": str(e) or "Stopped by user.",
            }
        except IpcFrameError as e:
            if not isinstance(e, IpcPayloadSizeError):
                # Genuine read-side desync (e.g. from exchange_tool_call reading host replies).
                # Do not write an error frame to a corrupted stream.
                log.warning("IPC frame error during request id=%s: %s", req_id, e)
                break
            log.exception("IPC payload size error handling request id=%s", req_id)
            response = _error_response(e)
        except Exception as e:
            log.exception("Exception handling request id=%s", req_id)
            response = _error_response(e)
        except BaseException as e:
            # SystemExit and KeyboardInterrupt are BaseException. After
            # EXEC_STARTED a process death with no terminal frame is not
            # replayed, so every shared session on this worker is dropped.
            # The sandbox returns these as an error dict; this writes that
            # frame when they escape _handle_request, then reads the next request.
            log.exception("BaseException handling request id=%s", req_id)
            response = _error_response(e)

        if response is None:
            continue

        response["id"] = req_id
        try:
            write_pickle_frame(stdout, response, max_payload_bytes=DEFAULT_MAX_PAYLOAD_BYTES)
        except Exception as e:
            err_response = {"id": req_id, "status": "error", "message": f"Pickle serialization failed: {e}"}
            try:
                write_pickle_frame(stdout, err_response, max_payload_bytes=DEFAULT_MAX_PAYLOAD_BYTES)
            except Exception:
                log.exception("Failed to write capped error frame for request id=%s", req_id)
                break


if __name__ == "__main__":
    main()
