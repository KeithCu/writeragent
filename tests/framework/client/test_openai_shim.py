import pytest
import json
from plugin.framework.client.base_provider_shim import BaseProviderShim
from plugin.framework.client.openai_shim import OpenAIShim, OpenRouterShim, get_provider_shim_class


class _DummyClient:
    def _get_provider(self):
        return "openai"

    def _endpoint(self):
        return "http://test"

    def _api_path(self):
        return "/v1"

    def _headers(self):
        return {}


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
        {"role": "tool", "tool_call_id": "t2", "content": "res2"},
        {"role": "user", "content": snap},
    ]

    # 1. OpenRouterShim with anthropic/claude-sonnet-4.5
    _m, _url, body1, _h = shim.build_chat_request(
        tool_loop_messages, 100, None, None, False, "anthropic/claude-sonnet-4.5", None
    )
    data1 = json.loads(body1)
    assert "cache_control" not in data1  # Root JSON has no cache_control

    msgs1 = data1["messages"]
    # Roles stay assistant, tool, tool, user
    assert [m["role"] for m in msgs1] == ["assistant", "tool", "tool", "user"]
    # The last tool content block has the marker
    assert msgs1[1]["content"] == "res1"  # first tool unchanged
    tool2_content = msgs1[2]["content"]
    assert isinstance(tool2_content, list)
    assert len(tool2_content) == 1
    assert tool2_content[0]["text"] == "res2"
    assert tool2_content[0].get("cache_control") == {"type": "ephemeral"}
    # The snapshot user message has none
    assert msgs1[3]["content"] == snap
    assert "cache_control" not in msgs1[3]

    # 2. Same shim with openai/gpt-oss-120b: no cache_control anywhere
    _m, _url, body2, _h = shim.build_chat_request(
        tool_loop_messages, 100, None, None, False, "openai/gpt-oss-120b", None
    )
    data2 = json.loads(body2)
    assert "cache_control" not in data2
    body2_str = json.dumps(data2)
    assert "cache_control" not in body2_str

    # 3. BaseProviderShim (Cerebras path) keeps assistant + tool + user order with no cache_control
    assert get_provider_shim_class("cerebras") is BaseProviderShim
    cerebras_shim = BaseProviderShim(DummyClient())
    _m, _url, body3, _h = cerebras_shim.build_chat_request(
        tool_loop_messages, 100, None, None, False, "llama3.1-70b", None
    )
    data3 = json.loads(body3)
    msgs3 = data3["messages"]
    assert [m["role"] for m in msgs3] == ["assistant", "tool", "tool", "user"]
    body3_str = json.dumps(data3)
    assert "cache_control" not in body3_str

    # 4. User send: anthropic/* folds the tail and marks the block before it
    user_send_messages = [
        {"role": "user", "content": "hello"},
        {"role": "user", "content": snap},
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

    # 5. User send: openai/gpt-oss-120b and Cerebras keep two string user messages without fold
    _m, _url, body_u2, _h = shim.build_chat_request(
        user_send_messages, 100, None, None, False, "openai/gpt-oss-120b", None
    )
    data_u2 = json.loads(body_u2)
    msgs_u2 = data_u2["messages"]
    assert len(msgs_u2) == 2
    assert msgs_u2[0] == {"role": "user", "content": "hello"}
    assert msgs_u2[1] == {"role": "user", "content": snap}
    assert "cache_control" not in json.dumps(data_u2)

    _m, _url, body_u3, _h = cerebras_shim.build_chat_request(
        user_send_messages, 100, None, None, False, "openai/gpt-oss-120b", None
    )
    data_u3 = json.loads(body_u3)
    msgs_u3 = data_u3["messages"]
    assert len(msgs_u3) == 2
    assert msgs_u3[0] == {"role": "user", "content": "hello"}
    assert msgs_u3[1] == {"role": "user", "content": snap}
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
