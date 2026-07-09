import unittest

from plugin.framework.client.errors import append_zai_unknown_model_hint
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
