# WriterAgent tests for scripts/prompt_optimization/eval_auth.py
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_PO = Path(__file__).resolve().parents[2] / "scripts" / "prompt_optimization"
if str(_PO) not in sys.path:
    sys.path.insert(0, str(_PO))

from eval_auth import (  # noqa: E402
    OPENROUTER_DEFAULT_JUDGE,
    build_eval_api_config,
    is_openrouter_endpoint,
    resolve_api_base,
    resolve_api_key,
    resolve_judge_model,
)


def test_resolve_api_key_cli_wins(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "env-or")
    assert resolve_api_key(cli_key="cli-key") == "cli-key"


def test_resolve_api_key_env_precedence(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("WRITERAGENT_API_KEY", raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "openai-k")
    monkeypatch.setenv("OPENROUTER_API_KEY", "or-k")
    assert resolve_api_key(cli_key=None) == "openai-k"


def test_resolve_api_base_defaults() -> None:
    assert "openrouter.ai" in resolve_api_base(cli_base=None)


def test_resolve_judge_openrouter_default() -> None:
    assert (
        resolve_judge_model(
            cli_judge=None,
            endpoint="https://openrouter.ai/api/v1",
            model_ids=["qwen/qwen3-coder-next"],
        )
        == OPENROUTER_DEFAULT_JUDGE
    )


def test_resolve_judge_local_first_model() -> None:
    assert (
        resolve_judge_model(
            cli_judge=None,
            endpoint="http://127.0.0.1:11434/v1",
            model_ids=["llama3.2", "mistral"],
        )
        == "llama3.2"
    )


def test_build_eval_api_config_openrouter_flag() -> None:
    cfg = build_eval_api_config(
        endpoint="https://openrouter.ai/api/v1",
        api_key="k",
        model="openai/gpt-4o-mini",
    )
    assert cfg["is_openrouter"] is True
    assert cfg["model"] == "openai/gpt-4o-mini"


def test_is_openrouter_endpoint() -> None:
    assert is_openrouter_endpoint("https://openrouter.ai/api/v1")
    assert not is_openrouter_endpoint("http://127.0.0.1:11434/v1")
