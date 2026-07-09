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


@patch("plugin.scripting.venv.trusted_dispatch.dispatch_trusted", return_value={"status": "ok", "helper": "describe_data"})
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
