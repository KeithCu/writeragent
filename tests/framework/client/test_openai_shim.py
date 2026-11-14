import pytest
import json
from plugin.framework.client.base_provider_shim import BaseProviderShim
from plugin.framework.client.grok_shim import GrokShim
from plugin.framework.client.openai_shim import OpenAIShim, OpenRouterShim, get_provider_shim_class


class _DummyClient:
    def __init__(self, provider: str = "openai", headers: dict[str, str] | None = None, config: dict | None = None) -> None:
        self._provider = provider
        self._custom_headers = headers if headers is not None else {"Authorization": "Bearer test"}
        self.config = config or {}

    def _get_provider(self) -> str:
        return self._provider

    def _endpoint(self) -> str:
        return "http://test"

    def _api_path(self) -> str:
        return "/v1"

    def _headers(self) -> dict[str, str]:
        return dict(self._custom_headers)


def test_openai_shim_build_image_request():
    shim = OpenAIShim(_DummyClient())

    # test gpt-image drops response_format and size
    _method, _path, body, _headers = shim.build_image_request("prompt", "gpt-image-alpha", 1024, 768)
    data = json.loads(body)
    assert "response_format" not in data
    assert data["size"] == "1536x1024"

    # chatgpt-image-latest is a GPT Image alias. It used to keep response_format
    # and a raw WxH, which the API rejects.
    _method, _path, body, _headers = shim.build_image_request("prompt", "chatgpt-image-latest", 1024, 768)
    data = json.loads(body)
    assert "response_format" not in data
    assert data["size"] == "1536x1024"
    assert data["model"] == "chatgpt-image-latest"

    # omitted model is the GPT Image default, not a DALL·E body
    _method, path, body, _headers = shim.build_image_request("prompt", None, 1024, 1024)
    data = json.loads(body)
    assert path.endswith("/images/generations")
    assert "response_format" not in data
    assert "model" not in data
    assert data["size"] == "1024x1024"

    # width=0 used to divide as 0 and pick the portrait size
    _method, _path, body, _headers = shim.build_image_request("prompt", "gpt-image-2", 0, 1024)
    assert json.loads(body)["size"] == "1024x1024"

    # test dall-e snapping. Legacy create still sends response_format.
    _method, _path, body, _headers = shim.build_image_request("prompt", "dall-e-2", 400, 300)
    data = json.loads(body)
    assert data["size"] == "512x512"
    assert data["response_format"] == "b64_json"

    _method, _path, body, _headers = shim.build_image_request("prompt", "dall-e-3", 200, 200)
    data = json.loads(body)
    assert data["size"] == "1024x1024"
    assert data["response_format"] == "b64_json"


def test_openai_shim_edits_use_gpt_image_json():
    shim = OpenAIShim(_DummyClient())

    _method, path, body, _headers = shim.build_image_request("prompt", None, 1024, 768, source_image="abc")
    data = json.loads(body)
    assert path.endswith("/images/edits")
    assert "response_format" not in data
    assert "model" not in data
    assert data["images"] == [{"image_url": "data:image/png;base64,abc"}]
    assert data["size"] == "1536x1024"

    _method, path, body, _headers = shim.build_image_request("prompt", "chatgpt-image-latest", 1024, 1024, source_image="abc")
    data = json.loads(body)
    assert path.endswith("/images/edits")
    assert "response_format" not in data
    assert data["model"] == "chatgpt-image-latest"
    assert data["images"] == [{"image_url": "data:image/png;base64,abc"}]


@pytest.mark.parametrize("model", ["dall-e-2", "dall-e-3"])
def test_openai_shim_dalle_edit_raises(model):
    shim = OpenAIShim(_DummyClient())
    with pytest.raises(ValueError, match="cannot edit") as raised:
        shim.build_image_request("prompt", model, 1024, 1024, source_image="data:image/png;base64,123")
    message = str(raised.value)
    assert model in message
    assert "or dall-e-2" not in message
    assert "gpt-image" in message


def test_generic_provider_image_request_stays_on_generations():
    """Unknown hosts stay on the generic body, not the official OpenAI edit route."""
    assert get_provider_shim_class("deepseek") is BaseProviderShim
    shim = BaseProviderShim(_DummyClient())
    _method, path, body, _headers = shim.build_image_request("prompt", "flux", 512, 512, source_image="aaa")
    data = json.loads(body)
    assert path.endswith("/images/generations")
    assert data["response_format"] == "b64_json"
    assert data["image_url"] == "data:image/png;base64,aaa"
    assert data["size"] == "512x512"


def test_openrouter_shim_build_image_request_model_truthy():
    class DummyClient:
        def _get_provider(self): return "openrouter"
        def _endpoint(self): return "http://test"
        def _api_path(self): return "/api/v1"
        def _headers(self): return {}
    shim = OpenRouterShim(DummyClient())

    # When model is None, "model" key must not be present
    _, _, body, _ = shim.build_image_request("a prompt", None, 512, 512)
    data = json.loads(body)
    assert "model" not in data

    # When model is empty string, "model" key must not be present
    _, _, body, _ = shim.build_image_request("a prompt", "", 512, 512)
    data = json.loads(body)
    assert "model" not in data

    # When model is truthy, "model" key must be included
    _, _, body, _ = shim.build_image_request("a prompt", "black-forest-labs/flux-1", 512, 512)
    data = json.loads(body)
    assert data["model"] == "black-forest-labs/flux-1"



def test_responses_effort_uses_openrouter_vocabulary():
    # Pins that the Responses path maps effort via the shim's reasoning provider
    # (openrouter -> nested reasoning.effort), not a hardcoded "openai".
    from plugin.framework.client.base_provider_shim import effort_payload

    class DummyClient:
        config = {"is_openrouter": True, "reasoning_effort": "high"}
        def _get_provider(self): return "openrouter"
        def _endpoint(self): return "http://test"
        def _api_path(self): return "/api/v1"
        def _headers(self): return {}

    shim = OpenAIShim(DummyClient())
    _, url, body, _ = shim.build_chat_request(
        [{"role": "user", "content": "hi"}], 100, None, [{"type": "function", "function": {"name": "f", "parameters": {}}}], False, "openai/gpt-6", None, None
    )
    assert url.endswith("/responses")
    expected = effort_payload("openrouter", "high")["reasoning"]["effort"]
    assert json.loads(body)["reasoning"]["effort"] == expected


def test_openrouter_prompt_caching():
    from plugin.framework.client.shim_helpers import snapshot_text

    class DummyClient:
        def _get_provider(self):
            return "openrouter"

        def _endpoint(self):
            return "http://test"

        def _api_path(self):
            return "/api/v1"

        def _headers(self):
            return {}

    shim = OpenRouterShim(DummyClient())
    snap = snapshot_text("Document text")
    tool_loop_messages = [
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {"id": "t1", "function": {"name": "f1", "arguments": "{}"}},
                {"id": "t2", "function": {"name": "f2", "arguments": "{}"}},
            ],
        },
        {"role": "tool", "tool_call_id": "t1", "content": "res1"},
        {"role": "tool", "tool_call_id": "t2", "content": f"res2\n\n{snap}"},
    ]

    # 1. OpenRouterShim with anthropic/claude-sonnet-4.5
    _m, _url, body1, _h = shim.build_chat_request(
        tool_loop_messages, 100, None, None, False, "anthropic/claude-sonnet-4.5", None
    )
    data1 = json.loads(body1)
    assert "cache_control" not in data1  # Root JSON has no cache_control

    msgs1 = data1["messages"]
    # Roles become assistant, tool, tool, user (split snapshot onto trailing user message)
    assert [m["role"] for m in msgs1] == ["assistant", "tool", "tool", "user"]
    assert msgs1[0]["content"] == ""
    assert msgs1[1]["content"] == "res1"  # first tool unchanged
    assert msgs1[2]["content"] == [{"type": "text", "text": "res2", "cache_control": {"type": "ephemeral"}}]
    assert msgs1[3]["content"] == snap
    assert json.dumps(data1).count("cache_control") == 1

    # 2. Same shim with openai/gpt-oss-120b: no cache_control anywhere
    _m, _url, body2, _h = shim.build_chat_request(
        tool_loop_messages, 100, None, None, False, "openai/gpt-oss-120b", None
    )
    data2 = json.loads(body2)
    assert "cache_control" not in data2
    body2_str = json.dumps(data2)
    assert "cache_control" not in body2_str
    assert [m["role"] for m in data2["messages"]] == ["assistant", "tool", "tool"]

    # 3. BaseProviderShim (Cerebras path) keeps assistant + tool + tool order with no cache_control
    assert get_provider_shim_class("cerebras") is BaseProviderShim
    cerebras_shim = BaseProviderShim(DummyClient())
    _m, _url, body3, _h = cerebras_shim.build_chat_request(
        tool_loop_messages, 100, None, None, False, "llama3.1-70b", None
    )
    data3 = json.loads(body3)
    msgs3 = data3["messages"]
    assert [m["role"] for m in msgs3] == ["assistant", "tool", "tool"]
    body3_str = json.dumps(data3)
    assert "cache_control" not in body3_str
    # 4. User send: folded messages to anthropic/* mark the block before the tail
    user_send_messages = [
        {"role": "user", "content": [
            {"type": "text", "text": "hello"},
            {"type": "text", "text": snap},
        ]},
    ]
    _m, _url, body_u1, _h = shim.build_chat_request(
        user_send_messages, 100, None, None, False, "anthropic/claude-sonnet-4.5", None
    )
    data_u1 = json.loads(body_u1)
    msgs_u1 = data_u1["messages"]
    assert len(msgs_u1) == 1
    assert msgs_u1[0]["role"] == "user"
    assert msgs_u1[0]["content"] == [
        {"type": "text", "text": "hello", "cache_control": {"type": "ephemeral"}},
        {"type": "text", "text": snap},
    ]

    # 5. User send: openai/gpt-oss-120b and Cerebras keep the single folded user message without cache_control
    _m, _url, body_u2, _h = shim.build_chat_request(
        user_send_messages, 100, None, None, False, "openai/gpt-oss-120b", None
    )
    data_u2 = json.loads(body_u2)
    msgs_u2 = data_u2["messages"]
    assert len(msgs_u2) == 1
    assert msgs_u2[0]["role"] == "user"
    assert msgs_u2[0]["content"] == [
        {"type": "text", "text": "hello"},
        {"type": "text", "text": snap},
    ]
    assert "cache_control" not in json.dumps(data_u2)

    _m, _url, body_u3, _h = cerebras_shim.build_chat_request(
        user_send_messages, 100, None, None, False, "openai/gpt-oss-120b", None
    )
    data_u3 = json.loads(body_u3)
    msgs_u3 = data_u3["messages"]
    assert len(msgs_u3) == 1
    assert msgs_u3[0]["role"] == "user"
    assert msgs_u3[0]["content"] == [
        {"type": "text", "text": "hello"},
        {"type": "text", "text": snap},
    ]
    assert "cache_control" not in json.dumps(data_u3)


def test_openai_tool_choice():
    class DummyClient:
        def _get_provider(self):
            return "openai"

        def _endpoint(self):
            return "http://test"

        def _api_path(self):
            return "/api/v1"

        def _headers(self):
            return {}

    shim = OpenAIShim(DummyClient())
    tools = [{"type": "function", "function": {"name": "test_func", "parameters": {}}}]
    messages = [{"role": "user", "content": "hi"}]

    # tool_choice=None sends "auto"
    _m, _url, body_auto, _h = shim.build_chat_request(
        messages, 100, None, tools, False, "gpt-4o", None, tool_choice=None
    )
    data_auto = json.loads(body_auto)
    assert data_auto["tool_choice"] == "auto"
    assert data_auto["parallel_tool_calls"] is False
    assert "tools" in data_auto

    # tool_choice="none" sends "none"
    _m, _url, body_none, _h = shim.build_chat_request(
        messages, 100, None, tools, False, "gpt-4o", None, tool_choice="none"
    )
    data_none = json.loads(body_none)
    assert data_none["tool_choice"] == "none"
    assert data_none["parallel_tool_calls"] is False
    assert "tools" in data_none


def test_openrouter_tool_round_splits_trailing_snapshot():
    from plugin.framework.client.shim_helpers import snapshot_text

    class DummyClient:
        def _get_provider(self):
            return "openrouter"

        def _service_name(self):
            return "openrouter"

        def _endpoint(self):
            return "http://test"

        def _api_path(self):
            return "/api/v1"

        def _headers(self):
            return {}

    shim = OpenRouterShim(DummyClient())
    snap = snapshot_text("Document text")
    messages = [
        {"role": "user", "content": "hello"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {"id": "t1", "function": {"name": "f1", "arguments": "{}"}},
                {"id": "t2", "function": {"name": "f2", "arguments": "{}"}},
            ],
        },
        {"role": "tool", "tool_call_id": "t1", "content": "res1"},
        {"role": "tool", "tool_call_id": "t2", "content": f"res2\n\n{snap}"},
    ]

    _m, _url, body, _h = shim.build_chat_request(
        messages, 100, None, None, False, "anthropic/claude-sonnet-4.5", None
    )
    data = json.loads(body)
    msgs = data["messages"]
    assert msgs[0]["content"] == "hello"
    assert msgs[1]["content"] == ""
    assert msgs[2]["content"] == "res1"
    assert msgs[3]["content"] == [{"type": "text", "text": "res2", "cache_control": {"type": "ephemeral"}}]
    assert msgs[4]["role"] == "user"
    assert msgs[4]["content"] == snap
    assert json.dumps(data).count("cache_control") == 1


def test_openrouter_single_tool_marks_tool_result():
    from plugin.framework.client.shim_helpers import snapshot_text

    class DummyClient:
        def _get_provider(self):
            return "openrouter"

        def _service_name(self):
            return "openrouter"

        def _endpoint(self):
            return "http://test"

        def _api_path(self):
            return "/api/v1"

        def _headers(self):
            return {}

    shim = OpenRouterShim(DummyClient())
    snap = snapshot_text("Document text")
    messages = [
        {"role": "user", "content": "hello"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {"id": "t1", "function": {"name": "f1", "arguments": "{}"}},
            ],
        },
        {"role": "tool", "tool_call_id": "t1", "content": f"res1\n\n{snap}"},
    ]

    _m, _url, body, _h = shim.build_chat_request(
        messages, 100, None, None, False, "anthropic/claude-sonnet-4.5", None
    )
    data = json.loads(body)
    msgs = data["messages"]
    assert msgs[0]["content"] == "hello"
    assert msgs[1]["content"] == ""
    assert msgs[2]["content"] == [{"type": "text", "text": "res1", "cache_control": {"type": "ephemeral"}}]
    assert msgs[3]["role"] == "user"
    assert msgs[3]["content"] == snap
    assert json.dumps(data).count("cache_control") == 1


def test_cache_route_openrouter_body_session_id():
    # Chat session id overrides a user-set session_id in chat_extra (see llm-hacks §13).
    client = _DummyClient(provider="openrouter")
    shim = OpenRouterShim(client)
    _method, _url, body, headers = shim.build_chat_request(
        [{"role": "user", "content": "hi"}],
        100,
        None,
        None,
        False,
        "openai/gpt-oss-120b:nitro",
        None,
        chat_extra={"session_id": "user-set"},
        cache_session_id="conv-1",
    )
    data = json.loads(body)
    assert data.get("session_id") == "conv-1"
    assert "prompt_cache_key" not in data
    assert "x-session-id" not in headers
    assert "x-grok-conv-id" not in headers


def test_cache_route_openrouter_caps_at_256():
    client = _DummyClient(provider="openrouter")
    shim = OpenRouterShim(client)
    long_id = "é" * 300
    _method, _url, body, _headers = shim.build_chat_request(
        [{"role": "user", "content": "hi"}], 100, None, None, False, "openai/gpt-oss-120b:nitro", None, cache_session_id=long_id
    )
    data = json.loads(body)
    assert len(data.get("session_id", "")) == 256
    assert data["session_id"] == ("é" * 256)

    # Same id on anthropic/claude-sonnet-4.5 still sets body session_id (OpenRouter, not native Anthropic)
    _method, _url, body2, _headers2 = shim.build_chat_request(
        [{"role": "user", "content": "hi"}], 100, None, None, False, "anthropic/claude-sonnet-4.5", None, cache_session_id=long_id
    )
    data2 = json.loads(body2)
    assert len(data2.get("session_id", "")) == 256
    assert data2["session_id"] == ("é" * 256)


def test_cache_route_openai_prompt_cache_key():
    client = _DummyClient(provider="openai")
    shim = OpenAIShim(client)
    long_id = "k" * 300
    _method, _url, body, headers = shim.build_chat_request(
        [{"role": "user", "content": "hi"}], 100, None, None, False, "gpt-4o", None, cache_session_id=long_id
    )
    data = json.loads(body)
    assert data.get("prompt_cache_key") == long_id
    assert "session_id" not in data
    assert "x-grok-conv-id" not in headers


def test_cache_route_openai_responses_prompt_cache_key():
    client = _DummyClient(provider="openai")
    shim = OpenAIShim(client)
    tools = [{"type": "function", "function": {"name": "test_tool", "parameters": {}}}]
    _method, url, body, _headers = shim.build_chat_request(
        [{"role": "user", "content": "hi"}], 100, None, tools, False, "gpt-6-astra", None, cache_session_id="resp-key-1"
    )
    assert url.endswith("/responses")
    data = json.loads(body)
    assert data.get("prompt_cache_key") == "resp-key-1"
    assert "input" in data


def test_cache_route_cerebras_prompt_cache_key():
    client = _DummyClient(provider="cerebras")
    shim = BaseProviderShim(client)
    long_id = "a" * 1100
    _method, _url, body, _headers = shim.build_chat_request(
        [{"role": "user", "content": "hi"}], 100, None, None, False, "llama3.1-70b", None, cache_session_id=long_id
    )
    data = json.loads(body)
    assert data.get("prompt_cache_key") == "a" * 1024
    assert len(data["prompt_cache_key"]) == 1024
    assert "session_id" not in data


def test_cache_route_xai_header():
    client = _DummyClient(provider="xai")
    shim = GrokShim(client)
    _method, _url, body, headers = shim.build_chat_request(
        [{"role": "user", "content": "hi"}], 100, None, None, False, "grok-2", None, cache_session_id="xai-conv-1"
    )
    assert headers.get("x-grok-conv-id") == "xai-conv-1"
    assert headers["Authorization"] == "Bearer test"
    data = json.loads(body)
    assert "session_id" not in data
    assert "prompt_cache_key" not in data

    # Id "bad\nid" omits the header and still returns a body
    _method, _url, body_bad, headers_bad = shim.build_chat_request(
        [{"role": "user", "content": "hi"}], 100, None, None, False, "grok-2", None, cache_session_id="bad\nid"
    )
    assert "x-grok-conv-id" not in headers_bad
    assert headers_bad["Authorization"] == "Bearer test"
    data_bad = json.loads(body_bad)
    assert "messages" in data_bad


def test_cache_route_absent_when_unset():
    for prov, shim_cls in [("openrouter", OpenRouterShim), ("openai", OpenAIShim), ("cerebras", BaseProviderShim), ("xai", GrokShim)]:
        client = _DummyClient(provider=prov)
        shim = shim_cls(client)
        for unset_id in (None, "", "   "):
            _m, _u, body, headers = shim.build_chat_request(
                [{"role": "user", "content": "hi"}], 100, None, None, False, "default-model", None, cache_session_id=unset_id
            )
            data = json.loads(body)
            assert "session_id" not in data
            assert "prompt_cache_key" not in data
            assert "x-grok-conv-id" not in headers
            assert headers == client._headers()

    # Provider google on BaseProviderShim and provider groq on BaseProviderShim with a real id also omit them
    for other_prov in ("google", "groq"):
        client = _DummyClient(provider=other_prov)
        shim = BaseProviderShim(client)
        _m, _u, body, headers = shim.build_chat_request(
            [{"role": "user", "content": "hi"}], 100, None, None, False, "default-model", None, cache_session_id="real-id-123"
        )
        data = json.loads(body)
        assert "session_id" not in data
        assert "prompt_cache_key" not in data
        assert "x-grok-conv-id" not in headers
        assert headers == client._headers()


def test_openrouter_cache_family_gate():
    from plugin.framework.client.shim_helpers import snapshot_text

    client = _DummyClient(provider="openrouter")
    shim = OpenRouterShim(client)
    snap = snapshot_text("Document text")

    # 1. System + user send with snapshot
    sys_user_messages = [
        {"role": "system", "content": "You are a helpful assistant."},
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "hello"},
                {"type": "text", "text": snap},
            ],
        },
    ]
    tools = [
        {
            "type": "function",
            "function": {"name": "search", "description": "Search web", "parameters": {"type": "object"}},
        }
    ]

    # Anthropic models on OpenRouter: 1h system breakpoint, ephemeral user breakpoint
    for m in ("anthropic/claude-sonnet-4.5", "anthropic/claude-sonnet-4.5:nitro"):
        _m, _u, body, _h = shim.build_chat_request(sys_user_messages, 100, None, tools, False, m, None)
        data = json.loads(body)
        assert "cache_control" not in data
        assert "diagnostics" not in data
        assert data["messages"][0]["role"] == "system"
        assert data["messages"][0]["content"][0]["cache_control"] == {"type": "ephemeral", "ttl": "1h"}
        assert "cache_control" not in data["tools"][0]
        assert data["messages"][1]["content"][0]["cache_control"] == {"type": "ephemeral"}
        assert "ttl" not in data["messages"][1]["content"][0]["cache_control"]
        assert "cache_control" not in data["messages"][1]["content"][1]
        assert json.dumps(data).count("cache_control") == 2

    # Alibaba models on OpenRouter: ephemeral on system (no ttl), ephemeral on user (no ttl)
    alibaba_ids = [
        "deepseek/deepseek-v3.2",
        "qwen/qwen3-max",
        "qwen/qwen-plus",
        "qwen/qwen3.6-plus",
        "qwen/qwen3-coder-plus",
        "qwen/qwen3-coder-flash",
        "qwen/qwen3-max:nitro",
    ]
    for m in alibaba_ids:
        _m, _u, body, _h = shim.build_chat_request(sys_user_messages, 100, None, tools, False, m, None)
        data = json.loads(body)
        assert "cache_control" not in data
        assert "diagnostics" not in data
        assert data["messages"][0]["content"][0]["cache_control"] == {"type": "ephemeral"}
        assert "ttl" not in data["messages"][0]["content"][0]["cache_control"]
        assert "cache_control" not in data["tools"][0]
        assert data["messages"][1]["content"][0]["cache_control"] == {"type": "ephemeral"}
        assert "ttl" not in data["messages"][1]["content"][0]["cache_control"]
        assert json.dumps(data).count("cache_control") == 2

    # Unmarked models: zero cache_control anywhere, diagnostics absent
    unmarked_ids = [
        "qwen/qwen3.5-plus-02-15",
        "qwen/qwen3.5-flash-02-23",
        "qwen/qwen-plus-2025-07-28",
        "qwen/qwen3-max-thinking",
        "deepseek/deepseek-v3.2-exp",
        "qwen/qwen3-max:free",
        "openai/gpt-oss-120b",
        "deepseek/deepseek-chat",
        "x-ai/grok-2",
        "google/gemini-2.5-pro",
        "moonshotai/kimi-k2",
        "groq/llama-3.3-70b-versatile",
    ]
    for m in unmarked_ids:
        _m, _u, body, _h = shim.build_chat_request(sys_user_messages, 100, None, tools, False, m, None)
        data = json.loads(body)
        assert "cache_control" not in json.dumps(data)
        assert "diagnostics" not in data

    # Unmarked models on tool round: snapshot remains inside tool message, zero cache_control
    tool_round_messages = [
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [{"id": "t1", "function": {"name": "f1", "arguments": "{}"}}],
        },
        {"role": "tool", "tool_call_id": "t1", "content": f"res1\n\n{snap}"},
    ]
    for m in unmarked_ids:
        _m, _u, body, _h = shim.build_chat_request(tool_round_messages, 100, None, None, False, m, None)
        data = json.loads(body)
        assert "cache_control" not in json.dumps(data)
        assert len(data["messages"]) == 2
        assert data["messages"][1]["role"] == "tool"
        assert snap in data["messages"][1]["content"]

    # Cerebras BaseProviderShim body has neither cache_control nor diagnostics
    cerebras_shim = BaseProviderShim(_DummyClient(provider="cerebras"))
    _m, _u, c_body, _h = cerebras_shim.build_chat_request(sys_user_messages, 100, None, tools, False, "llama3.1-70b", None)
    c_data = json.loads(c_body)
    assert "cache_control" not in json.dumps(c_data)
    assert "diagnostics" not in c_data
