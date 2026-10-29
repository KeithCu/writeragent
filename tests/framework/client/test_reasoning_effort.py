import json
from typing import Any
import pytest

from plugin.framework.client.base_provider_shim import BaseProviderShim
from plugin.framework.client.google_shim import GoogleShim
from plugin.framework.client.grok_shim import GrokShim
from plugin.framework.client.openai_shim import OllamaShim, OpenAIShim, TogetherShim


class DummyClient:
    def __init__(self, config: dict[str, Any] | None = None) -> None:
        self.config = config if config is not None else {}

    def _endpoint(self) -> str:
        return "https://api.example.com"

    def _api_path(self) -> str:
        return "/v1"

    def _headers(self) -> dict[str, str]:
        return {"Authorization": "Bearer test"}


def test_unset_sends_nothing() -> None:
    client = DummyClient(config={"is_openrouter": False})
    shim = BaseProviderShim(client)
    _method, _path, body, _headers = shim.build_chat_request(
        [{"role": "user", "content": "hi"}], 100, None, None, False, "test-model", None
    )
    data = json.loads(body)
    assert "reasoning" not in data
    assert "reasoning_effort" not in data


@pytest.mark.parametrize("invalid_effort", ["", "   ", 3, None])
def test_blank_and_non_string_send_nothing(invalid_effort: Any) -> None:
    client = DummyClient(config={"is_openrouter": False, "reasoning_effort": invalid_effort})
    shim = BaseProviderShim(client)
    _method, _path, body, _headers = shim.build_chat_request(
        [{"role": "user", "content": "hi"}], 100, None, None, False, "test-model", None
    )
    data = json.loads(body)
    assert "reasoning" not in data
    assert "reasoning_effort" not in data


def test_openrouter_effort_object() -> None:
    client = DummyClient(config={"is_openrouter": True, "reasoning_effort": "high"})
    shim = BaseProviderShim(client)
    _method, _path, body, _headers = shim.build_chat_request(
        [{"role": "user", "content": "hi"}], 100, None, None, False, "test-model", None
    )
    data = json.loads(body)
    assert data.get("reasoning") == {"effort": "high"}
    assert "reasoning_effort" not in data


@pytest.mark.parametrize(
    "shim_cls,model_name",
    [
        (BaseProviderShim, "test-model"),
        (OpenAIShim, "gpt-4o"),
        (GoogleShim, "gemini-2.0-flash"),
        (GrokShim, "grok-2"),
        (OllamaShim, "llama3"),
        (TogetherShim, "meta-llama/Llama-3-70b"),
    ],
)
def test_other_host_top_level(shim_cls: Any, model_name: str) -> None:
    client = DummyClient(config={"is_openrouter": False, "reasoning_effort": "xhigh"})
    shim = shim_cls(client)
    _method, _path, body, _headers = shim.build_chat_request(
        [{"role": "user", "content": "hi"}], 100, None, None, False, model_name, None
    )
    data = json.loads(body)
    assert data.get("reasoning_effort") == "xhigh"
    assert "reasoning" not in data


def test_openai_reasoning_model_keeps_effort_and_max_completion_tokens() -> None:
    client = DummyClient(config={"is_openrouter": False, "reasoning_effort": "max"})
    shim = OpenAIShim(client)
    _method, _path, body, _headers = shim.build_chat_request(
        [{"role": "user", "content": "hi"}], 100, 0.2, None, False, "o3", None
    )
    data = json.loads(body)
    assert data.get("reasoning_effort") == "max"
    assert data.get("max_completion_tokens") == 100
    assert "max_tokens" not in data
    assert "temperature" not in data
    assert "reasoning" not in data


def test_grammar_extra_wins_over_setting() -> None:
    # OpenRouter
    client_or = DummyClient(config={"is_openrouter": True, "reasoning_effort": "high"})
    shim_or = BaseProviderShim(client_or)
    grammar_extra = {"reasoning": {"effort": "minimal"}}
    _method, _path, body, _headers = shim_or.build_chat_request(
        [{"role": "user", "content": "hi"}], 100, None, None, False, "test-model", None, chat_extra=grammar_extra
    )
    data_or = json.loads(body)
    assert data_or.get("reasoning") == {"effort": "minimal"}
    assert "reasoning_effort" not in data_or

    # Other host
    client_other = DummyClient(config={"is_openrouter": False, "reasoning_effort": "high"})
    shim_other = BaseProviderShim(client_other)
    _method, _path, body, _headers = shim_other.build_chat_request(
        [{"role": "user", "content": "hi"}], 100, None, None, False, "test-model", None, chat_extra=grammar_extra
    )
    data_other = json.loads(body)
    assert data_other.get("reasoning_effort") == "minimal"
    assert "reasoning" not in data_other

    # Caller's dict must NOT be mutated
    assert grammar_extra == {"reasoning": {"effort": "minimal"}}


def test_grammar_extra_on_non_openrouter_is_not_a_reasoning_object() -> None:
    # Side bug fix: without reasoning_effort setting, non-OpenRouter host gets reasoning_effort, not reasoning dict
    client = DummyClient(config={"is_openrouter": False})
    shim = BaseProviderShim(client)
    grammar_extra = {"reasoning": {"effort": "minimal"}}
    _method, _path, body, _headers = shim.build_chat_request(
        [{"role": "user", "content": "hi"}], 100, None, None, False, "test-model", None, chat_extra=grammar_extra
    )
    data = json.loads(body)
    assert "reasoning" not in data
    assert data.get("reasoning_effort") == "minimal"


def test_openrouter_chat_extra_survives_when_unset() -> None:
    extra = {"provider": {"order": ["a"]}, "reasoning": {"effort": "low", "exclude": True}}
    client = DummyClient(config={"is_openrouter": True, "openrouter_chat_extra": extra})
    shim = BaseProviderShim(client)
    _method, _path, body, _headers = shim.build_chat_request(
        [{"role": "user", "content": "hi"}], 100, None, None, False, "test-model", None
    )
    data = json.loads(body)
    assert data.get("provider") == {"order": ["a"]}
    assert data.get("reasoning") == {"effort": "low", "exclude": True}
    assert "reasoning_effort" not in data


def test_setting_overrides_extra_effort_only() -> None:
    extra = {"provider": {"order": ["a"]}, "reasoning": {"effort": "low", "exclude": True}}
    client = DummyClient(config={"is_openrouter": True, "openrouter_chat_extra": extra, "reasoning_effort": "high"})
    shim = BaseProviderShim(client)
    _method, _path, body, _headers = shim.build_chat_request(
        [{"role": "user", "content": "hi"}], 100, None, None, False, "test-model", None
    )
    data = json.loads(body)
    assert data.get("provider") == {"order": ["a"]}
    assert data.get("reasoning") == {"effort": "high", "exclude": True}


def test_grammar_wins_over_setting_and_extra() -> None:
    extra = {"provider": {"order": ["a"]}, "reasoning": {"effort": "low", "exclude": True}}
    client = DummyClient(config={"is_openrouter": True, "openrouter_chat_extra": extra, "reasoning_effort": "high"})
    shim = BaseProviderShim(client)
    grammar_extra = {"reasoning": {"effort": "minimal"}}
    _method, _path, body, _headers = shim.build_chat_request(
        [{"role": "user", "content": "hi"}], 100, None, None, False, "test-model", None, chat_extra=grammar_extra
    )
    data = json.loads(body)
    assert data.get("provider") == {"order": ["a"]}
    assert data.get("reasoning") == {"effort": "minimal", "exclude": True}


def test_pass_through_unknown_level() -> None:
    client_or = DummyClient(config={"is_openrouter": True, "reasoning_effort": "turbo"})
    shim_or = BaseProviderShim(client_or)
    _m, _p, body_or, _h = shim_or.build_chat_request(
        [{"role": "user", "content": "hi"}], 100, None, None, False, "test-model", None
    )
    data_or = json.loads(body_or)
    assert data_or.get("reasoning") == {"effort": "turbo"}

    client_other = DummyClient(config={"is_openrouter": False, "reasoning_effort": "turbo"})
    shim_other = BaseProviderShim(client_other)
    _m, _p, body_other, _h = shim_other.build_chat_request(
        [{"role": "user", "content": "hi"}], 100, None, None, False, "test-model", None
    )
    data_other = json.loads(body_other)
    assert data_other.get("reasoning_effort") == "turbo"
