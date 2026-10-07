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
