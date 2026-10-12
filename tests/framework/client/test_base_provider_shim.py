import json

import pytest

from plugin.framework.client.base_provider_shim import BaseProviderShim, adjust_image_body_for_rejection, coerce_image_data_url, coerce_raw_b64

def test_adjust_image_body_for_rejection():
    # Test that regex trailing JSON junk is not captured
    body = b'{"resolution": "1K", "aspect_ratio": "16:9"}'

    # Error message contains trailing JSON syntax from being embedded in a JSON response
    error_text = "resolution: not supported. Accepted: 2K\"}"

    new_body = adjust_image_body_for_rejection(body, error_text)
    assert new_body is not None
    new_data = json.loads(new_body)
    assert new_data["resolution"] == "2K" # and not "2K\"}"


def test_adjust_image_body_keeps_value_when_one_provider_omits_accepted():
    # A line with no Accepted list must not force a delete when another
    # line names a value. The retry uses that value.
    body = b'{"resolution": "512"}'
    error_text = "resolution: not supported. Accepted: 1K | resolution: not supported."
    new_body = adjust_image_body_for_rejection(body, error_text)
    assert new_body is not None
    assert json.loads(new_body)["resolution"] == "1K"


def test_parse_sync_response_null_usage():
    class DummyClient:
        def _get_provider(self): return "generic"
        def _endpoint(self): return "http://test"
        def _api_path(self): return "/v1"
        def _headers(self): return {}
    shim = BaseProviderShim(DummyClient())

    # When usage is None ("usage": null in JSON), it must normalize to {}
    resp_null_usage = {
        "choices": [{"message": {"role": "assistant", "content": "hello"}}],
        "usage": None,
    }
    _, _, _, usage, _, _ = shim.parse_sync_response(resp_null_usage)
    assert usage == {}

    # When usage is omitted entirely, it defaults to {}
    resp_no_usage = {
        "choices": [{"message": {"role": "assistant", "content": "hello"}}],
    }
    _, _, _, usage, _, _ = shim.parse_sync_response(resp_no_usage)
    assert usage == {}

    # When usage is present as a dict, it is returned intact
    resp_with_usage = {
        "choices": [{"message": {"role": "assistant", "content": "hello"}}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 20},
    }
    _, _, _, usage, _, _ = shim.parse_sync_response(resp_with_usage)
    assert usage == {"prompt_tokens": 10, "completion_tokens": 20}

    # A string images value used to be returned as-is and iterated per character.
    content, _finish, _tools, _usage, images, _message = shim.parse_sync_response({"choices": [{"message": {"role": "assistant", "content": "hi", "images": "http://x"}}]})
    assert content == "hi"
    assert images == []
    assert shim.parse_image_responses(["nope"]) == []


def test_coerce_image_data_url_keeps_any_data_url():
    # A non-image data URL used to get a second data:image/png prefix.
    assert coerce_image_data_url("data:application/octet-stream;base64,aaa") == "data:application/octet-stream;base64,aaa"
    assert coerce_image_data_url("data:image/jpeg;base64,abc") == "data:image/jpeg;base64,abc"
    # Raw selection bytes are PNG exports, so the label stays image/png.
    assert coerce_image_data_url(source_image="aaa") == "data:image/png;base64,aaa"


def test_coerce_raw_b64_rejects_http_url():
    # An https URL used to pass through into Ollama images[] and Gemini
    # inlineData, which both require raw base64.
    with pytest.raises(ValueError, match="http"):
        coerce_raw_b64("https://example.com/a.png")
    assert coerce_raw_b64("data:image/png;base64,aaa") == "aaa"
    assert coerce_raw_b64(source_image="aaa") == "aaa"
    # A data URL with no comma stays the whole string.
    assert coerce_raw_b64("data:image/png;base64") == "data:image/png;base64"


class _DummyClient:
    def _endpoint(self):
        return "http://test"

    def _api_path(self):
        return "/v1"

    def _headers(self):
        return {}


def test_image_payload_omits_size_when_axis_is_zero():
    # (n // 64) * 64 and an explicit width=0 used to send size "0x512".
    shim = BaseProviderShim(_DummyClient())
    _method, _path, body, _headers = shim.build_image_request("cat", "flux", 0, 512)
    data = json.loads(body)
    assert "size" not in data
    assert data["prompt"] == "cat"
    assert data["response_format"] == "b64_json"

    _method, _path, body, _headers = shim.build_image_request("cat", "flux", 512, 0)
    assert "size" not in json.loads(body)


def test_parse_response_chunk_flattens_list_content():
    class DummyClient:
        def _get_provider(self):
            return "generic"

        def _endpoint(self):
            return "http://test"

        def _api_path(self):
            return "/v1"

        def _headers(self):
            return {}

    shim = BaseProviderShim(DummyClient())
    chunk = {"choices": [{"delta": {"content": [{"type": "text", "text": "hi"}]}}]}
    content, _finish, _thinking, _delta = shim.parse_response_chunk(chunk)
    assert content == "hi"

