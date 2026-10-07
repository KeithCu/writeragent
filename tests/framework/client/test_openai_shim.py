import pytest
import json
from plugin.framework.client.openai_shim import OpenAIShim, OpenRouterShim

def test_openai_shim_build_image_request():
    class DummyClient:
        def _get_provider(self): return "openai"
        def _endpoint(self): return "http://test"
        def _api_path(self): return "/v1"
        def _headers(self): return {}
    shim = OpenAIShim(DummyClient())

    # test gpt-image drops response_format and size
    method, path, body, headers = shim.build_image_request("prompt", "gpt-image-alpha", 1024, 768)
    data = json.loads(body)
    assert "response_format" not in data
    assert data["size"] == "1536x1024"

    # test dall-e snapping
    method, path, body, headers = shim.build_image_request("prompt", "dall-e-2", 400, 300)
    data = json.loads(body)
    assert data["size"] == "512x512"

    method, path, body, headers = shim.build_image_request("prompt", "dall-e-3", 200, 200)
    data = json.loads(body)
    assert data["size"] == "1024x1024"

    # test ValueError
    with pytest.raises(ValueError, match="dall-e-3 cannot edit an existing image. Pick a GPT Image model or dall-e-2."):
        shim.build_image_request("prompt", "dall-e-3", 1024, 1024, source_image="data:image/png;base64,123")


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

