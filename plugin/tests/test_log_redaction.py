# WriterAgent tests — log redaction (framework/logging.py)
import unittest

from plugin.framework.logging import (
    LOG_REDACT_AUDIO_PLACEHOLDER,
    LOG_REDACT_IMAGE_PLACEHOLDER,
    redact_sensitive_payload_for_log,
)


class TestLogRedaction(unittest.TestCase):
    def test_redact_chat_multimodal(self) -> None:
        messages = [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "hi"},
                    {
                        "type": "input_audio",
                        "input_audio": {"data": "AAAABBBB", "format": "wav"},
                    },
                    {
                        "type": "image_url",
                        "image_url": {"url": "data:image/jpeg;base64,ZZZZ"},
                    },
                ],
            }
        ]
        out = redact_sensitive_payload_for_log(messages)
        self.assertIsNot(out, messages)
        parts = out[0]["content"]
        self.assertEqual(parts[0], {"type": "text", "text": "hi"})
        self.assertEqual(parts[1]["input_audio"]["data"], LOG_REDACT_AUDIO_PLACEHOLDER % 8)
        self.assertEqual(
            parts[2]["image_url"]["url"],
            LOG_REDACT_IMAGE_PLACEHOLDER % len("data:image/jpeg;base64,ZZZZ"),
        )
        self.assertEqual(messages[0]["content"][1]["input_audio"]["data"], "AAAABBBB")

    def test_redact_image_generations_request_body(self) -> None:
        u = "data:image/png;base64," + "x" * 100
        data = {"prompt": "p", "image_url": u}
        r = redact_sensitive_payload_for_log(data)
        self.assertEqual(r["prompt"], "p")
        self.assertEqual(r["image_url"], LOG_REDACT_IMAGE_PLACEHOLDER % len(u))

    def test_redact_image_api_response_nested(self) -> None:
        raw = {
            "data": [
                {"b64_json": "Ym9n", "url": "http://ok"},
                {"url": "data:image/png;base64,QQ=="},
            ]
        }
        r = redact_sensitive_payload_for_log(raw)
        self.assertEqual(r["data"][0]["b64_json"], LOG_REDACT_IMAGE_PLACEHOLDER % 4)
        self.assertEqual(r["data"][0]["url"], "http://ok")
        self.assertEqual(
            r["data"][1]["url"],
            LOG_REDACT_IMAGE_PLACEHOLDER % len("data:image/png;base64,QQ=="),
        )


if __name__ == "__main__":
    unittest.main()
