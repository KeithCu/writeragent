import sys
from plugin.framework.utils import get_plugin_dir
import os
import unittest
import json
import base64
from unittest.mock import MagicMock, patch

# Add parent directory to path to import core
sys.path.insert(0, os.path.dirname(get_plugin_dir()))

from plugin.framework.image_utils import EndpointImageProvider
from plugin.modules.http.client import LlmClient
from plugin.tests.testing_utils import MockContext, create_mock_client, create_mock_http_response

class TestEndpointImageProvider(unittest.TestCase):
    def setUp(self):
        self.mock_ctx = MockContext()
        self.api_config = {"model": "test-model"}
        with patch('plugin.framework.image_utils.LlmClient') as mock_client_cls:
            self.provider = EndpointImageProvider(self.api_config, self.mock_ctx)
            self.mock_client = self.provider.client

    @patch('plugin.framework.image_utils.sync_request')
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
        
        self.assertEqual(len(paths), 1)
        self.assertEqual(err, "")
        self.assertTrue(paths[0].endswith(".webp"))
        mock_sync.assert_called_once_with("http://example.com/image.png", parse_json=False)

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
        
        self.assertEqual(len(paths), 1)
        self.assertEqual(err, "")
        self.assertTrue(paths[0].endswith(".png"))
        with open(paths[0], 'rb') as f:
            self.assertEqual(f.read(), b"fake-image-data-b64")
        os.unlink(paths[0])

    def test_generate_standard_b64(self):
        self.mock_client.config.get.return_value = False # Not OpenRouter
        self.mock_client.make_image_request.return_value = ("POST", "/images", "{}", {})
        
        # Mock standard connection and response
        mock_conn = MagicMock()
        self.mock_client._get_connection.return_value = mock_conn
        b64_data = base64.b64encode(b"standard-b64-data").decode()
        mock_http_resp = create_mock_http_response(200, {"data": [{"b64_json": b64_data}]})
        mock_conn.getresponse.return_value = mock_http_resp

        paths, err = self.provider.generate("test prompt")
        
        self.assertEqual(len(paths), 1)
        self.assertEqual(err, "")
        self.assertTrue(paths[0].endswith(".png"))
        with open(paths[0], 'rb') as f:
            self.assertEqual(f.read(), b"standard-b64-data")
        os.unlink(paths[0])

    @patch('plugin.framework.image_utils.sync_request')
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
        
        self.assertEqual(len(paths), 1)
        self.assertEqual(err, "")
        mock_sync.assert_called_with("http://fallback.com/image.png", parse_json=False)
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
        
        self.assertEqual(len(paths), 1)
        self.assertEqual(err, "")
        with open(paths[0], 'rb') as f:
            self.assertEqual(f.read(), b"fallback-b64-data")
        os.unlink(paths[0])

    def test_scoping_bug_fix_verification(self):
        """
        Verifies that the scoping bug is fixed. 
        Previously, 'response' in the fallback block would be an HTTPResponse 
        if the standard path was taken, causing a crash.
        """
        self.mock_client.config.get.return_value = False # Standard path
        self.mock_client.make_image_request.return_value = ("POST", "/images", "{}", {})
        
        # Mock standard connection and response that returns no images in data
        mock_conn = MagicMock()
        self.mock_client._get_connection.return_value = mock_conn
        mock_http_resp = create_mock_http_response(200, {"data": []}) # No images
        mock_conn.getresponse.return_value = mock_http_resp

        # This should NOT crash now, even if fallback fails to find anything.
        # It should just return [].
        try:
            result = self.provider.generate("test prompt")
            self.assertEqual(result, ([], ""))
        except AttributeError as e:
            self.fail(f"Scoping bug still present! AttributeError: {e}")

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
        self.assertEqual(paths, [])
        self.assertEqual(err, "")

    @patch('plugin.framework.image_utils.sync_request')
    def test_generate_multi_image(self, mock_sync):
        """If provider returns multiple images, ensure paths preserves ordering and all paths are created/cleaned."""
        self.mock_client.config.get.return_value = False # Not OpenRouter
        self.mock_client.make_image_request.return_value = ("POST", "/images", "{}", {})

        # Mock standard connection and response with multiple images
        mock_conn = MagicMock()
        self.mock_client._get_connection.return_value = mock_conn

        b64_data = base64.b64encode(b"multi-image-b64-data").decode()
        mock_sync.return_value = b"multi-image-url-data"

        mock_http_resp = create_mock_http_response(200, {"data": [
            {"b64_json": b64_data},
            {"url": "http://example.com/multi_image.png"}
        ]})
        mock_conn.getresponse.return_value = mock_http_resp

        paths, err = self.provider.generate("test prompt")

        self.assertEqual(len(paths), 2)
        self.assertEqual(err, "")

        self.assertTrue(paths[0].endswith(".png"))
        with open(paths[0], 'rb') as f:
            self.assertEqual(f.read(), b"multi-image-b64-data")

        self.assertTrue(paths[1].endswith(".webp"))
        mock_sync.assert_called_once_with("http://example.com/multi_image.png", parse_json=False)

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

        self.assertEqual(paths, [])
        self.assertEqual(err, "")

    @patch('plugin.framework.image_utils.LlmClient')
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
        self.assertEqual(len(call_messages), 1)
        content = call_messages[0]["content"]
        self.assertIsInstance(content, list)
        self.assertEqual(content[0], {"type": "text", "text": "edit prompt"})
        self.assertEqual(content[1]["type"], "image_url")
        self.assertEqual(content[1]["image_url"]["url"], "data:image/png;base64," + b64)

    @patch('plugin.framework.image_utils.LlmClient')
    def test_edit_image_standard_endpoint_passes_source_image(self, mock_client_cls):
        """When not OpenRouter and source_image is set, make_image_request is called with source_image (img2img)."""
        mock_client = create_mock_client()
        mock_client.make_image_request.return_value = ("POST", "/images", "{}", {})
        mock_conn = MagicMock()
        mock_client._get_connection.return_value = mock_conn
        mock_http_resp = create_mock_http_response(200, {"data": [{"b64_json": base64.b64encode(b"edited").decode()}]})
        mock_conn.getresponse.return_value = mock_http_resp
        mock_client_cls.return_value = mock_client
        provider = EndpointImageProvider({"model": "test"}, MockContext())
        provider.client = mock_client

        b64 = "xyz789"
        provider.generate("edit prompt", source_image=b64)

        mock_client.make_image_request.assert_called_once()
        kwargs = mock_client.make_image_request.call_args[1]
        self.assertEqual(kwargs.get("source_image"), b64)

    @patch('plugin.modules.http.client.init_logging')
    def test_make_image_request_body_includes_image_url_when_source_image(self, mock_init):
        """LlmClient.make_image_request adds image_url (data URL) to body when source_image is provided."""
        config = {"endpoint": "https://api.example.com", "model": "test-model"}
        client = LlmClient(config, MockContext())
        method, path, body, headers = client.make_image_request("a cat", source_image="b64data")
        data = json.loads(body.decode("utf-8"))
        self.assertIn("image_url", data)
        self.assertEqual(data["image_url"], "data:image/png;base64,b64data")

if __name__ == '__main__':
    unittest.main()
