import pytest
import sys
from plugin.framework.constants import get_plugin_dir
import os
import json
import base64
from unittest.mock import MagicMock, patch

# Add parent directory to path to import core
sys.path.insert(0, os.path.dirname(get_plugin_dir()))

from plugin.writer.images.image_utils import ImageService, EndpointImageProvider
from plugin.framework.client.base_provider_shim import canonical_aspect_ratio, canonical_resolution
from plugin.framework.client.llm_client import LlmClient
from plugin.tests.testing_utils import MockContext, create_mock_client

class TestEndpointImageProvider:
    def setup_method(self):
        self.mock_ctx = MockContext()
        self.api_config = {"model": "test-model"}
        with patch('plugin.writer.images.image_utils.LlmClient'):
            self.provider = EndpointImageProvider(self.api_config, self.mock_ctx)
            self.mock_client = self.provider.client

    @patch('plugin.writer.images.image_utils.sync_request')
    def test_generate_openrouter_url(self, mock_sync):
        self.mock_client.config.get.side_effect = lambda k, d=None: True if k == "is_openrouter" else d
        self.mock_client.make_chat_request.return_value = ("POST", "/chat", "{}", {})
        
        # Mock OpenRouter response with image URL
        mock_resp = {
            "content": "Here is your image",
            "images": [{"image_url": {"url": "http://example.com/image.png"}}]
        }
        self.mock_client.request_with_tools.return_value = mock_resp
        mock_sync.return_value = b"fake-image-data"

        paths, err = self.provider.generate("test prompt")
        
        assert (len(paths)) == (1)
        assert (err) == ("")
        assert (paths[0].endswith(".webp"))
        from plugin.framework.config import get_config_int
        mock_sync.assert_called_once_with(
            "http://example.com/image.png",
            parse_json=False,
            timeout=get_config_int("request_timeout"),
        )

    def test_generate_openrouter_b64(self):
        self.mock_client.config.get.side_effect = lambda k, d=None: True if k == "is_openrouter" else d
        self.mock_client.make_chat_request.return_value = ("POST", "/chat", "{}", {})
        
        # Mock OpenRouter response with b64 image
        b64_data = base64.b64encode(b"fake-image-data-b64").decode()
        mock_resp = {
            "images": [{"image_url": f"data:image/png;base64,{b64_data}"}]
        }
        self.mock_client.request_with_tools.return_value = mock_resp

        paths, err = self.provider.generate("test prompt")
        
        assert (len(paths)) == (1)
        assert (err) == ("")
        assert (paths[0].endswith(".png"))
        with open(paths[0], 'rb') as f:
            assert (f.read()) == (b"fake-image-data-b64")
        os.unlink(paths[0])

    def test_generate_standard_b64(self):
        self.mock_client.config.get.return_value = False # Not OpenRouter
        b64_data = base64.b64encode(b"standard-b64-data").decode()
        self.mock_client.image_completion.return_value = [b64_data]

        paths, err = self.provider.generate("test prompt")

        assert (len(paths)) == (1)
        assert (err) == ("")
        assert (paths[0].endswith(".png"))
        with open(paths[0], 'rb') as f:
            assert (f.read()) == (b"standard-b64-data")
        os.unlink(paths[0])

    @patch('plugin.writer.images.image_utils.sync_request')
    def test_fallback_logic_url(self, mock_sync):
        self.mock_client.config.get.side_effect = lambda k, d=None: True if k == "is_openrouter" else d
        self.mock_client.make_chat_request.return_value = ("POST", "/chat", "{}", {})
        
        # Mock response where image is in content (fallback)
        mock_resp = {
            "content": "http://fallback.com/image.png",
            "images": []
        }
        self.mock_client.request_with_tools.return_value = mock_resp
        mock_sync.return_value = b"fallback-image-data"

        paths, err = self.provider.generate("test prompt")
        
        assert (len(paths)) == (1)
        assert (err) == ("")
        from plugin.framework.config import get_config_int
        mock_sync.assert_called_with(
            "http://fallback.com/image.png",
            parse_json=False,
            timeout=get_config_int("request_timeout"),
        )
        os.unlink(paths[0])

    def test_fallback_logic_b64(self):
        self.mock_client.config.get.side_effect = lambda k, d=None: True if k == "is_openrouter" else d
        self.mock_client.make_chat_request.return_value = ("POST", "/chat", "{}", {})
        
        # Mock response where image is in content (fallback b64)
        b64_data = base64.b64encode(b"fallback-b64-data").decode()
        mock_resp = {
            "content": f"Check this out: data:image/png;base64,{b64_data}",
            "images": []
        }
        self.mock_client.request_with_tools.return_value = mock_resp

        paths, err = self.provider.generate("test prompt")
        
        assert (len(paths)) == (1)
        assert (err) == ("")
        with open(paths[0], 'rb') as f:
            assert (f.read()) == (b"fallback-b64-data")
        os.unlink(paths[0])

    def test_scoping_bug_fix_verification(self):
        """
        Verifies that the scoping bug is fixed. 
        """
        self.mock_client.config.get.return_value = False # Standard path
        self.mock_client.image_completion.return_value = [] # No images

        # This should NOT crash now. It should just return [].
        result = self.provider.generate("test prompt")
        assert (result) == (([], "No image data returned from provider"))

    def test_generate_error_handling_missing_fields(self):
        """When provider response lacks expected image fields, ensure we return ([], '')."""
        self.mock_client.config.get.side_effect = lambda k, d=None: True if k == "is_openrouter" else d
        self.mock_client.make_chat_request.return_value = ("POST", "/chat", "{}", {})

        # Missing image_url key
        mock_resp = {
            "images": [{"wrong_key": "http://example.com/image.png"}]
        }
        self.mock_client.request_with_tools.return_value = mock_resp

        paths, err = self.provider.generate("test prompt")
        assert (paths) == ([])
        assert (err) == ("")

    def test_fallback_logic_non_string_content(self):
        """When provider response content is non-string (e.g. dict or list), fallback should handle it gracefully."""
        self.mock_client.config.get.side_effect = lambda k, d=None: True if k == "is_openrouter" else d
        self.mock_client.make_chat_request.return_value = ("POST", "/chat", "{}", {})

        # Content is a list or dict instead of str
        mock_resp = {
            "content": [{"type": "text", "text": "no image here"}],
            "images": []
        }
        self.mock_client.request_with_tools.return_value = mock_resp

        paths, err = self.provider.generate("test prompt")
        assert (paths) == ([])
        assert (err) == ("")

    def test_generate_multi_image(self):
        """If provider returns multiple images, ensure paths preserves ordering and all paths are created/cleaned."""
        self.mock_client.config.get.return_value = False # Not OpenRouter

        b64_data1 = base64.b64encode(b"multi-image-b64-data-1").decode()
        b64_data2 = base64.b64encode(b"multi-image-b64-data-2").decode()
        self.mock_client.image_completion.return_value = [b64_data1, b64_data2]

        paths, err = self.provider.generate("test prompt")

        assert (len(paths)) == (2)
        assert (err) == ("")

        assert (paths[0].endswith(".png"))
        with open(paths[0], 'rb') as f:
            assert (f.read()) == (b"multi-image-b64-data-1")

        assert (paths[1].endswith(".png"))
        with open(paths[1], 'rb') as f:
            assert (f.read()) == (b"multi-image-b64-data-2")

        os.unlink(paths[0])
        os.unlink(paths[1])

    def test_fallback_logic_invalid_data_url(self):
        """For OpenRouter fallback content path, verify behavior when content contains a partial/invalid data URL string."""
        self.mock_client.config.get.side_effect = lambda k, d=None: True if k == "is_openrouter" else d
        self.mock_client.make_chat_request.return_value = ("POST", "/chat", "{}", {})

        # Invalid data URL
        mock_resp = {
            "content": "Check this out: data:image/png;invalid",
            "images": []
        }
        self.mock_client.request_with_tools.return_value = mock_resp

        paths, err = self.provider.generate("test prompt")

        assert (paths) == ([])
        assert (err) == ("")

    @patch('plugin.writer.images.image_utils.LlmClient')
    def test_edit_image_openrouter_sends_multimodal_message(self, mock_client_cls):
        """When OpenRouter and source_image are set, make_chat_request receives message content with text + image_url."""
        mock_client = create_mock_client()
        mock_client.config.get.side_effect = lambda k, d=None: True if k == "is_openrouter" else d
        mock_client_cls.return_value = mock_client
        mock_client.make_chat_request.return_value = ("POST", "/chat", "{}", {})
        mock_client.request_with_tools.return_value = {"images": []}
        provider = EndpointImageProvider({"model": "test"}, MockContext())
        provider.client = mock_client

        b64 = "abc123"
        provider.generate("edit prompt", source_image=b64)

        mock_client.make_chat_request.assert_called_once()
        call_messages = mock_client.make_chat_request.call_args[0][0]
        assert (len(call_messages)) == (1)
        content = call_messages[0]["content"]
        assert isinstance(content, list)
        assert (content[0]) == ({"type": "text", "text": "edit prompt"})
        assert (content[1]["type"]) == ("image_url")
        assert (content[1]["image_url"]["url"]) == ("data:image/png;base64," + b64)

    @patch('plugin.writer.images.image_utils.LlmClient')
    def test_edit_image_standard_endpoint_passes_source_image(self, mock_client_cls):
        """When not OpenRouter and source_image is set, image_completion is called with source_image."""
        mock_client = create_mock_client()
        mock_client.image_completion.return_value = [base64.b64encode(b"edited").decode()]
        mock_client_cls.return_value = mock_client
        provider = EndpointImageProvider({"model": "test"}, MockContext())
        provider.client = mock_client

        b64 = "xyz789"
        provider.generate("edit prompt", source_image=b64)

        mock_client.image_completion.assert_called_once()
        kwargs = mock_client.image_completion.call_args[1]
        assert (kwargs.get("source_image")) == (b64)

    @patch('plugin.framework.client.llm_client.init_logging')
    def test_make_image_request_body_includes_image_url_when_source_image(self, mock_init):
        """Non-OpenRouter image requests keep OpenAI-style image_url for source_image."""
        config = {"endpoint": "https://api.example.com", "model": "test-model"}
        client = LlmClient(config, MockContext())
        method, path, body, headers = client.make_image_request("a cat", source_image="b64data")
        data = json.loads(body.decode("utf-8"))
        assert ("image_url") in (data)
        assert (data["image_url"]) == ("data:image/png;base64,b64data")

    @patch('plugin.framework.client.llm_client.init_logging')
    def test_openrouter_image_request_uses_input_references_for_edit(self, mock_init):
        """OpenRouter /images img2img must send input_references, not top-level image_url.

        OpenRouter ignores image_url on POST /api/v1/images (HTTP 200, no input image
        tokens), so a selected graphic would not be edited.
        """
        config = {"endpoint": "https://openrouter.ai/api", "model": "black-forest-labs/flux.2-klein-4b", "is_openrouter": True}
        client = LlmClient(config, MockContext())
        with patch.object(client, "_resolve_auth", return_value={"provider": "openrouter"}):
            method, path, body, headers = client.make_image_request("make him a wizard", source_image="b64data")
        data = json.loads(body.decode("utf-8"))
        assert ("image_url") not in (data)
        assert (data["input_references"]) == ([{"type": "image_url", "image_url": {"url": "data:image/png;base64,b64data"}}])

    @patch('plugin.framework.client.llm_client.init_logging')
    def test_openai_image_request_uses_images_edits_for_edit(self, mock_init):
        """Official OpenAI img2img is POST /images/edits JSON images[].image_url, not generations image_url."""
        config = {"endpoint": "https://api.openai.com", "model": "gpt-image-2", "api_key": "sk-test"}
        client = LlmClient(config, MockContext())
        with patch.object(client, "_resolve_auth", return_value={"provider": "openai"}):
            method, path, body, headers = client.make_image_request("a cat", model="gpt-image-2")
            data = json.loads(body.decode("utf-8"))
            assert (path.endswith("/images/generations"))
            assert not (path.endswith("/images/edits"))
            assert ("image_url") not in (data)
            assert ("images") not in (data)
            assert ("steps") not in (data)
            assert (data["response_format"]) == ("b64_json")

            method, path, body, headers = client.make_image_request(
                "make him a wizard", model="gpt-image-2", source_image="b64data"
            )
        data = json.loads(body.decode("utf-8"))
        assert (path.endswith("/images/edits"))
        assert ("image_url") not in (data)
        assert (data["images"]) == ([{"image_url": "data:image/png;base64,b64data"}])
        assert (data["response_format"]) == ("b64_json")
        assert (data["model"]) == ("gpt-image-2")

    @patch('plugin.framework.client.llm_client.init_logging')
    def test_openai_dalle3_rejects_edit(self, mock_init):
        """dall-e-3 is generations-only; refuse rather than replace the selected graphic."""
        config = {"endpoint": "https://api.openai.com", "model": "dall-e-3", "api_key": "sk-test"}
        client = LlmClient(config, MockContext())
        with patch.object(client, "_resolve_auth", return_value={"provider": "openai"}):
            with pytest.raises(ValueError) as raised:
                client.make_image_request("make it dusk", model="dall-e-3", source_image="b64data")
        assert ("dall-e-3") in (str(raised.value).lower())
        assert ("cannot edit") in (str(raised.value).lower())

    @patch('plugin.framework.client.llm_client.init_logging')
    def test_together_image_request_uses_reference_images_for_edit(self, mock_init):
        """Together default image models (Flash Image / FLUX.2) want reference_images, not image_url."""
        config = {"endpoint": "https://api.together.xyz", "model": "google/flash-image-2.5"}
        client = LlmClient(config, MockContext())
        with patch.object(client, "_resolve_auth", return_value={"provider": "together"}):
            method, path, body, headers = client.make_image_request("a cat", model="google/flash-image-2.5")
            data = json.loads(body.decode("utf-8"))
            assert ("image_url") not in (data)
            assert ("reference_images") not in (data)
            assert ("size") not in (data)
            assert (data["width"]) == (1024)
            assert (data["height"]) == (1024)

            method, path, body, headers = client.make_image_request(
                "wide", model="google/flash-image-2.5", width=896, height=512
            )
            data = json.loads(body.decode("utf-8"))
            assert (data["width"]) == (896)
            assert (data["height"]) == (512)
            assert ("size") not in (data)

            method, path, body, headers = client.make_image_request(
                "make him a wizard", model="google/flash-image-2.5", source_image="b64data"
            )
        data = json.loads(body.decode("utf-8"))
        assert ("image_url") not in (data)
        assert (data["reference_images"]) == (["data:image/png;base64,b64data"])

    @patch('plugin.framework.client.llm_client.init_logging')
    def test_together_kontext_image_request_uses_image_url_for_edit(self, mock_init):
        """FLUX.1 Kontext on Together documents a single image_url string."""
        config = {"endpoint": "https://api.together.xyz", "model": "black-forest-labs/FLUX.1-kontext-pro"}
        client = LlmClient(config, MockContext())
        with patch.object(client, "_resolve_auth", return_value={"provider": "together"}):
            method, path, body, headers = client.make_image_request(
                "a lake", model="black-forest-labs/FLUX.1-kontext-pro", width=896, height=512
            )
            create = json.loads(body.decode("utf-8"))
            assert (create["aspect_ratio"]) == ("16:9")
            assert ("size") not in (create)
            assert ("width") not in (create)
            assert ("height") not in (create)

            method, path, body, headers = client.make_image_request(
                "watercolor", model="black-forest-labs/FLUX.1-kontext-pro", source_image="b64data"
            )
        data = json.loads(body.decode("utf-8"))
        assert (data["image_url"]) == ("data:image/png;base64,b64data")
        assert ("reference_images") not in (data)
        assert ("size") not in (data)
        assert ("width") not in (data)
        assert ("height") not in (data)
        assert (data["aspect_ratio"]) == ("1:1")

class TestImageService:
    def test_endpoint_provider_with_none_config(self):
        """ImageService(..., None) must not call .get on None (regression: generate_image / endpoint)."""
        mock_ctx = MagicMock()
        api = {
            "endpoint": "https://api.example/v1",
            "api_key": "k",
            "is_openrouter": False,
        }
        with (
            patch("plugin.framework.config.get_api_config", return_value=api.copy()),
            patch("plugin.framework.client.model_fetcher.get_image_model", return_value="image-model-fallback"),
        ):
            service = ImageService(mock_ctx, None)
            provider = service.get_provider("endpoint")
            assert isinstance(provider, EndpointImageProvider)
            assert (provider.model) == ("image-model-fallback")

        with (
            patch("plugin.framework.config.get_api_config", return_value=api.copy()),
            patch("plugin.framework.client.model_fetcher.get_image_model", return_value="image-model-fallback"),
        ):
            service = ImageService(mock_ctx, {"image_model": "  my-image-model  "})
            provider = service.get_provider("endpoint")
            assert (provider.model) == ("my-image-model")

    def test_openrouter_image_request_uses_resolved_model(self):
        """Per-request image_model must reach make_chat_request (OpenRouter modalities path)."""
        mock_ctx = MagicMock()
        api = {
            "endpoint": "https://openrouter.ai/api/v1",
            "api_key": "k",
            "is_openrouter": True,
            "model": "default-init-model",
        }
        provider = EndpointImageProvider(api, mock_ctx)
        captured: dict[str, object] = {}

        def fake_make_chat_request(messages, max_tokens=512, tools=None, stream=False, model=None, **kw):
            captured["model"] = model
            return "POST", "/v1/chat/completions", '{"model":"wrong","messages":[]}', {}

        def fake_request_with_tools(messages, body_override=None, model=None, **kw):
            captured["rwt_model"] = model
            return {"content": "", "images": []}

        with (
            patch.object(provider.client, "make_chat_request", side_effect=fake_make_chat_request),
            patch.object(provider.client, "request_with_tools", side_effect=fake_request_with_tools),
        ):
            provider.generate("a dog", width=512, height=512, image_model="multimodal-custom-model")

        assert (captured.get("model")) == ("multimodal-custom-model")
        assert (captured.get("rwt_model")) == ("multimodal-custom-model")

    def test_openrouter_image_request_dedicated_routing(self):
        """Image models should route to image_completion on OpenRouter."""
        mock_ctx = MagicMock()
        api = {
            "endpoint": "https://openrouter.ai/api/v1",
            "api_key": "k",
            "is_openrouter": True,
            "model": "default-init-model",
        }
        provider = EndpointImageProvider(api, mock_ctx)
        captured = {}

        def fake_image_completion(prompt, model=None, width=512, height=512, steps=None, source_image=None):
            captured["prompt"] = prompt
            captured["model"] = model
            captured["width"] = width
            captured["height"] = height
            return [base64.b64encode(b"fake").decode()]

        with (
            patch("plugin.framework.client.model_fetcher.is_image_only_model", return_value=True),
            patch.object(provider.client, "image_completion", side_effect=fake_image_completion)
        ):
            provider.generate("a dog", width=1024, height=1024, image_model="black-forest-labs/flux.2-klein-4b")

        assert (captured.get("prompt")) == ("a dog")
        assert (captured.get("model")) == ("black-forest-labs/flux.2-klein-4b")
        assert (captured.get("width")) == (1024)
        assert (captured.get("height")) == (1024)

    def test_openrouter_chat_path_sends_image_config_aspect(self):
        """Gemini multimodal create must hint aspect_ratio / 0.5K via image_config."""
        mock_ctx = MagicMock()
        api = {
            "endpoint": "https://openrouter.ai/api/v1",
            "api_key": "k",
            "is_openrouter": True,
            "model": "google/gemini-3.1-flash-lite-image",
        }
        provider = EndpointImageProvider(api, mock_ctx)
        captured: dict[str, object] = {}

        def fake_make_chat_request(messages, max_tokens=512, tools=None, stream=False, model=None, **kw):
            return "POST", "/v1/chat/completions", '{"model":"m","messages":[]}', {}

        def fake_request_with_tools(messages, body_override=None, model=None, **kw):
            captured["body"] = json.loads(body_override)
            return {"content": "", "images": []}

        with (
            patch("plugin.framework.client.model_fetcher.is_image_only_model", return_value=False),
            patch.object(provider.client, "make_chat_request", side_effect=fake_make_chat_request),
            patch.object(provider.client, "request_with_tools", side_effect=fake_request_with_tools),
        ):
            provider.generate(
                "a tabby cat",
                width=512,
                height=512,
                aspect_ratio="square",
                image_model="google/gemini-3.1-flash-lite-image",
            )

        body = captured["body"]
        assert isinstance(body, dict)
        assert (body["modalities"]) == (["image"])
        assert (body["image_config"]) == ({"aspect_ratio": "1:1", "image_size": "0.5K"})
        assert ("size") not in (body)

    def test_openrouter_chat_edit_omits_image_config(self):
        """Img2img must not send sidebar size/aspect; source image defines geometry."""
        mock_ctx = MagicMock()
        api = {
            "endpoint": "https://openrouter.ai/api/v1",
            "api_key": "k",
            "is_openrouter": True,
            "model": "google/gemini-3.1-flash-lite-image",
        }
        provider = EndpointImageProvider(api, mock_ctx)
        captured: dict[str, object] = {}

        def fake_make_chat_request(messages, max_tokens=512, tools=None, stream=False, model=None, **kw):
            return "POST", "/v1/chat/completions", '{"model":"m","messages":[]}', {}

        def fake_request_with_tools(messages, body_override=None, model=None, **kw):
            captured["body"] = json.loads(body_override)
            return {"content": "", "images": []}

        tiny_png_b64 = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
        with (
            patch("plugin.framework.client.model_fetcher.is_image_only_model", return_value=False),
            patch.object(provider.client, "make_chat_request", side_effect=fake_make_chat_request),
            patch.object(provider.client, "request_with_tools", side_effect=fake_request_with_tools),
        ):
            provider.generate(
                "make it fancier",
                width=512,
                height=512,
                aspect_ratio="square",
                source_image=tiny_png_b64,
                image_model="google/gemini-3.1-flash-lite-image",
            )

        body = captured["body"]
        assert isinstance(body, dict)
        assert (body["modalities"]) == (["image"])
        assert ("image_config") not in (body)


class TestCanonicalAspectRatio:
    def test_canonical_aspect_ratio_named_and_pixels(self):
        assert (canonical_aspect_ratio(named="square")) == ("1:1")
        assert (canonical_aspect_ratio(named="Square")) == ("1:1")
        assert (canonical_aspect_ratio(named="Landscape (16:9)")) == ("16:9")
        assert (canonical_aspect_ratio(named="landscape_3_2")) == ("3:2")
        assert (canonical_aspect_ratio(1024, 1024)) == ("1:1")
        assert (canonical_aspect_ratio(1024, 576)) == ("16:9")
        assert (canonical_aspect_ratio(896, 512)) == ("16:9")
        assert (canonical_aspect_ratio(1024, 768)) == ("4:3")
        assert (canonical_aspect_ratio(768, 1024)) == ("3:4")

    def test_canonical_resolution_tiers_and_clamps(self):
        assert (canonical_resolution(512, 512)) == ("512")
        assert (canonical_resolution(1024, 1024)) == ("1K")
        assert (canonical_resolution(2048, 2048)) == ("2K")
        assert (canonical_resolution(4096, 4096)) == ("4K")
        assert (canonical_resolution(512, 512, family="openrouter_chat")) == ("0.5K")
        assert (canonical_resolution(1024, 1024, family="openrouter_chat")) == ("1K")
        assert (canonical_resolution(2048, 2048, family="openrouter_chat")) == ("2K")
        assert (canonical_resolution(4096, 4096, family="openrouter_chat")) == ("4K")
        assert (canonical_resolution(512, 512, family="grok")) == ("1k")
        assert (canonical_resolution(1024, 1024, family="grok")) == ("1k")
        assert (canonical_resolution(2048, 2048, family="grok")) == ("2k")
        assert (canonical_resolution(4096, 4096, family="grok")) == ("2k")
        assert (canonical_resolution(512, 512, family="imagen")) == ("1K")
        assert (canonical_resolution(1024, 768, family="imagen")) == ("1K")
        assert (canonical_resolution(2048, 2048, family="imagen")) == ("2K")
        assert (canonical_resolution(4096, 4096, family="imagen")) == ("2K")
        assert (canonical_resolution(0, 512)) is None


