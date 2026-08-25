# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Direct tests for trusted_rpc host packet + worker-result parsing."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from plugin.framework.constants import EMBEDDINGS_HEARTBEAT_GRACE_S, WORKER_POOL_DEFAULT
from plugin.framework.errors import ToolExecutionError
from plugin.scripting.trusted_rpc import parse_worker_dict_result, run_trusted_worker_action


def test_parse_worker_dict_result_ok() -> None:
    res = parse_worker_dict_result(
        {"status": "ok", "result": {"output": 42}},
        error_code="ERR",
        error_label="Test",
    )
    assert res == {"output": 42}


def test_parse_worker_dict_result_error_status() -> None:
    response = {"status": "error", "message": "Failed"}
    with pytest.raises(ToolExecutionError) as exc_info:
        parse_worker_dict_result(
            response,
            error_code="WORKER_FAIL",
            error_label="Test",
        )
    assert exc_info.value.code == "WORKER_FAIL"
    assert "Failed" in str(exc_info.value)
    assert exc_info.value.details == {"worker": response}


def test_parse_worker_dict_result_missing_message_fallback() -> None:
    with pytest.raises(ToolExecutionError) as exc_info:
        parse_worker_dict_result(
            {"status": "error"},
            error_code="WORKER_FAIL",
            error_label="Analysis",
        )
    assert "Analysis worker failed." in str(exc_info.value)


@pytest.mark.parametrize("bad_result", [None, [], 42, "nope"])
def test_parse_worker_dict_result_non_dict_result(bad_result: object) -> None:
    with pytest.raises(ToolExecutionError) as exc_info:
        parse_worker_dict_result(
            {"status": "ok", "result": bad_result},
            error_code="BAD_SHAPE",
            error_label="Vision",
        )
    assert exc_info.value.code == "BAD_SHAPE"
    assert "unexpected result" in str(exc_info.value)
    assert exc_info.value.details == {"result_type": type(bad_result).__name__}


def test_parse_worker_dict_result_overflow_pre_fails_closed() -> None:
    import deal

    from plugin.framework.deal_shim import DEAL_MAX_SHAPE_DIM, DEAL_MAX_TOKEN
    from tests.strip_bundle import deal_pre_present

    if not deal_pre_present(parse_worker_dict_result):
        pytest.skip("@deal.pre stripped in release bundle")
    too_many = {f"k{i}": i for i in range(DEAL_MAX_SHAPE_DIM + 1)}
    with pytest.raises(deal.PreContractError):
        parse_worker_dict_result(too_many, error_code="ERR", error_label="Test")
    with pytest.raises(deal.PreContractError):
        parse_worker_dict_result(
            {"status": "ok", "result": {}},
            error_code="E" * (DEAL_MAX_TOKEN + 1),
            error_label="Test",
        )
    parse_worker_dict_result({"status": "ok", "result": {"ok": True}}, error_code="ERR", error_label="Test")


def test_run_trusted_worker_action_builds_packet() -> None:
    ctx = MagicMock()
    with patch("plugin.scripting.trusted_rpc.run_code_in_user_venv") as mock_run:
        mock_run.return_value = {"status": "ok", "result": {"helper": "x"}}
        out = run_trusted_worker_action(
            ctx,
            domain="analysis",
            helper="describe_data",
            params={"a": 1},
            data_range="A1:B2",
            context={"k": 1},
            session_id="writeragent:analysis",
            timeout_sec=30,
            additional_data={"extra": 2},
        )
    assert out == {"helper": "x"}
    mock_run.assert_called_once()
    args, kwargs = mock_run.call_args
    assert args[0] is ctx
    assert kwargs["code"] is None
    assert kwargs["action"] == "run_trusted_action"
    assert kwargs["session_id"] == "writeragent:analysis"
    assert kwargs["timeout_sec"] == 30
    assert kwargs["worker_pool"] == WORKER_POOL_DEFAULT
    assert kwargs["allow_heartbeat"] is False
    assert kwargs["on_heartbeat"] is None
    assert kwargs["heartbeat_grace_sec"] == EMBEDDINGS_HEARTBEAT_GRACE_S
    data = kwargs["data"]
    assert data["domain"] == "analysis"
    assert data["helper"] == "describe_data"
    assert data["params"] == {"a": 1}
    assert data["data_range"] == "A1:B2"
    assert data["context"] == {"k": 1}
    assert data["extra"] == 2


def test_run_trusted_worker_action_defaults_empty_maps() -> None:
    ctx = MagicMock()
    with patch("plugin.scripting.trusted_rpc.run_code_in_user_venv") as mock_run:
        mock_run.return_value = {"status": "ok", "result": {"ok": True}}
        run_trusted_worker_action(
            ctx,
            domain="viz",
            session_id="s",
            timeout_sec=10,
        )
    data = mock_run.call_args.kwargs["data"]
    assert data["helper"] == ""
    assert data["params"] == {}
    assert data["context"] == {}
    assert data["data_range"] is None
    assert "extra" not in data


def test_run_trusted_worker_action_error_status() -> None:
    ctx = MagicMock()
    with patch("plugin.scripting.trusted_rpc.run_code_in_user_venv") as mock_run:
        mock_run.return_value = {"status": "error", "message": "boom"}
        with pytest.raises(ToolExecutionError) as exc_info:
            run_trusted_worker_action(
                ctx,
                domain="analysis",
                session_id="s",
                timeout_sec=10,
                error_code="ANALYSIS_ERROR",
                error_label="Analysis",
            )
    assert exc_info.value.code == "ANALYSIS_ERROR"
    assert "boom" in str(exc_info.value)


def test_run_trusted_worker_action_heartbeat_callback() -> None:
    ctx = MagicMock()
    seen: list[dict] = []

    def heartbeat_fn(hb: dict) -> None:
        seen.append(hb)

    with patch("plugin.scripting.trusted_rpc.run_code_in_user_venv") as mock_run:
        mock_run.return_value = {"status": "ok", "result": {"ok": True}}
        run_trusted_worker_action(
            ctx,
            domain="embeddings_index",
            helper="maintain_folder_index",
            session_id="s",
            timeout_sec=90,
            allow_heartbeat=True,
            heartbeat_fn=heartbeat_fn,
        )
    kwargs = mock_run.call_args.kwargs
    assert kwargs["allow_heartbeat"] is True
    assert kwargs["heartbeat_grace_sec"] == EMBEDDINGS_HEARTBEAT_GRACE_S
    on_heartbeat = kwargs["on_heartbeat"]
    assert callable(on_heartbeat)
    on_heartbeat({"progress": 1})
    assert seen == [{"progress": 1}]
