# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Contract tests for worker_harness run_trusted_action dispatch."""

from __future__ import annotations

from io import BytesIO
from unittest.mock import patch

from plugin.scripting.venv.worker_harness import _handle_request


def test_handle_request_run_trusted_action_unknown_domain() -> None:
    res = _handle_request(
        {
            "action": "run_trusted_action",
            "data": {"domain": "missing_domain", "helper": "demo"},
        }
    )
    assert res is not None
    assert res["status"] == "error"
    assert "Unknown trusted action domain" in res["message"]


@patch("plugin.scripting.venv.trusted_dispatch.dispatch_analysis", return_value={"status": "ok", "helper": "describe_data"})
def test_handle_request_run_trusted_action_analysis(mock_dispatch) -> None:
    res = _handle_request(
        {
            "action": "run_trusted_action",
            "data": {
                "domain": "analysis",
                "helper": "describe_data",
                "params": {},
                "data_range": "A1:B2",
                "context": {},
            },
        }
    )
    assert res == {"status": "ok", "result": {"status": "ok", "helper": "describe_data"}}
    mock_dispatch.assert_called_once()


@patch(
    "plugin.embeddings.venv.embeddings_index_dispatch.dispatch_trusted",
    return_value={"mode": "cold", "indexed_paragraphs": 1},
)
def test_handle_request_maintain_heartbeat_without_stub_code(mock_dispatch) -> None:
    stdout = BytesIO()
    res = _handle_request(
        {
            "id": "hb-1",
            "action": "run_trusted_action",
            "allow_heartbeat": True,
            "data": {
                "domain": "embeddings_index",
                "helper": "maintain_folder_index",
                "params": {
                    "listing_root": "/tmp/folder",
                    "model": "demo-model",
                    "mode": "auto",
                    "search_mode": "hybrid",
                },
            },
        },
        stdout=stdout,
    )
    assert res is None
    mock_dispatch.assert_called_once()
    assert mock_dispatch.call_args.kwargs.get("heartbeat_fn") is not None
    out = stdout.getvalue()
    assert len(out) > 0


@patch(
    "plugin.embeddings.venv.embeddings_index_dispatch.dispatch_trusted",
    return_value={"large": "x" * 17_000_000},
)
def test_handle_request_trusted_action_heartbeat_oversized_payload_writes_error_frame(mock_dispatch) -> None:
    from plugin.scripting.ipc import DEFAULT_MAX_PAYLOAD_BYTES
    from plugin.scripting.venv.worker_heartbeat import parse_frame
    from plugin.scripting.ipc import read_frame_payload

    stdout = BytesIO()
    res = _handle_request(
        {
            "id": "hb-oversize",
            "action": "run_trusted_action",
            "allow_heartbeat": True,
            "data": {
                "domain": "embeddings_index",
                "helper": "maintain_folder_index",
                "params": {},
            },
        },
        stdout=stdout,
    )
    assert res is None
    mock_dispatch.assert_called_once()
    stdout.seek(0)
    raw = read_frame_payload(stdout, max_payload_bytes=DEFAULT_MAX_PAYLOAD_BYTES)
    assert raw is not None
    frame = parse_frame(raw)
    assert frame["id"] == "hb-oversize"
    assert frame["frame_type"] == "result"
    assert frame["status"] == "error"
    assert "maximum payload size" in frame["message"]


@patch(
    "plugin.embeddings.venv.embeddings_index_dispatch.dispatch_trusted",
    side_effect=RuntimeError("Index corrupt"),
)
def test_handle_request_trusted_action_heartbeat_exception_writes_error_frame(mock_dispatch) -> None:
    from plugin.scripting.ipc import DEFAULT_MAX_PAYLOAD_BYTES
    from plugin.scripting.venv.worker_heartbeat import parse_frame
    from plugin.scripting.ipc import read_frame_payload

    stdout = BytesIO()
    res = _handle_request(
        {
            "id": "hb-err",
            "action": "run_trusted_action",
            "allow_heartbeat": True,
            "data": {
                "domain": "embeddings_index",
                "helper": "maintain_folder_index",
                "params": {},
            },
        },
        stdout=stdout,
    )
    assert res is None
    stdout.seek(0)
    raw = read_frame_payload(stdout, max_payload_bytes=DEFAULT_MAX_PAYLOAD_BYTES)
    assert raw is not None
    frame = parse_frame(raw)
    assert frame["id"] == "hb-err"
    assert frame["frame_type"] == "result"
    assert frame["status"] == "error"
    assert "Index corrupt" in frame["message"]
    assert "traceback" in frame


@patch("plugin.scripting.venv.worker_harness._action_execute", return_value={"status": "ok", "result": 1})
def test_handle_request_unknown_action_does_not_execute(mock_execute) -> None:
    res = _handle_request({"action": "not_a_real_action", "code": "result = 1"})
    assert res is not None
    assert res["status"] == "error"
    assert "Unknown action" in res["message"]
    mock_execute.assert_not_called()

    ok = _handle_request({"action": "execute", "code": "result = 1"})
    assert ok is not None
    assert ok["status"] == "ok"
    assert ok["result"] == 1

    missing = _handle_request({"code": "result = 1"})
    assert missing is not None
    assert missing["status"] == "ok"

    blank = _handle_request({"action": "", "code": "result = 1"})
    assert blank is not None
    assert blank["status"] == "ok"

    bad_type = _handle_request({"action": 1, "code": "result = 1"})
    assert bad_type is not None
    assert bad_type["status"] == "error"
    assert "Unknown action" in bad_type["message"]
    assert mock_execute.call_count == 3


def test_system_exit_during_request_writes_terminal_frame(monkeypatch) -> None:
    """SystemExit after EXEC_STARTED must become a terminal error frame.

    What was wrong: the harness only caught Exception. raise SystemExit
    killed the process with no terminal frame, and the host refused replay.
    """
    from plugin.scripting.ipc import EXEC_STARTED, read_pickle_frame, write_pickle_frame
    from plugin.scripting.venv import worker_harness

    stdin = BytesIO()
    write_pickle_frame(stdin, {"id": "req-1", "action": "execute", "code": "raise SystemExit(3)"})
    stdin.seek(0)
    stdout = BytesIO()

    def boom(request: dict, stdout: object = None) -> None:
        del request, stdout
        raise SystemExit(3)

    monkeypatch.setattr(worker_harness, "_die_with_parent", lambda: None)
    monkeypatch.setattr(worker_harness, "_init_logging", lambda: None)
    monkeypatch.setattr(worker_harness, "claim_ipc_channel", lambda: stdout)
    monkeypatch.setattr(worker_harness, "_handle_request", boom)
    monkeypatch.setattr(worker_harness.sys, "stdin", type("Stdin", (), {"buffer": stdin})())

    worker_harness.main()

    stdout.seek(0)
    started = read_pickle_frame(stdout, require_dict=True)
    assert started["type"] == EXEC_STARTED
    assert started["id"] == "req-1"
    terminal = read_pickle_frame(stdout, require_dict=True)
    assert terminal["id"] == "req-1"
    assert terminal["status"] == "error"
    assert "SystemExit" in terminal.get("traceback", "")


@patch(
    "plugin.scripting.venv.trusted_dispatch.dispatch_analysis",
    return_value={"helper": "describe_data"},
)
def test_allow_heartbeat_frames_non_heartbeat_success(mock_dispatch) -> None:
    from plugin.scripting.ipc import DEFAULT_MAX_PAYLOAD_BYTES, read_frame_payload
    from plugin.scripting.venv.worker_heartbeat import parse_frame

    stdout = BytesIO()
    res = _handle_request(
        {
            "id": "plain-hb",
            "action": "run_trusted_action",
            "allow_heartbeat": True,
            "data": {
                "domain": "analysis",
                "helper": "describe_data",
                "params": {},
            },
        },
        stdout=stdout,
    )
    assert res is None
    mock_dispatch.assert_called_once()
    assert "heartbeat_fn" not in mock_dispatch.call_args.kwargs
    stdout.seek(0)
    raw = read_frame_payload(stdout, max_payload_bytes=DEFAULT_MAX_PAYLOAD_BYTES)
    assert raw is not None
    frame = parse_frame(raw)
    assert frame["frame_type"] == "result"
    assert frame["status"] == "ok"
    assert frame["id"] == "plain-hb"
    assert frame["result"] == {"helper": "describe_data"}
    assert read_frame_payload(stdout, max_payload_bytes=DEFAULT_MAX_PAYLOAD_BYTES) is None


@patch(
    "plugin.scripting.venv.trusted_dispatch.dispatch_analysis",
    side_effect=RuntimeError("bad spec"),
)
def test_allow_heartbeat_frames_non_heartbeat_error(mock_dispatch) -> None:
    from plugin.scripting.ipc import DEFAULT_MAX_PAYLOAD_BYTES, read_frame_payload
    from plugin.scripting.venv.worker_heartbeat import parse_frame

    stdout = BytesIO()
    res = _handle_request(
        {
            "id": "plain-hb-err",
            "action": "run_trusted_action",
            "allow_heartbeat": True,
            "data": {
                "domain": "analysis",
                "helper": "describe_data",
                "params": {},
            },
        },
        stdout=stdout,
    )
    assert res is None
    stdout.seek(0)
    raw = read_frame_payload(stdout, max_payload_bytes=DEFAULT_MAX_PAYLOAD_BYTES)
    assert raw is not None
    frame = parse_frame(raw)
    assert frame["frame_type"] == "result"
    assert frame["status"] == "error"
    assert "bad spec" in frame["message"]
    assert "traceback" in frame


def test_allow_heartbeat_frames_unknown_domain() -> None:
    from plugin.scripting.ipc import DEFAULT_MAX_PAYLOAD_BYTES, read_frame_payload
    from plugin.scripting.venv.worker_heartbeat import parse_frame

    stdout = BytesIO()
    res = _handle_request(
        {
            "id": "missing-hb",
            "action": "run_trusted_action",
            "allow_heartbeat": True,
            "data": {
                "domain": "missing_domain",
                "helper": "demo",
            },
        },
        stdout=stdout,
    )
    assert res is None
    stdout.seek(0)
    raw = read_frame_payload(stdout, max_payload_bytes=DEFAULT_MAX_PAYLOAD_BYTES)
    assert raw is not None
    frame = parse_frame(raw)
    assert frame["status"] == "error"
    assert frame["frame_type"] == "result"
    assert "Unknown trusted action domain" in frame["message"]


@patch(
    "plugin.scripting.venv.trusted_dispatch.dispatch_analysis",
    return_value={"helper": "describe_data"},
)
def test_stdout_without_allow_heartbeat_returns_dict(mock_dispatch) -> None:
    stdout = BytesIO()
    res = _handle_request(
        {
            "id": "no-hb",
            "action": "run_trusted_action",
            "data": {
                "domain": "analysis",
                "helper": "describe_data",
                "params": {},
            },
        },
        stdout=stdout,
    )
    assert res == {"status": "ok", "result": {"helper": "describe_data"}}
    assert stdout.getvalue() == b""


def test_harness_library_import_does_not_claim_or_load_sandbox() -> None:
    import os
    import subprocess
    import sys
    from pathlib import Path

    repo_root = str(Path(__file__).resolve().parents[2])
    env = dict(os.environ)
    existing_pp = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = f"{repo_root}:{existing_pp}" if existing_pp else repo_root

    child_code = (
        "import sys\n"
        "import plugin.scripting.ipc as ipc\n"
        "initial_stream = ipc._child_ipc_stream\n"
        "import plugin.scripting.venv.worker_harness\n"
        "if ipc._child_ipc_stream is not initial_stream:\n"
        "    sys.stderr.write('ipc._child_ipc_stream changed during harness import\\n')\n"
        "    sys.exit(1)\n"
        "if 'plugin.scripting.venv.venv_sandbox' in sys.modules:\n"
        "    sys.stderr.write('plugin.scripting.venv.venv_sandbox loaded during harness import\\n')\n"
        "    sys.exit(1)\n"
        "sys.exit(0)\n"
    )

    proc = subprocess.run(
        [sys.executable, "-c", child_code],
        cwd=repo_root,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc.returncode == 0, f"Child failed (stdout={proc.stdout}, stderr={proc.stderr})"

