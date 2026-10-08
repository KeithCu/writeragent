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


@pytest.mark.parametrize(
    "value, value_2, value_3",
    [
        pytest.param(5, "structural", 1.0, id="test_weighted_judge_score_structural"),
        pytest.param(1, "table", 0.36, id="test_weighted_judge_score_table"),
    ],
)
def test_weighted_judge_score_structural(value, value_2, value_3) -> None:
    assert _weighted_judge_score(5, value, "N/A", value_2) == pytest.approx(value_3)


def test_weighted_judge_score_creative() -> None:
    assert _weighted_judge_score(5, 5, 5, "creative") == pytest.approx(1.0)
    # Accuracy-first: 50/20/30, not 30/20/50.
    assert _weighted_judge_score(5, 1, 1, "creative") == pytest.approx(0.6)

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
    assert r.parsed_ok is True


def test_parse_judge_json_garbage_keeps_parsed_ok_false() -> None:
    r = _parse_judge_json("not json at all", "creative")
    assert r.parsed_ok is False


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
        judge_model="openai/gpt-oss-120b",
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
    assert cfg["model"] == "openai/gpt-oss-120b"
    assert cfg["is_openrouter"] is True
    instance.request_with_tools.assert_called_once()
    call_kw = instance.request_with_tools.call_args.kwargs
    assert call_kw.get("prepend_dev_build_system_prefix") is False


@patch("plugin.framework.client.llm_client.LlmClient")
def test_score_with_judge_llm_retries_unparseable(mock_client_cls: MagicMock) -> None:
    instance = MagicMock()
    mock_client_cls.return_value = instance
    good = json.dumps({
        "thought_process": "recovered",
        "accuracy_score": 5,
        "formatting_score": 5,
        "naturalness_score": 5,
    })
    instance.request_with_tools.side_effect = [
        {"content": "not json"},
        {"content": good},
    ]
    score, result = score_with_judge_llm(
        endpoint="https://openrouter.ai/api/v1",
        api_key="test-key",
        judge_model="openai/gpt-oss-120b",
        document_content="a",
        user_question="rewrite",
        model_answer="ok",
        task_category="creative",
    )
    assert result.parsed_ok is True
    assert result.thought_process == "recovered"
    assert score == pytest.approx(1.0)
    assert instance.request_with_tools.call_count == 2


def test_example_passed_keeps_hard_pass_when_judge_fails() -> None:
    from eval_core import ExampleEval, example_passed

    result = ExampleEval(
        task_id="reformat_resume",
        correctness=1.0,
        missing_expected=[],
        found_reject=[],
        metric_score=1.0,
        prompt_tokens=1,
        completion_tokens=1,
        total_tokens=2,
        final_document="<h1>John Doe</h1>",
        judge_score=None,
        judge_error="unparseable judge JSON",
        agent_score=1.0,
    )
    assert example_passed(result) is True
