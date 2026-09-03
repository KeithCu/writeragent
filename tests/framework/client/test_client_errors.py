import unittest

from plugin.framework.client.errors import (
    _format_http_error_response,
    append_zai_unknown_model_hint,
    is_local_model_server_crash,
    local_model_overflow_message,
)
from plugin.framework.config import validate_api_config


class TestZaiUnknownModelHint(unittest.TestCase):

    def test_hint_on_general_endpoint_unknown_model(self):
        msg = "HTTP Error 400 from AI Provider: Bad Request. Unknown Model"
        err_body = '{"error":{"code":"1211","message":"Unknown Model, please check the model code."}}'
        out = append_zai_unknown_model_hint(msg, err_body, "/api/paas/v4/chat/completions", "zai", "glm-5.2")
        self.assertIn("Coding Plan", out)
        self.assertIn("api/coding/paas/v4", out)
        self.assertIn("glm-5.2", out)

    def test_no_hint_on_coding_endpoint(self):
        msg = "HTTP Error 400"
        err_body = '{"error":{"code":"1211","message":"Unknown Model"}}'
        out = append_zai_unknown_model_hint(msg, err_body, "/api/coding/paas/v4/chat/completions", "zai", "glm-5.2")
        self.assertEqual(out, msg)

    def test_no_hint_for_other_providers(self):
        msg = "HTTP Error 400"
        err_body = '{"error":{"code":"1211","message":"Unknown Model"}}'
        out = append_zai_unknown_model_hint(msg, err_body, "/api/paas/v4/chat/completions", "openai", "gpt-4o")
        self.assertEqual(out, msg)


class TestLlamaServerOverflowSentence(unittest.TestCase):
    """Issue #570: sidebar sentence names overflow; no raw HTTP 500 dict."""

    _CRASH_BODY = (
        '{"error":{"message":"llama-server process has terminated: '
        'exit status 0xc0000005: The instruction at 0xp referenced memory."}}'
    )

    def test_format_names_4k_when_window_known(self):
        msg = _format_http_error_response(
            500, "Internal Server Error", self._CRASH_BODY, context_window=4096
        )
        self.assertIn("Ollama/llama.cpp", msg)
        self.assertIn("4K", msg)
        self.assertIn("overflowed", msg)
        self.assertNotIn("HTTP Error 500", msg)
        self.assertNotIn("0xc0000005", msg)
        self.assertNotIn("{'status'", msg)

    def test_format_without_window_still_explains_overflow(self):
        msg = _format_http_error_response(500, "Internal Server Error", self._CRASH_BODY)
        self.assertEqual(msg, local_model_overflow_message())
        self.assertIn("too-small context", msg)
        self.assertNotIn("HTTP Error 500", msg)

    def test_generic_500_keeps_provider_detail(self):
        body = '{"error":{"message":"boom"}}'
        msg = _format_http_error_response(500, "Internal Server Error", body)
        self.assertIn("HTTP Error 500", msg)
        self.assertIn("boom", msg)

    def test_markers_detected(self):
        self.assertTrue(is_local_model_server_crash(self._CRASH_BODY))
        self.assertTrue(is_local_model_server_crash("truncating input prompt limit=4095"))
        self.assertFalse(is_local_model_server_crash("HTTP Error 500 from AI Provider: boom"))


class TestValidateApiConfigPlaceholders(unittest.TestCase):

    def test_rejects_connection_failed_placeholder(self):
        ok, err = validate_api_config({
            "endpoint": "https://api.z.ai/api/paas",
            "model": "(Connection failed)",
        })
        self.assertFalse(ok)
        self.assertIn("valid model", err.lower())

    def test_accepts_real_model(self):
        ok, err = validate_api_config({
            "endpoint": "https://api.z.ai/api/paas",
            "model": "glm-5.2",
        })
        self.assertTrue(ok)
        self.assertEqual(err, "")
