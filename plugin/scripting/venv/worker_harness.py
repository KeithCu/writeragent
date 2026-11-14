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

from functools import partial
import logging
import os
import sys
from typing import Any, BinaryIO

# Standalone entry (venv python worker_harness.py): repo root must be on sys.path for plugin.* imports.
_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.abspath(os.path.join(_SCRIPT_DIR, "..", "..", ".."))
if _SCRIPT_DIR in sys.path:
    sys.path.remove(_SCRIPT_DIR)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from plugin.framework.ipc import (
    DEFAULT_MAX_PAYLOAD_BYTES,
    EXEC_STARTED,
    FRAME_HEARTBEAT,
    FRAME_RESULT,
    IpcFrameError,
    IpcPayloadSizeError,
    UserStopped,
    claim_ipc_channel,
    claim_ipc_stdin,
    read_pickle_frame,
    write_child_ipc_frame,
    write_pickle_frame,
)
from plugin.framework.child_runtime import announce_ready, die_with_parent
from plugin.scripting.venv.sandbox_request import SandboxRequest

# claim_ipc_channel runs before other scripting imports when this file is the main entry so stdout is not dup'd onto the IPC pipe before the claim. Library imports (tests) skip this block.
if __name__ == "__main__":
    claim_ipc_channel()

from plugin.framework.uno_bootstrap import register_alias_importer
from plugin.scripting.sandbox import install_alias_authorizer

register_alias_importer()
install_alias_authorizer()

from plugin.scripting.host_tool_call import ROLE_WORKER, set_process_role
from plugin.scripting.trusted_action_registry import get_trusted_action_wiring
from plugin.scripting.venv.results import DEFER_TIMEOUT_FINALIZE, error_result

log = logging.getLogger("worker_harness")
# The sandbox imports this module; sys.modules lookup avoids a second import on every request.
_LPE_MODULE = "plugin.contrib.smolagents.local_python_executor"


def _clean_str(val: Any) -> str | None:
    return val if isinstance(val, str) and val.strip() else None


def emit_heartbeat(stream: BinaryIO | Any, payload: dict[str, Any]) -> None:
    # Heartbeat and result frames share _tool_call_lock with exchange_tool_call.
    # _write_all's header and payload are separate writes; a second thread in
    # a host tool_call would otherwise split the length prefix. The lock is not
    # reentrant: do not call emit on the thread that is inside exchange_tool_call.
    write_child_ipc_frame(
        stream,
        {"frame_type": FRAME_HEARTBEAT, "payload": dict(payload)},
    )


def write_result_frame(stream: BinaryIO | Any, response: dict[str, Any]) -> None:
    """Write the terminal result frame for a heartbeat-enabled worker request."""
    frame = dict(response)
    frame["frame_type"] = FRAME_RESULT
    write_child_ipc_frame(stream, frame)


def _handle_trusted_action(
    request: dict[str, Any],
    data: dict[str, Any],
    *,
    stdout: Any | None = None,
) -> dict[str, Any] | None:
    """Dispatch run_trusted_action via the declarative registry. None means the terminal frame is already written."""
    domain = str(data.get("domain") or "")
    emit_result_frame = bool(request.get("allow_heartbeat")) and stdout is not None
    # Resolve wiring inside the try. A bad domain or a contract error must be
    # an error frame, not a dead worker.
    try:
        wiring = get_trusted_action_wiring(domain)
        if wiring is None:
            resp = error_result(message=f"Unknown trusted action domain: {domain}")
        else:
            heartbeat_fn = None
            if emit_result_frame and wiring.supports_heartbeat:
                heartbeat_fn = partial(emit_heartbeat, stdout)
            result = wiring.dispatch(data, heartbeat_fn=heartbeat_fn)
            resp = {"status": "ok", "result": result}
    except Exception as exc:
        resp = error_result(message=str(exc), with_traceback=True)

    if emit_result_frame:
        assert stdout is not None
        req_id = str(request.get("id", ""))
        # Request id is assigned after merging resp so a key inside resp cannot override it and make the host kill the worker on a mismatch.
        # Packing fails before any byte hits stdout, so the pipe is still in
        # sync. An oversized result is a small error frame. Letting
        # IpcFrameError escape after EXEC_STARTED ends the worker with no
        # terminal frame, and the host restarts it.
        try:
            write_result_frame(stdout, {**resp, "id": req_id})
        except IpcPayloadSizeError as exc:
            write_result_frame(
                stdout,
                {
                    "id": req_id,
                    **error_result(message=f"Result exceeds maximum payload size: {exc}"),
                },
            )
        return None

    return resp


def _action_ppt_master_turn(request: dict[str, Any]) -> dict[str, Any] | None:
    # Lazy (weight): the ppt-master agent stack loads only for this action.
    from plugin.ppt_master.venv.runner import run_turn

    data = request.get("data")
    if not isinstance(data, dict):
        return error_result(message="ppt_master_turn requires data dict.")
    try:
        result = run_turn(data)
        return {"status": "ok", "result": result}
    except Exception as exc:
        return error_result(message=str(exc), with_traceback=True)


def _action_execute(request: dict[str, Any]) -> dict[str, Any] | None:
    # Lazy (weight): venv_sandbox loads the smolagents executor; importing this harness as a library or serving only trusted actions must not (test_harness_library_import_does_not_claim_or_load_sandbox).
    from plugin.scripting.venv.venv_sandbox import run_sandboxed_code

    code = request.get("code")
    if not isinstance(code, str) or not code.strip():
        return error_result(message="No code provided.")
    return run_sandboxed_code(SandboxRequest.from_wire(request))


def _handle_request(request: dict[str, Any], *, stdout: Any | None = None) -> dict[str, Any] | None:
    action = request.get("action")
    # Omitted, blank, and ``"execute"`` run user code. A typo that also carries
    # ``code`` must not run.
    if action is None or action == "" or action == "execute":
        return _action_execute(request)
    if action == "reset_session":
        # Lazy for the same reason as _action_execute.
        from plugin.scripting.venv.venv_sandbox import reset_sandbox_session

        session_id = _clean_str(request.get("session_id"))
        if not session_id:
            return error_result(message="No session_id provided.")
        return reset_sandbox_session(session_id)
    if action == "ppt_master_turn":
        return _action_ppt_master_turn(request)
    if action == "run_trusted_action":
        data = request.get("data")
        if not isinstance(data, dict):
            return error_result(message="run_trusted_action requires data dict.")
        return _handle_trusted_action(request, data, stdout=stdout)
    return error_result(message=f"Unknown action: {action!r}")


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



def main() -> None:
    set_process_role(ROLE_WORKER)
    die_with_parent()
    _init_logging()
    log.info("Worker process %d starting up with python %s", os.getpid(), sys.version)

    # First read of stdin, so detach cannot drop a frame already buffered.
    stdin = claim_ipc_stdin()
    # Idempotent. The script entry already claimed before later imports. This
    # call is the one in-process tests replace.
    stdout = claim_ipc_channel()
    announce_ready(stdout)

    while True:
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
            bad_req_frame = {"id": "", **error_result(message=f"Invalid pickle request: {e}")}
            try:
                write_pickle_frame(stdout, bad_req_frame)
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
            )
        except Exception:
            log.exception("Failed to write exec_started for request id=%s", req_id)
            break

        response: dict[str, Any] | None = None
        # UserStopped is a USER_STOPPED frame. A pack-size error or any other
        # exception is a terminal error frame with a traceback. Only a read
        # that desyncs the stream (exchange_tool_call reading a host reply)
        # ends the loop.
        lpe = sys.modules.get(_LPE_MODULE)
        if lpe is not None:
            setattr(lpe, "abandoned_timeout", False)

        try:
            defer_token = DEFER_TIMEOUT_FINALIZE.set(True)
            try:
                response = _handle_request(request, stdout=stdout)
            finally:
                DEFER_TIMEOUT_FINALIZE.reset(defer_token)
            log.debug(
                "Finished request id=%s, response status=%s",
                req_id,
                response.get("status") if response else "none",
            )
        except UserStopped as e:
            # Sandbox returns this as a dict. This is the backstop when Stop
            # is raised outside that path, so the process still writes a
            # terminal frame instead of dying without one.
            response = error_result(code="USER_STOPPED", message=str(e) or "Stopped by user.")
        except IpcPayloadSizeError as e:
            log.exception("IPC payload size error handling request id=%s", req_id)
            response = error_result(message=str(e), with_traceback=True)
        except IpcFrameError as e:
            # Genuine read-side desync (e.g. from exchange_tool_call reading host replies).
            # Do not write an error frame to a corrupted stream.
            log.warning("IPC frame error during request id=%s: %s", req_id, e)
            break
        except Exception as e:
            log.exception("Exception handling request id=%s", req_id)
            response = error_result(message=str(e), with_traceback=True)
        except BaseException as e:
            # SystemExit and KeyboardInterrupt are BaseException. After
            # EXEC_STARTED a process death with no terminal frame is not
            # replayed, so every shared session on this worker is dropped.
            # The sandbox returns these as an error dict; this writes that
            # frame when they escape _handle_request, then reads the next request.
            log.exception("BaseException handling request id=%s", req_id)
            response = error_result(message=str(e), with_traceback=True)

        if response is None:
            continue

        response["id"] = req_id
        try:
            write_pickle_frame(stdout, response)
        except Exception as e:
            err_response = {"id": req_id, **error_result(message=f"Pickle serialization failed: {e}")}
            try:
                write_pickle_frame(stdout, err_response)
            except Exception:
                log.exception("Failed to write capped error frame for request id=%s", req_id)
                break

        lpe = sys.modules.get(_LPE_MODULE)
        if lpe is not None:
            # Process backstop: sets abandoned_timeout before os._exit when a pool future outlives the frame.
            lpe.finalize_thread_pool_timeout()
            if getattr(lpe, "abandoned_timeout", False):
                setattr(lpe, "abandoned_timeout", False)
                os._exit(1)


if __name__ == "__main__":
    main()
