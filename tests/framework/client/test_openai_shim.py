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

