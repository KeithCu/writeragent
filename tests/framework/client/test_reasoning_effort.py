import json
from typing import Any
from unittest.mock import MagicMock, patch
import pytest

from plugin.framework.client import base_provider_shim
from plugin.framework.client.base_provider_shim import (
    BaseProviderShim,
    effort_payload,
    effort_retry_bodies,
)
from plugin.framework.client.google_shim import GoogleShim
from plugin.framework.client.grok_shim import GrokShim
from plugin.framework.client.llm_client import LlmClient
from plugin.framework.client.openai_shim import OllamaShim, OpenAIShim, TogetherShim
from plugin.framework.errors import NetworkError
from tests.doc_stubs import MockContext


@pytest.fixture(autouse=True)
def _clear_accepted_effort() -> Any:
    base_provider_shim._ACCEPTED_EFFORT.clear()
    base_provider_shim._EFFORT_UNSUPPORTED.clear()
    yield
    base_provider_shim._ACCEPTED_EFFORT.clear()
    base_provider_shim._EFFORT_UNSUPPORTED.clear()


class DummyClient:
    def __init__(self, config: dict[str, Any] | None = None, provider: str = "custom") -> None:
        self.config = config if config is not None else {}
        self._provider = provider

    def _get_provider(self) -> str:
        return self._provider

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
    for shim_cls, model, provider in [(GrokShim, "grok-2", "xai"), (OllamaShim, "llama3", "ollama")]:
        shim = shim_cls(DummyClient(config={"is_openrouter": False, "reasoning_effort": "xhigh"}, provider=provider))
        _method, _path, body, _headers = shim.build_chat_request(
            [{"role": "user", "content": "hi"}], 100, None, None, False, model, None
        )
        data = json.loads(body)
        assert data.get("reasoning_effort") == "xhigh"
        assert "reasoning" not in data

    # Base (custom) and Together pass xhigh through; a 400 retry steps it down
    for shim_cls, model, provider in [(BaseProviderShim, "test-model", "custom"), (TogetherShim, "meta-llama/Llama-3-70b", "together")]:
        shim = shim_cls(DummyClient(config={"is_openrouter": False, "reasoning_effort": "xhigh"}, provider=provider))
        _method, _path, body, _headers = shim.build_chat_request(
            [{"role": "user", "content": "hi"}], 100, None, None, False, model, None
        )
        data = json.loads(body)
        assert data.get("reasoning_effort") == "xhigh"
        assert "reasoning" not in data

    # Google clamps to high
    shim_google = GoogleShim(DummyClient(config={"is_openrouter": False, "reasoning_effort": "xhigh"}, provider="google"))
    _method, _path, body_g, _headers = shim_google.build_chat_request(
        [{"role": "user", "content": "hi"}], 100, None, None, False, "gemini-4-argon", None
    )
    assert json.loads(body_g).get("reasoning_effort") == "high"


def test_openai_reasoning_model_keeps_effort_and_max_completion_tokens() -> None:
    client = DummyClient(config={"is_openrouter": False, "reasoning_effort": "max"}, provider="openai")
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
    for prov in ("openrouter", "openai", "anthropic", "custom", "lmstudio", "deepseek", "groq", "ollama", "together"):
        frag = effort_payload(prov, "max")
        assert "max" in json.dumps(frag), prov
    assert effort_payload("openai", "xhigh") == {"reasoning_effort": "xhigh"}
    assert effort_payload("anthropic", "xhigh") == {"output_config": {"effort": "xhigh"}}
    assert effort_payload("xai", "xhigh") == {"reasoning_effort": "xhigh"}
    assert effort_payload("mistral", "low") == {"reasoning_effort": "low"}
    assert effort_payload("mistral", "medium") == {"reasoning_effort": "medium"}
    assert effort_payload("mistral", "max") == {"reasoning_effort": "max"}


def test_effort_payload_nearest_lower_word() -> None:
    assert effort_payload("google", "xhigh") == {"reasoning_effort": "high"}
    assert effort_payload("google", "max") == {"reasoning_effort": "high"}
    assert effort_payload("xai", "max") == {"reasoning_effort": "xhigh"}
    assert effort_payload("mistral", "xhigh") == {"reasoning_effort": "high"}


def test_g38f_and_argon_on_gemini() -> None:
    # g38f = gemini-3.8-flash at medium; Argon = gemini-4-argon. Both accept low/medium/high
    # and reject minimal. Neither can turn thinking off.
    assert effort_payload("google", "medium") == {"reasoning_effort": "medium"}
    assert effort_payload("google", "high") == {"reasoning_effort": "high"}
    assert effort_payload("google", "minimal") == {"reasoning_effort": "low"}
    for model in ("gemini-3.8-flash", "gemini-4-argon"):
        shim = GoogleShim(DummyClient(config={"is_openrouter": False, "reasoning_effort": "medium"}, provider="google"))
        _m, _p, body, _h = shim.build_chat_request([{"role": "user", "content": "hi"}], 100, None, None, False, model, None)
        data = json.loads(body)
        assert data["reasoning_effort"] == "medium"
        assert "reasoning" not in data


def test_effort_payload_omit() -> None:
    for lvl in ["", "default", None, "   ", "turbo", 3]:
        assert effort_payload("openrouter", lvl) == {}
        assert effort_payload("anthropic", lvl) == {}


def test_effort_payload_field_placement() -> None:
    assert effort_payload("openrouter", "high") == {"reasoning": {"effort": "high"}}
    assert effort_payload("anthropic", "high") == {"output_config": {"effort": "high"}}
    for prov in ("openai", "google", "xai", "mistral", "deepseek", "groq", "ollama", "custom"):
        assert effort_payload(prov, "high") == {"reasoning_effort": "high"}, prov


def test_effort_payload_grammar_sentinel() -> None:
    assert effort_payload("openrouter", "minimal") == {"reasoning": {"effort": "minimal"}}
    assert effort_payload("openai", "minimal") == {"reasoning_effort": "minimal"}
    assert effort_payload("anthropic", "minimal") == {"output_config": {"effort": "low"}}
    assert effort_payload("xai", "minimal") == {"reasoning_effort": "low"}
    assert effort_payload("custom", "minimal") == {"reasoning_effort": "low"}
    # Only the grammar sentinel turns Mistral thinking off.
    assert effort_payload("mistral", "minimal") == {"reasoning_effort": "none"}


# --- effort_retry_bodies unit tests ---


def _efforts(provider: str, body: dict[str, Any]) -> list[Any]:
    out = []
    for raw in effort_retry_bodies(provider, json.dumps(body).encode("utf-8")):
        data = json.loads(raw)
        out.append(data.get("reasoning_effort") or (data.get("output_config") or {}).get("effort") or (data.get("reasoning") or {}).get("effort"))
    return out


def test_retry_steps_down_then_up_then_drops() -> None:
    assert _efforts("openai", {"model": "o3", "reasoning_effort": "high"}) == ["medium", "low", "xhigh", "max", None]
    assert _efforts("anthropic", {"output_config": {"effort": "max"}}) == ["xhigh", "high", "medium", "low", None]
    assert _efforts("google", {"reasoning_effort": "low"}) == ["medium", "high", None]


def test_retry_never_turns_mistral_thinking_off() -> None:
    # Mistral Small 4 / Medium 3.5 accept only none|high: a rejected low or medium moves up to high.
    assert _efforts("mistral", {"reasoning_effort": "low"}) == ["medium", "high", "max", None]
    assert _efforts("mistral", {"reasoning_effort": "medium"}) == ["low", "high", "max", None]
    # Grammar's "none" is rejected by Mistral Large 4 and zai-glm-5-3; low is next.
    assert _efforts("mistral", {"reasoning_effort": "none"})[0] == "low"


def test_retry_body_shapes() -> None:
    bodies = effort_retry_bodies("openrouter", json.dumps({"model": "x", "reasoning": {"effort": "high", "exclude": True}}))
    first, last = json.loads(bodies[0]), json.loads(bodies[-1])
    assert first["reasoning"] == {"effort": "medium", "exclude": True}
    assert last["reasoning"] == {"exclude": True}

    last_a = json.loads(effort_retry_bodies("anthropic", json.dumps({"output_config": {"effort": "low"}}))[-1])
    assert "output_config" not in last_a

    # Unknown sent word: only the drop body.
    assert _efforts("custom", {"reasoning_effort": "turbo"}) == [None]
    assert effort_retry_bodies("openai", json.dumps({"model": "o3", "messages": []})) == []
    assert effort_retry_bodies("openai", b"not-json") == []


# --- HTTP 400 retry tests ---


_EFFORT_400 = NetworkError("HTTP Error 400: Invalid reasoning_effort for this model", code="HTTP_ERROR", details={"status": 400, "url": "/v1/chat/completions"})
_OK = ("ok", {"choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}]})


def _effort_400_http_response() -> MagicMock:
    response = MagicMock()
    response.status = 400
    response.reason = "Bad Request"
    response.getheader.return_value = None
    response.read.return_value = b'{"error":"Unsupported parameter: reasoning_effort"}'
    return response


def _ok_sync_http_response() -> MagicMock:
    response = MagicMock()
    response.status = 200
    response.read.return_value = json.dumps(
        {"choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}]}
    ).encode()
    response.getheader.return_value = "application/json"
    return response


def test_400_retry_sync_success_on_second_attempt() -> None:
    client = LlmClient(
        {"endpoint": "https://api.openai.com", "api_key": "sk-test", "model": "o3", "reasoning_effort": "high"},
        MockContext(),
    )
    with patch.object(client._transport, "send", side_effect=[_effort_400_http_response(), _ok_sync_http_response()]) as mock_send:
        res = client.request_with_tools([{"role": "user", "content": "hi"}], stream=False)
        assert res["content"] == "ok"
        assert mock_send.call_count == 2
        second_body = mock_send.call_args_list[1][0][2]
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


def test_400_retry_exhausts_then_raises() -> None:
    client = LlmClient(
        {"endpoint": "https://api.openai.com", "api_key": "sk-test", "model": "o3", "reasoning_effort": "high"},
        MockContext(),
    )
    # high, then medium, low, xhigh, max, then no field: six sends, then raise.
    # This fixture tests the value walk ladder ("Unsupported parameter: reasoning_effort"),
    # not the parameter phrase ("The model does not support the effort parameter.").
    with patch.object(client._transport, "send", side_effect=[_effort_400_http_response()] * 6) as mock_send:
        with pytest.raises(NetworkError, match="reasoning_effort"):
            client.request_with_tools([{"role": "user", "content": "hi"}], stream=False)
        assert mock_send.call_count == 6
        assert "reasoning_effort" not in json.loads(mock_send.call_args_list[-1][0][2])


def test_400_no_effort_field_in_body_does_not_retry() -> None:
    # Model gpt-4o sends no reasoning effort
    client = LlmClient(
        {"endpoint": "https://api.openai.com", "api_key": "sk-test", "model": "gpt-4o"},
        MockContext(),
    )

    with patch.object(client._transport, "send", side_effect=_effort_400_http_response()) as mock_send:
        with pytest.raises(NetworkError):
            client.request_with_tools([{"role": "user", "content": "hi"}], stream=False)
        assert mock_send.call_count == 1


def test_400_retry_mistral_low_moves_up_to_high() -> None:
    # Mistral Small 4 accepts none|high only: low and medium are rejected, high works.
    client = LlmClient(
        {"endpoint": "https://api.mistral.ai", "api_key": "k", "model": "mistral-small-2603", "reasoning_effort": "low"},
        MockContext(),
    )
    with patch.object(
        client._transport,
        "send",
        side_effect=[_effort_400_http_response(), _effort_400_http_response(), _ok_sync_http_response()],
    ) as mock_send:
        res = client.request_with_tools([{"role": "user", "content": "hi"}], stream=False)
        assert res["content"] == "ok"
        sent = [json.loads(c[0][2]).get("reasoning_effort") for c in mock_send.call_args_list]
        assert sent == ["low", "medium", "high"]


# --- Accepted effort memo ---


def _mistral_client(model: str = "mistral-small-2603") -> LlmClient:
    return LlmClient({"endpoint": "https://api.mistral.ai", "api_key": "k", "model": model, "reasoning_effort": "low"}, MockContext())


def test_accepted_effort_remembered_sync() -> None:
    client = _mistral_client()
    with patch.object(
        client._transport,
        "send",
        side_effect=[_effort_400_http_response(), _effort_400_http_response(), _ok_sync_http_response()],
    ) as first:
        client.request_with_tools([{"role": "user", "content": "hi"}], stream=False)
    assert [json.loads(c[0][2]).get("reasoning_effort") for c in first.call_args_list] == ["low", "medium", "high"]

    # Same endpoint, model and level: the accepted value goes out first, one request.
    with patch.object(client._transport, "send", side_effect=[_ok_sync_http_response()]) as second:
        assert client.request_with_tools([{"role": "user", "content": "again"}], stream=False)["content"] == "ok"
    assert second.call_count == 1
    assert json.loads(second.call_args_list[0][0][2])["reasoning_effort"] == "high"

    # Another model on the same endpoint starts from its own level.
    other = _mistral_client("mistral-large-2604")
    with patch.object(other._transport, "send", side_effect=[_ok_sync_http_response()]) as third:
        other.request_with_tools([{"role": "user", "content": "hi"}], stream=False)
    assert json.loads(third.call_args_list[0][0][2])["reasoning_effort"] == "low"


def test_accepted_no_effort_remembered() -> None:
    client = _mistral_client()
    # low, medium, high, max rejected; the body without effort works.
    with patch.object(
        client._transport,
        "send",
        side_effect=[_effort_400_http_response()] * 4 + [_ok_sync_http_response()],
    ) as first:
        client.request_with_tools([{"role": "user", "content": "hi"}], stream=False)
    assert first.call_count == 5
    with patch.object(client._transport, "send", side_effect=[_ok_sync_http_response()]) as second:
        client.request_with_tools([{"role": "user", "content": "hi"}], stream=False)
    assert second.call_count == 1
    assert "reasoning_effort" not in json.loads(second.call_args_list[0][0][2])

    # A separate client on the same endpoint and model with reasoning_effort="high"
    # is not affected by the per-level omit recorded for "low", and sends with "high".
    second_client = LlmClient(
        {"endpoint": "https://api.mistral.ai", "api_key": "k", "model": "mistral-small-2603", "reasoning_effort": "high"},
        MockContext(),
    )
    with patch.object(second_client._transport, "send", side_effect=[_ok_sync_http_response()]) as third:
        second_client.request_with_tools([{"role": "user", "content": "hi"}], stream=False)
    assert third.call_count == 1
    assert json.loads(third.call_args_list[0][0][2]).get("reasoning_effort") == "high"


def test_failed_dance_records_nothing() -> None:
    client = _mistral_client()
    with patch.object(client._transport, "send", side_effect=[_effort_400_http_response()] * 5):
        with pytest.raises(NetworkError):
            client.request_with_tools([{"role": "user", "content": "hi"}], stream=False)
    assert base_provider_shim._ACCEPTED_EFFORT == {}


def test_accepted_effort_remembered_streaming() -> None:
    client = _mistral_client()
    body = json.dumps({"model": "mistral-small-2603", "messages": [], "stream": True, "reasoning_effort": "low"}).encode("utf-8")

    def ok_response() -> MagicMock:
        response = MagicMock()
        response.status = 200
        response.getheader.return_value = None
        response.read.return_value = b""
        lines = [b'data: {"choices": [{"delta": {"content": "ok"}, "finish_reason": "stop"}]}\n', b"data: [DONE]\n"]
        response.__iter__ = lambda _self: iter(lines)
        return response

    def effort_400_response() -> MagicMock:
        response = MagicMock()
        response.status = 400
        response.reason = "Bad Request"
        response.getheader.return_value = None
        response.read.return_value = b'{"error":"reasoning effort invalid"}'
        return response

    with patch.object(client._transport, "send", side_effect=[effort_400_response(), effort_400_response(), ok_response()]) as first:
        parts: list[str] = []
        client._run_streaming_loop("POST", "/v1/chat/completions", body, {}, parts.append)
    assert parts == ["ok"]
    assert [json.loads(c[0][2]).get("reasoning_effort") for c in first.call_args_list] == ["low", "medium", "high"]

    with patch.object(client._transport, "send", side_effect=[ok_response()]) as second:
        client._run_streaming_loop("POST", "/v1/chat/completions", body, {}, lambda _t: None)
    assert second.call_count == 1
    assert json.loads(second.call_args_list[0][0][2])["reasoning_effort"] == "high"


def _unsupported_effort_400_http_response() -> MagicMock:
    response = MagicMock()
    response.status = 400
    response.reason = "Bad Request"
    response.getheader.return_value = None
    response.read.return_value = b'{"error":{"message":"The model does not support the effort parameter."}}'
    return response


def test_effort_parameter_unsupported_phrase() -> None:
    # Verifies that only the specific phrase for unsupported effort parameter triggers the immediate omit.
    assert base_provider_shim.effort_parameter_unsupported("The model does not support the effort parameter.")
    assert base_provider_shim.effort_parameter_unsupported("THE MODEL DOES NOT SUPPORT THE EFFORT PARAMETER.")
    assert not base_provider_shim.effort_parameter_unsupported("Unsupported parameter: reasoning_effort")
    assert not base_provider_shim.effort_parameter_unsupported("reasoning effort invalid")
    assert not base_provider_shim.effort_parameter_unsupported("Invalid reasoning_effort for this model")
    assert not base_provider_shim.effort_parameter_unsupported("")


def test_effort_omit_body_shapes() -> None:
    # Anthropic output_config with only effort is pruned completely.
    anthropic_body = json.dumps({"model": "claude-3-5-sonnet", "output_config": {"effort": "high"}})
    omitted_anthropic = base_provider_shim.effort_omit_body(anthropic_body)
    assert omitted_anthropic is not None
    data_anthropic = json.loads(omitted_anthropic)
    assert "output_config" not in data_anthropic

    # OpenRouter reasoning container preserves sibling keys when effort is dropped.
    openrouter_body = json.dumps({"model": "anthropic/claude-3-5-sonnet", "reasoning": {"effort": "high", "exclude": True}})
    omitted_openrouter = base_provider_shim.effort_omit_body(openrouter_body)
    assert omitted_openrouter is not None
    data_openrouter = json.loads(omitted_openrouter)
    assert data_openrouter.get("reasoning") == {"exclude": True}

    # Bodies without an effort parameter return None.
    no_effort_body = json.dumps({"model": "gpt-4o", "messages": []})
    assert base_provider_shim.effort_omit_body(no_effort_body) is None


def test_unsupported_effort_parameter_omits_once_and_applies_to_other_levels() -> None:
    # When a model explicitly does not support effort, omit immediately on retry and record for all levels.
    client = LlmClient(
        {"endpoint": "https://api.openai.com", "api_key": "sk-test", "model": "o3", "reasoning_effort": "high"},
        MockContext(),
    )
    with patch.object(client._transport, "send", side_effect=[_unsupported_effort_400_http_response(), _ok_sync_http_response()]) as mock_send:
        res = client.request_with_tools([{"role": "user", "content": "hi"}], stream=False)
        assert res["content"] == "ok"
    assert mock_send.call_count == 2
    assert json.loads(mock_send.call_args_list[0][0][2])["reasoning_effort"] == "high"
    assert "reasoning_effort" not in json.loads(mock_send.call_args_list[1][0][2])

    assert base_provider_shim._ACCEPTED_EFFORT == {}
    assert ("https://api.openai.com", "o3") in base_provider_shim._EFFORT_UNSUPPORTED

    # Same endpoint and model with a different level ("max") omits effort without retrying.
    client2 = LlmClient(
        {"endpoint": "https://api.openai.com", "api_key": "sk-test", "model": "o3", "reasoning_effort": "max"},
        MockContext(),
    )
    with patch.object(client2._transport, "send", side_effect=[_ok_sync_http_response()]) as mock_send2:
        res2 = client2.request_with_tools([{"role": "user", "content": "hi"}], stream=False)
        assert res2["content"] == "ok"
    assert mock_send2.call_count == 1
    assert "reasoning_effort" not in json.loads(mock_send2.call_args_list[0][0][2])

    # A different model on the same endpoint is not affected by the unsupported memo.
    client3 = LlmClient(
        {"endpoint": "https://api.openai.com", "api_key": "sk-test", "model": "o1", "reasoning_effort": "low"},
        MockContext(),
    )
    with patch.object(client3._transport, "send", side_effect=[_ok_sync_http_response()]) as mock_send3:
        res3 = client3.request_with_tools([{"role": "user", "content": "hi"}], stream=False)
        assert res3["content"] == "ok"
    assert mock_send3.call_count == 1
    assert json.loads(mock_send3.call_args_list[0][0][2])["reasoning_effort"] == "low"


def test_unsupported_effort_parameter_remembered_streaming() -> None:
    # Streaming path also omits effort on the unsupported phrase and records for subsequent sends.
    client = LlmClient(
        {"endpoint": "https://api.openai.com", "api_key": "sk-test", "model": "o3", "reasoning_effort": "high"},
        MockContext(),
    )
    body = json.dumps({"model": "o3", "messages": [], "stream": True, "reasoning_effort": "high"}).encode("utf-8")

    def ok_response() -> MagicMock:
        response = MagicMock()
        response.status = 200
        response.getheader.return_value = None
        response.read.return_value = b""
        lines = [b'data: {"choices": [{"delta": {"content": "ok"}, "finish_reason": "stop"}]}\n', b"data: [DONE]\n"]
        response.__iter__ = lambda _self: iter(lines)
        return response

    with patch.object(
        client._transport,
        "send",
        side_effect=[_unsupported_effort_400_http_response(), ok_response()],
    ) as first:
        parts: list[str] = []
        client._run_streaming_loop("POST", "/v1/chat/completions", body, {}, parts.append)
    assert parts == ["ok"]
    assert first.call_count == 2
    assert json.loads(first.call_args_list[0][0][2])["reasoning_effort"] == "high"
    assert "reasoning_effort" not in json.loads(first.call_args_list[1][0][2])

    assert base_provider_shim._ACCEPTED_EFFORT == {}
    assert ("https://api.openai.com", "o3") in base_provider_shim._EFFORT_UNSUPPORTED

    # A subsequent streaming request on the same endpoint and model omits effort on the first send
    client2 = LlmClient(
        {"endpoint": "https://api.openai.com", "api_key": "sk-test", "model": "o3", "reasoning_effort": "max"},
        MockContext(),
    )
    body2 = json.dumps({"model": "o3", "messages": [], "stream": True, "reasoning_effort": "max"}).encode("utf-8")
    with patch.object(client2._transport, "send", side_effect=[ok_response()]) as second:
        client2._run_streaming_loop("POST", "/v1/chat/completions", body2, {}, lambda _t: None)
    assert second.call_count == 1
    assert "reasoning_effort" not in json.loads(second.call_args_list[0][0][2])
