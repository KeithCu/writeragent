import json
from typing import Any
from unittest.mock import patch
import pytest

from plugin.framework.client.base_provider_shim import (
    BaseProviderShim,
    effort_payload,
    next_lower_effort_body,
)
from plugin.framework.client.google_shim import GoogleShim
from plugin.framework.client.grok_shim import GrokShim
from plugin.framework.client.llm_client import LlmClient
from plugin.framework.client.openai_shim import OllamaShim, OpenAIShim, TogetherShim
from plugin.framework.errors import NetworkError
from tests.doc_stubs import MockContext


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


def test_other_host_top_level() -> None:
    # Grok and Ollama keep xhigh
    for shim_cls, model in [(GrokShim, "grok-2"), (OllamaShim, "llama3")]:
        shim = shim_cls(DummyClient(config={"is_openrouter": False, "reasoning_effort": "xhigh"}))
        _method, _path, body, _headers = shim.build_chat_request(
            [{"role": "user", "content": "hi"}], 100, None, None, False, model, None
        )
        data = json.loads(body)
        assert data.get("reasoning_effort") == "xhigh"
        assert "reasoning" not in data

    # Base (custom) and Together clamp to high
    for shim_cls, model in [(BaseProviderShim, "test-model"), (TogetherShim, "meta-llama/Llama-3-70b")]:
        shim = shim_cls(DummyClient(config={"is_openrouter": False, "reasoning_effort": "xhigh"}))
        _method, _path, body, _headers = shim.build_chat_request(
            [{"role": "user", "content": "hi"}], 100, None, None, False, model, None
        )
        data = json.loads(body)
        assert data.get("reasoning_effort") == "high"
        assert "reasoning" not in data

    # Google clamps to high
    shim_google = GoogleShim(DummyClient(config={"is_openrouter": False, "reasoning_effort": "xhigh"}))
    _method, _path, body_g, _headers = shim_google.build_chat_request(
        [{"role": "user", "content": "hi"}], 100, None, None, False, "gemini-2.0-flash", None
    )
    assert json.loads(body_g).get("reasoning_effort") == "high"

    # OpenAI non-reasoning model (gpt-4o) omits effort
    shim_openai = OpenAIShim(DummyClient(config={"is_openrouter": False, "reasoning_effort": "xhigh"}))
    _method, _path, body_o, _headers = shim_openai.build_chat_request(
        [{"role": "user", "content": "hi"}], 100, None, None, False, "gpt-4o", None
    )
    data_o = json.loads(body_o)
    assert "reasoning_effort" not in data_o
    assert "reasoning" not in data_o


def test_openai_reasoning_model_keeps_effort_and_max_completion_tokens() -> None:
    client = DummyClient(config={"is_openrouter": False, "reasoning_effort": "max"})
    shim = OpenAIShim(client)
    _method, _path, body, _headers = shim.build_chat_request(
        [{"role": "user", "content": "hi"}], 100, 0.2, None, False, "o3", None
    )
    data = json.loads(body)
    assert data.get("reasoning_effort") == "high"
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

    # Other host (BaseProviderShim + DummyClient defaults to custom, whose lowest documented level is low)
    client_other = DummyClient(config={"is_openrouter": False, "reasoning_effort": "high"})
    shim_other = BaseProviderShim(client_other)
    _method, _path, body, _headers = shim_other.build_chat_request(
        [{"role": "user", "content": "hi"}], 100, None, None, False, "test-model", None, chat_extra=grammar_extra
    )
    data_other = json.loads(body)
    assert data_other.get("reasoning_effort") == "low"
    assert "reasoning" not in data_other

    # Caller's dict must NOT be mutated
    assert grammar_extra == {"reasoning": {"effort": "minimal"}}


def test_grammar_extra_on_non_openrouter_is_not_a_reasoning_object() -> None:
    client = DummyClient(config={"is_openrouter": False})
    shim = BaseProviderShim(client)
    grammar_extra = {"reasoning": {"effort": "minimal"}}
    _method, _path, body, _headers = shim.build_chat_request(
        [{"role": "user", "content": "hi"}], 100, None, None, False, "test-model", None, chat_extra=grammar_extra
    )
    data = json.loads(body)
    assert "reasoning" not in data
    assert data.get("reasoning_effort") == "low"


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


def test_unknown_level_omitted() -> None:
    client_or = DummyClient(config={"is_openrouter": True, "reasoning_effort": "turbo"})
    shim_or = BaseProviderShim(client_or)
    _m, _p, body_or, _h = shim_or.build_chat_request(
        [{"role": "user", "content": "hi"}], 100, None, None, False, "test-model", None
    )
    data_or = json.loads(body_or)
    assert "reasoning" not in data_or
    assert "reasoning_effort" not in data_or

    client_other = DummyClient(config={"is_openrouter": False, "reasoning_effort": "turbo"})
    shim_other = BaseProviderShim(client_other)
    _m, _p, body_other, _h = shim_other.build_chat_request(
        [{"role": "user", "content": "hi"}], 100, None, None, False, "test-model", None
    )
    data_other = json.loads(body_other)
    assert "reasoning" not in data_other
    assert "reasoning_effort" not in data_other


# --- Direct effort_payload unit tests ---


def test_effort_payload_passthrough() -> None:
    assert effort_payload("openrouter", "any", "max") == {"reasoning": {"effort": "max"}}
    assert effort_payload("openai", "gpt-6-astra", "max") == {"reasoning_effort": "max"}
    assert effort_payload("openai", "gpt-5.5", "xhigh") == {"reasoning_effort": "xhigh"}
    assert effort_payload("openai", "o3", "high") == {"reasoning_effort": "high"}
    assert effort_payload("anthropic", "claude-opus-4-7", "xhigh") == {"output_config": {"effort": "xhigh"}}
    assert effort_payload("anthropic", "claude-opus-4-6", "max") == {"output_config": {"effort": "max"}}
    assert effort_payload("xai", "grok-4.7", "xhigh") == {"reasoning_effort": "xhigh"}
    assert effort_payload("deepseek", "deepseek-v4-pro", "medium") == {"reasoning_effort": "medium"}
    assert effort_payload("ollama", "gpt-oss", "xhigh") == {"reasoning_effort": "xhigh"}
    assert effort_payload("groq", "meta-models/muse-spark-open", "xhigh") == {"reasoning_effort": "xhigh"}


def test_effort_payload_step_down_never_up() -> None:
    # OpenAI
    assert effort_payload("openai", "gpt-5.5", "max") == {"reasoning_effort": "xhigh"}
    assert effort_payload("openai", "o3", "max") == {"reasoning_effort": "high"}
    assert effort_payload("openai", "o3", "xhigh") == {"reasoning_effort": "high"}
    assert effort_payload("openai", "gpt-5", "low") == {"reasoning_effort": "low"}

    # Anthropic
    assert effort_payload("anthropic", "claude-opus-4-6", "xhigh") == {"output_config": {"effort": "high"}}
    assert effort_payload("anthropic", "claude-sonnet-4-6", "xhigh") == {"output_config": {"effort": "high"}}
    assert effort_payload("anthropic", "claude-mythos-preview", "xhigh") == {"output_config": {"effort": "high"}}
    assert effort_payload("anthropic", "claude-opus-4-5", "xhigh") == {"output_config": {"effort": "high"}}
    assert effort_payload("anthropic", "claude-opus-4-5", "max") == {"output_config": {"effort": "high"}}

    # Google
    assert effort_payload("google", "gemini-4-argon", "xhigh") == {"reasoning_effort": "high"}
    assert effort_payload("google", "gemini-4-argon", "max") == {"reasoning_effort": "high"}
    assert effort_payload("google", "gemini-4-argon", "minimal") == {"reasoning_effort": "low"}

    # xAI
    assert effort_payload("xai", "grok-4.5", "max") == {"reasoning_effort": "xhigh"}

    # Groq
    assert effort_payload("groq", "openai/gpt-oss-120b", "max") == {"reasoning_effort": "high"}

    # Mistral
    assert effort_payload("mistral", "mistral-large-4", "xhigh") == {"reasoning_effort": "high"}
    assert effort_payload("mistral", "mistral-small-2603", "low") == {"reasoning_effort": "none"}
    assert effort_payload("mistral", "mistral-small-2603", "medium") == {"reasoning_effort": "none"}
    assert effort_payload("mistral", "mistral-small-2603", "xhigh") == {"reasoning_effort": "high"}
    assert effort_payload("mistral", "mistral-small-2603", "high") == {"reasoning_effort": "high"}
    assert effort_payload("mistral", "mistral-medium-3-5", "medium") == {"reasoning_effort": "none"}
    assert effort_payload("mistral", "zai-glm-5-3", "medium") == {"reasoning_effort": "low"}
    assert effort_payload("mistral", "zai-glm-5-3", "xhigh") == {"reasoning_effort": "high"}
    assert effort_payload("mistral", "zai-glm-5-3", "max") == {"reasoning_effort": "max"}

    # Together, custom, lmstudio
    assert effort_payload("together", "meta-llama/Llama-3-70b", "xhigh") == {"reasoning_effort": "high"}
    assert effort_payload("custom", "whatever", "max") == {"reasoning_effort": "high"}
    assert effort_payload("lmstudio", "gpt-oss", "max") == {"reasoning_effort": "high"}


def test_effort_payload_omit() -> None:
    # Blank, default, None, unknown words
    for lvl in ["", "default", None, "   ", "turbo"]:
        assert effort_payload("openrouter", "any-model", lvl) == {}

    # Non-reasoning OpenAI
    assert effort_payload("openai", "gpt-4o", "high") == {}
    assert effort_payload("openai", "gpt-5-chat-latest", "high") == {}
    assert effort_payload("openai", "gpt-4.1", "low") == {}
    assert effort_payload("openai", "gpt-4o", "minimal") == {}

    # Pre-4.5 Anthropic
    assert effort_payload("anthropic", "claude-sonnet-4-5", "high") == {}
    assert effort_payload("anthropic", "claude-3-7-sonnet-20250219", "xhigh") == {}
    assert effort_payload("anthropic", "claude-3-7-sonnet-20250219", "minimal") == {}
    assert effort_payload("anthropic", "claude-opus-4-7", "") == {}

    # Unsupported Groq & Mistral
    assert effort_payload("groq", "llama-3.3-70b", "low") == {}
    assert effort_payload("mistral", "magistral-small", "high") == {}


def test_effort_payload_field_placement() -> None:
    # Same level "high", assert exact field placement
    assert effort_payload("openrouter", "any", "high") == {"reasoning": {"effort": "high"}}
    assert effort_payload("anthropic", "claude-sonnet-5", "high") == {"output_config": {"effort": "high"}}
    assert effort_payload("google", "gemini-4", "high") == {"reasoning_effort": "high"}
    assert effort_payload("xai", "grok-4.7", "high") == {"reasoning_effort": "high"}
    assert effort_payload("deepseek", "v3", "high") == {"reasoning_effort": "high"}
    assert effort_payload("groq", "gpt-oss", "high") == {"reasoning_effort": "high"}
    assert effort_payload("ollama", "qwen", "high") == {"reasoning_effort": "high"}
    assert effort_payload("custom", "local", "high") == {"reasoning_effort": "high"}


def test_effort_payload_grammar_sentinel() -> None:
    assert effort_payload("openrouter", "m", "minimal") == {"reasoning": {"effort": "minimal"}}
    assert effort_payload("openai", "gpt-6", "minimal") == {"reasoning_effort": "minimal"}
    assert effort_payload("openai", "o3", "minimal") == {"reasoning_effort": "minimal"}
    assert effort_payload("anthropic", "claude-opus-4-5", "minimal") == {"output_config": {"effort": "low"}}
    assert effort_payload("xai", "grok-4.5", "minimal") == {"reasoning_effort": "low"}
    assert effort_payload("google", "gemini-2.5", "minimal") == {"reasoning_effort": "low"}
    assert effort_payload("deepseek", "v3", "minimal") == {"reasoning_effort": "low"}
    assert effort_payload("ollama", "m", "minimal") == {"reasoning_effort": "low"}
    assert effort_payload("groq", "gpt-oss", "minimal") == {"reasoning_effort": "low"}
    assert effort_payload("custom", "m", "minimal") == {"reasoning_effort": "low"}
    assert effort_payload("mistral", "mistral-small-2603", "minimal") == {"reasoning_effort": "none"}
    assert effort_payload("mistral", "mistral-large-4", "minimal") == {"reasoning_effort": "low"}
    assert effort_payload("groq", "llama-3.3-70b", "minimal") == {}
    assert effort_payload("anthropic", "claude-sonnet-4-5", "minimal") == {}


def test_effort_payload_prefix_and_slash_ids() -> None:
    assert effort_payload("openai", "openai/gpt-5.5", "max") == {"reasoning_effort": "xhigh"}
    assert effort_payload("openai", "ft:gpt-6-mini:org:name:id", "max") == {"reasoning_effort": "max"}
    assert effort_payload("groq", "meta-models/muse-spark-open", "max") == {"reasoning_effort": "xhigh"}


# --- next_lower_effort_body unit tests ---


def test_next_lower_effort_body() -> None:
    # OpenAI o3: high -> medium -> minimal -> removed
    b1 = json.dumps({"model": "o3", "reasoning_effort": "high"}).encode("utf-8")
    b2 = next_lower_effort_body("openai", b1)
    assert b2 is not None
    assert json.loads(b2)["reasoning_effort"] == "medium"

    b3 = next_lower_effort_body("openai", b2)
    assert b3 is not None
    assert json.loads(b3)["reasoning_effort"] == "low"

    b4 = next_lower_effort_body("openai", b3)
    assert b4 is not None
    assert json.loads(b4)["reasoning_effort"] == "minimal"

    b5 = next_lower_effort_body("openai", b4)
    assert b5 is not None
    assert "reasoning_effort" not in json.loads(b5)

    # Google gemini-4-argon: low -> removed
    bg1 = json.dumps({"model": "gemini-4-argon", "reasoning_effort": "low"}).encode("utf-8")
    bg2 = next_lower_effort_body("google", bg1)
    assert bg2 is not None
    assert "reasoning_effort" not in json.loads(bg2)

    # OpenRouter: effort stepped in reasoning dict, exclude kept
    bor1 = json.dumps({"model": "x", "reasoning": {"effort": "high", "exclude": True}}).encode("utf-8")
    bor2 = next_lower_effort_body("openrouter", bor1)
    assert bor2 is not None
    data_or = json.loads(bor2)
    assert data_or["reasoning"]["effort"] == "medium"
    assert data_or["reasoning"]["exclude"] is True

    # Anthropic: max -> high on claude-opus-4-6
    ba1 = json.dumps({"model": "claude-opus-4-6", "output_config": {"effort": "max"}}).encode("utf-8")
    ba2 = next_lower_effort_body("anthropic", ba1)
    assert ba2 is not None
    assert json.loads(ba2)["output_config"]["effort"] == "high"

    # No effort field -> None
    b_no_effort = json.dumps({"model": "o3", "messages": []}).encode("utf-8")
    assert next_lower_effort_body("openai", b_no_effort) is None

    # Already removed or non-JSON -> None
    assert next_lower_effort_body("openai", b5) is None
    assert next_lower_effort_body("openai", b"not-json") is None


# --- HTTP 400 retry tests ---


def test_400_retry_sync_success_on_second_attempt() -> None:
    client = LlmClient(
        {"endpoint": "https://api.openai.com", "api_key": "sk-test", "model": "o3", "reasoning_effort": "high"},
        MockContext(),
    )
    err_400 = NetworkError(
        "HTTP Error 400 from AI Provider: Bad Request. Unsupported parameter: reasoning_effort",
        code="HTTP_ERROR",
        details={"status": 400, "url": "/v1/chat/completions"},
    )
    ok_response = ("ok", {"choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}]})

    with patch.object(client, "_exchange_json", side_effect=[err_400, ok_response]) as mock_exchange:
        res = client.request_with_tools([{"role": "user", "content": "hi"}], stream=False)
        assert res["content"] == "ok"
        assert mock_exchange.call_count == 2
        # Check that the second call stepped down effort to medium
        _method, _path, second_body, _headers = mock_exchange.call_args_list[1][0]
        data = json.loads(second_body)
        assert data.get("reasoning_effort") == "medium"


def test_400_non_effort_does_not_retry() -> None:
    client = LlmClient(
        {"endpoint": "https://api.openai.com", "api_key": "sk-test", "model": "o3", "reasoning_effort": "high"},
        MockContext(),
    )
    err_temp = NetworkError(
        "HTTP Error 400: bad temperature",
        code="HTTP_ERROR",
        details={"status": 400, "url": "/v1/chat/completions"},
    )

    with patch.object(client, "_exchange_json", side_effect=err_temp) as mock_exchange:
        with pytest.raises(NetworkError, match="bad temperature"):
            client.request_with_tools([{"role": "user", "content": "hi"}], stream=False)
        assert mock_exchange.call_count == 1


def test_400_retry_fails_twice_and_raises() -> None:
    client = LlmClient(
        {"endpoint": "https://api.openai.com", "api_key": "sk-test", "model": "o3", "reasoning_effort": "high"},
        MockContext(),
    )
    err_400 = NetworkError(
        "HTTP Error 400: Unsupported parameter: reasoning_effort",
        code="HTTP_ERROR",
        details={"status": 400, "url": "/v1/chat/completions"},
    )

    with patch.object(client, "_exchange_json", side_effect=[err_400, err_400]) as mock_exchange:
        with pytest.raises(NetworkError, match="reasoning_effort"):
            client.request_with_tools([{"role": "user", "content": "hi"}], stream=False)
        assert mock_exchange.call_count == 2


def test_400_no_effort_field_in_body_does_not_retry() -> None:
    # Model gpt-4o sends no reasoning effort
    client = LlmClient(
        {"endpoint": "https://api.openai.com", "api_key": "sk-test", "model": "gpt-4o"},
        MockContext(),
    )
    err_400 = NetworkError(
        "HTTP Error 400: effort error message",
        code="HTTP_ERROR",
        details={"status": 400, "url": "/v1/chat/completions"},
    )

    with patch.object(client, "_exchange_json", side_effect=err_400) as mock_exchange:
        with pytest.raises(NetworkError, match="effort error"):
            client.request_with_tools([{"role": "user", "content": "hi"}], stream=False)
        assert mock_exchange.call_count == 1
