# WriterAgent tests for LlmClient-based eval judge
from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

pytest.importorskip("dspy")

_PO = Path(__file__).resolve().parents[2] / "scripts" / "prompt_optimization"
if str(_PO) not in sys.path:
    sys.path.insert(0, str(_PO))

from eval_core import (  # noqa: E402
    JudgeResult,
    _parse_judge_json,
    _weighted_judge_score,
    score_with_judge_llm,
)


def test_weighted_judge_score_structural() -> None:
    assert _weighted_judge_score(5, 5, "N/A", "structural") == pytest.approx(1.0)


def test_weighted_judge_score_creative() -> None:
    assert _weighted_judge_score(5, 5, 5, "creative") == pytest.approx(1.0)


def test_parse_judge_json_object() -> None:
    payload = {
        "thought_process": "ok",
        "accuracy_score": 4,
        "formatting_score": 5,
        "naturalness_score": None,
    }
    r = _parse_judge_json(json.dumps(payload), "structural")
    assert isinstance(r, JudgeResult)
    assert r.thought_process == "ok"
    assert r.score > 0.7


@patch("plugin.framework.client.llm_client.LlmClient")
def test_score_with_judge_llm_uses_cli_config(mock_client_cls: MagicMock) -> None:
    instance = MagicMock()
    mock_client_cls.return_value = instance
    instance.request_with_tools.return_value = {
        "content": json.dumps({
            "thought_process": "good table",
            "accuracy_score": 5,
            "formatting_score": 4,
            "naturalness_score": None,
        }),
    }

    score, result = score_with_judge_llm(
        endpoint="https://openrouter.ai/api/v1",
        api_key="test-key",
        judge_model="x-ai/grok-4.1-fast",
        document_content="a",
        user_question="make table",
        model_answer="<table></table>",
        task_category="structural",
    )

    assert score > 0.5
    assert result.thought_process == "good table"
    mock_client_cls.assert_called_once()
    cfg = mock_client_cls.call_args[0][0]
    assert cfg["api_key"] == "test-key"
    assert cfg["model"] == "x-ai/grok-4.1-fast"
    assert cfg["is_openrouter"] is True
    instance.request_with_tools.assert_called_once()
    call_kw = instance.request_with_tools.call_args.kwargs
    assert call_kw.get("prepend_dev_build_system_prefix") is False
