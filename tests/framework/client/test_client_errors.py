
from plugin.framework.client.errors import (
    _format_http_error_response,
    append_zai_unknown_model_hint,
    is_local_model_server_crash,
    local_model_overflow_message,
)
from plugin.framework.config import validate_api_config


class TestZaiUnknownModelHint:

    def test_hint_on_general_endpoint_unknown_model(self):
        msg = "HTTP Error 400 from AI Provider: Bad Request. Unknown Model"
        err_body = '{"error":{"code":"1211","message":"Unknown Model, please check the model code."}}'
        out = append_zai_unknown_model_hint(msg, err_body, "/api/paas/v4/chat/completions", "zai", "glm-5.2")
        assert ("Coding Plan") in (out)
        assert ("api/coding/paas/v4") in (out)
        assert ("glm-5.2") in (out)

    def test_no_hint_on_coding_endpoint(self):
        msg = "HTTP Error 400"
        err_body = '{"error":{"code":"1211","message":"Unknown Model"}}'
        out = append_zai_unknown_model_hint(msg, err_body, "/api/coding/paas/v4/chat/completions", "zai", "glm-5.2")
        assert (out) == (msg)

    def test_no_hint_for_other_providers(self):
        msg = "HTTP Error 400"
        err_body = '{"error":{"code":"1211","message":"Unknown Model"}}'
        out = append_zai_unknown_model_hint(msg, err_body, "/api/paas/v4/chat/completions", "openai", "gpt-4o")
        assert (out) == (msg)


class TestLlamaServerOverflowSentence:
    """Issue #570: sidebar sentence names overflow; no raw HTTP 500 dict."""

    _CRASH_BODY = (
        '{"error":{"message":"llama-server process has terminated: '
        'exit status 0xc0000005: The instruction at 0xp referenced memory."}}'
    )

    def test_format_names_4k_when_window_known(self):
        msg = _format_http_error_response(
            500, "Internal Server Error", self._CRASH_BODY, context_window=4096
        )
        assert ("Ollama/llama.cpp") in (msg)
        assert ("4K") in (msg)
        assert ("overflowed") in (msg)
        assert ("HTTP Error 500") not in (msg)
        assert ("0xc0000005") not in (msg)
        assert ("{'status'") not in (msg)

    def test_format_without_window_still_explains_overflow(self):
        msg = _format_http_error_response(500, "Internal Server Error", self._CRASH_BODY)
        assert (msg) == (local_model_overflow_message())
        assert ("too-small context") in (msg)
        assert ("HTTP Error 500") not in (msg)

    def test_generic_500_keeps_provider_detail(self):
        body = '{"error":{"message":"boom"}}'
        msg = _format_http_error_response(500, "Internal Server Error", body)
        assert ("HTTP Error 500") in (msg)
        assert ("boom") in (msg)

    def test_markers_detected(self):
        assert (is_local_model_server_crash(self._CRASH_BODY))
        assert (is_local_model_server_crash("truncating input prompt limit=4095"))
        assert not (is_local_model_server_crash("HTTP Error 500 from AI Provider: boom"))


class TestValidateApiConfigPlaceholders:

    def test_rejects_connection_failed_placeholder(self):
        ok, err = validate_api_config({
            "endpoint": "https://api.z.ai/api/paas",
            "model": "(Connection failed)",
        })
        assert not (ok)
        assert ("valid model") in (err.lower())

    def test_accepts_real_model(self):
        ok, err = validate_api_config({
            "endpoint": "https://api.z.ai/api/paas",
            "model": "glm-5.2",
        })
        assert (ok)
        assert (err) == ("")
