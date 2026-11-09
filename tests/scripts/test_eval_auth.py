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
    is_anthropic_model_id,
    is_openrouter_endpoint,
    native_anthropic_model_id,
    require_api_key,
    require_eval_keys,
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


def test_openrouter_default_judge_is_gpt_oss_120b() -> None:
    assert OPENROUTER_DEFAULT_JUDGE == "openai/gpt-oss-120b:nitro"


def test_resolve_judge_openrouter_default() -> None:
    assert (
        resolve_judge_model(
            cli_judge=None,
            endpoint="https://openrouter.ai/api/v1",
            model_ids=["qwen/qwen3.8-flash"],
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
    assert "temperature" not in cfg


def test_is_openrouter_endpoint() -> None:
    assert is_openrouter_endpoint("https://openrouter.ai/api/v1")
    assert not is_openrouter_endpoint("http://127.0.0.1:11434/v1")


def test_require_api_key_openrouter_empty_exits() -> None:
    with pytest.raises(SystemExit):
        require_api_key("", "https://openrouter.ai/api/v1")


def test_require_api_key_openrouter_accepts_key() -> None:
    require_api_key("sk-test", "https://openrouter.ai/api/v1")


def test_is_anthropic_model_id() -> None:
    assert is_anthropic_model_id("anthropic/claude-sonnet-4.5:nitro")
    assert is_anthropic_model_id("openrouter/anthropic/claude-opus-4:exacto")
    assert is_anthropic_model_id("claude-haiku-4-5:nitro:online")
    assert not is_anthropic_model_id("openai/gpt-oss-120b:nitro")
    assert not is_anthropic_model_id("foo/claude-3")
    assert not is_anthropic_model_id("claude")
    assert not is_anthropic_model_id("claude-")
    assert not is_anthropic_model_id("anthropic/")
    assert not is_anthropic_model_id("")


def test_native_anthropic_model_id() -> None:
    assert native_anthropic_model_id("anthropic/claude-sonnet-4.5:nitro") == "claude-sonnet-4.5"
    assert native_anthropic_model_id("claude-haiku-4-5:nitro:online") == "claude-haiku-4-5"
    assert native_anthropic_model_id("openrouter/anthropic/claude-opus-4:exacto") == "claude-opus-4"
    assert native_anthropic_model_id("anthropic/claude-sonnet-4.5:thinking") == "claude-sonnet-4.5:thinking"
    assert native_anthropic_model_id("anthropic/anthropic/claude-x") == "anthropic/claude-x"


def test_build_eval_api_config_claude_routes_to_anthropic(monkeypatch: pytest.MonkeyPatch) -> None:
    from plugin.framework.client.auth import resolve_auth_for_config
    from plugin.framework.client.openai_shim import AnthropicShim, get_provider_shim_class

    monkeypatch.setenv("ANTHROPIC_API_KEY", "ant-key-123")
    cfg = build_eval_api_config(
        endpoint="https://openrouter.ai/api/v1",
        api_key="or-key",
        model="anthropic/claude-sonnet-4.5:nitro",
    )
    assert "api.anthropic.com" in cfg["endpoint"]
    assert cfg["api_key"] == "ant-key-123"
    assert cfg["is_openrouter"] is False
    assert cfg["model"] == "claude-sonnet-4.5"

    auth = resolve_auth_for_config(cfg)
    assert auth["provider"] == "anthropic"
    assert get_provider_shim_class(auth["provider"]) is AnthropicShim


def test_build_eval_api_config_variants(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "ant-key-123")
    cfg1 = build_eval_api_config(
        endpoint="https://openrouter.ai/api/v1",
        api_key="or-key",
        model="claude-haiku-4-5:nitro:online",
    )
    assert cfg1["model"] == "claude-haiku-4-5"

    cfg2 = build_eval_api_config(
        endpoint="https://openrouter.ai/api/v1",
        api_key="or-key",
        model="openrouter/anthropic/claude-opus-4:exacto",
    )
    assert cfg2["model"] == "claude-opus-4"


def test_build_eval_api_config_env_deleted_stays_openrouter(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    cfg = build_eval_api_config(
        endpoint="https://openrouter.ai/api/v1",
        api_key="or-key",
        model="anthropic/claude-sonnet-4.5:nitro",
    )
    assert "openrouter.ai" in cfg["endpoint"]
    assert cfg["api_key"] == "or-key"
    assert cfg["model"] == "anthropic/claude-sonnet-4.5:nitro"
    assert cfg["is_openrouter"] is True


def test_build_eval_api_config_non_anthropic_model(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "ant-key-123")
    cfg = build_eval_api_config(
        endpoint="https://openrouter.ai/api/v1",
        api_key="or-key",
        model="openai/gpt-oss-120b:nitro",
    )
    assert "openrouter.ai" in cfg["endpoint"]
    assert cfg["api_key"] == "or-key"
    assert cfg["model"] == "openai/gpt-oss-120b:nitro"


def test_build_eval_api_config_local_endpoint_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "ant-key-123")
    cfg = build_eval_api_config(
        endpoint="http://127.0.0.1:8080/v1",
        api_key="loc-key",
        model="claude-sonnet-4-5",
    )
    assert cfg["endpoint"] == "http://127.0.0.1:8080"
    assert cfg["api_key"] == "loc-key"
    assert cfg["model"] == "claude-sonnet-4-5"


def test_build_eval_api_config_already_anthropic_base(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "env-key")
    cfg1 = build_eval_api_config(
        endpoint="https://api.anthropic.com/v1",
        api_key="cli-key",
        model="anthropic/claude-sonnet-4.5:nitro",
    )
    assert cfg1["api_key"] == "cli-key"
    assert cfg1["model"] == "claude-sonnet-4.5"

    cfg2 = build_eval_api_config(
        endpoint="https://api.anthropic.com/v1",
        api_key="",
        model="anthropic/claude-sonnet-4.5:nitro",
    )
    assert cfg2["api_key"] == "env-key"
    assert cfg2["model"] == "claude-sonnet-4.5"


def test_require_eval_keys_all_claude_no_openrouter_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "ant-key")
    require_eval_keys(
        endpoint="https://openrouter.ai/api/v1",
        api_key="",
        models=["anthropic/claude-sonnet-4.5"],
    )


def test_require_eval_keys_claude_and_openrouter_judge_missing_key_exits(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "ant-key")
    with pytest.raises(SystemExit):
        require_eval_keys(
            endpoint="https://openrouter.ai/api/v1",
            api_key="",
            models=["anthropic/claude-sonnet-4.5", "openai/gpt-oss-120b:nitro"],
        )


def test_require_eval_keys_mixed_models(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "ant-key")
    with pytest.raises(SystemExit):
        require_eval_keys(
            endpoint="https://openrouter.ai/api/v1",
            api_key="",
            models=["anthropic/claude-sonnet-4.5", "openai/gpt-4o"],
        )

    require_eval_keys(
        endpoint="https://openrouter.ai/api/v1",
        api_key="or-key",
        models=["anthropic/claude-sonnet-4.5", "openai/gpt-4o"],
    )


def test_require_api_key_anthropic_hint(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit):
        require_api_key("", "https://api.anthropic.com/v1")
    err = capsys.readouterr().err
    assert "ANTHROPIC_API_KEY" in err
