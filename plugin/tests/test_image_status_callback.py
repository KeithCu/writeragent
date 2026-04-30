
import sys
from plugin.framework.utils import get_plugin_dir
import os
import unittest
from unittest.mock import MagicMock, patch

# Add current directory to path
sys.path.insert(0, get_plugin_dir())

from plugin.framework.image_utils import ImageService, AIHordeImageProvider, EndpointImageProvider

class TestImageStatusCallback(unittest.TestCase):
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
            patch("plugin.framework.config.get_image_model", return_value="image-model-fallback"),
        ):
            service = ImageService(mock_ctx, None)
            provider = service.get_provider("endpoint")
            self.assertIsInstance(provider, EndpointImageProvider)
            self.assertEqual(provider.model, "image-model-fallback")

        with (
            patch("plugin.framework.config.get_api_config", return_value=api.copy()),
            patch("plugin.framework.config.get_image_model", return_value="image-model-fallback"),
        ):
            service = ImageService(mock_ctx, {"image_model": "  my-image-model  "})
            provider = service.get_provider("endpoint")
            self.assertEqual(provider.model, "my-image-model")

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
            import json as _json

            d = _json.loads(body_override) if body_override else {}
            return {"content": "", "images": []}

        with (
            patch.object(provider.client, "make_chat_request", side_effect=fake_make_chat_request),
            patch.object(provider.client, "request_with_tools", side_effect=fake_request_with_tools),
        ):
            provider.generate("a dog", width=512, height=512, image_model="black-forest-labs/flux.2-klein-4b")

        self.assertEqual(captured.get("model"), "black-forest-labs/flux.2-klein-4b")
        self.assertEqual(captured.get("rwt_model"), "black-forest-labs/flux.2-klein-4b")

    def test_status_callback_propagation(self):
        # Mock context and config
        mock_ctx = MagicMock()
        mock_ctx.ServiceManager.createInstanceWithContext.return_value = MagicMock() # Toolkit
        
        config = {"aihorde_api_key": "test_key", "image_provider": "aihorde"}
        
        # Instantiate ImageService
        service = ImageService(mock_ctx, config)
        
        # Mock the callback
        status_callback = MagicMock()
        
        # Verify provider creation
        provider = service.get_provider("aihorde")
        self.assertIsInstance(provider, AIHordeImageProvider)
        
        # Verify SimpleInformer setup
        informer = provider.client.informer
        self.assertIsNotNone(informer)
        
        # Simulate generate call
        # Mock AiHordeClient.generate_image to call informer.update_status
        def mock_generate_image(options):
            # Simulate progress
            informer.update_status("Starting...", 0)
            informer.update_status("Generating...", 50)
            return ["/tmp/image.png"]
            
        with patch.object(service, 'get_provider', return_value=provider):
            with patch.object(provider.client, 'generate_image', side_effect=mock_generate_image):
                result = service.generate_image("test prompt", status_callback=status_callback)
                
                # Assert calls
                self.assertEqual(result, (["/tmp/image.png"], ""))
                
                # Check callback calls
                status_callback.assert_any_call("Horde: Starting... (0%)")
                status_callback.assert_any_call("Horde: Generating... (50%)")
                print("Status callback successfully invoked!")

    def test_provider_failure_shape(self):
        mock_ctx = MagicMock()
        mock_ctx.ServiceManager.createInstanceWithContext.return_value = MagicMock()
        config = {"aihorde_api_key": "test_key", "image_provider": "aihorde"}
        service = ImageService(mock_ctx, config)

        provider = service.get_provider("aihorde")
        informer = provider.client.informer

        def mock_generate_image_error(options):
            informer.update_status("Starting...", 0)
            raise ValueError("Test provider failure")

        with patch.object(service, 'get_provider', return_value=provider):
            with patch.object(provider.client, 'generate_image', side_effect=mock_generate_image_error):
                paths, error = service.generate_image("test prompt", status_callback=MagicMock())

                self.assertEqual(paths, [])
                self.assertIn("Test provider failure", error)

    def test_callback_optionality(self):
        mock_ctx = MagicMock()
        mock_ctx.ServiceManager.createInstanceWithContext.return_value = MagicMock()
        config = {"aihorde_api_key": "test_key", "image_provider": "aihorde"}
        service = ImageService(mock_ctx, config)

        provider = service.get_provider("aihorde")
        informer = provider.client.informer

        def mock_generate_image(options):
            # This should not crash even without a callback
            informer.update_status("Starting...", 0)
            informer.show_error("Some warning")
            return ["/tmp/image.png"]

        with patch.object(service, 'get_provider', return_value=provider):
            with patch.object(provider.client, 'generate_image', side_effect=mock_generate_image):
                result = service.generate_image("test prompt", status_callback=None)

                self.assertEqual(result, (["/tmp/image.png"], ""))

    def test_ordering_and_formatting(self):
        from unittest.mock import call
        mock_ctx = MagicMock()
        mock_ctx.ServiceManager.createInstanceWithContext.return_value = MagicMock()
        config = {"aihorde_api_key": "test_key", "image_provider": "aihorde"}
        service = ImageService(mock_ctx, config)

        status_callback = MagicMock()
        provider = service.get_provider("aihorde")
        informer = provider.client.informer

        def mock_generate_image(options):
            informer.update_status("Initializing", 0)
            informer.update_status("Processing", 50)
            informer.update_status("Refining", 10)
            informer.update_status("Finishing", 100)
            return ["/tmp/image.png"]

        with patch.object(service, 'get_provider', return_value=provider):
            with patch.object(provider.client, 'generate_image', side_effect=mock_generate_image):
                result = service.generate_image("test prompt", status_callback=status_callback)

                self.assertEqual(result, (["/tmp/image.png"], ""))

                expected_calls = [
                    call("Horde: Initializing (0%)"),
                    call("Horde: Processing (50%)"),
                    call("Horde: Refining (10%)"),
                    call("Horde: Finishing (100%)"),
                ]
                self.assertEqual(status_callback.call_args_list, expected_calls)

if __name__ == '__main__':
    unittest.main()
