from plugin.framework.client.base_provider_shim import BaseProviderShim, adjust_image_body_for_rejection

def test_adjust_image_body_for_rejection():
    # Test that regex trailing JSON junk is not captured
    body = b'{"resolution": "1K", "aspect_ratio": "16:9"}'

    # Error message contains trailing JSON syntax from being embedded in a JSON response
    error_text = "resolution: not supported. Accepted: 2K\"}"

    new_body = adjust_image_body_for_rejection(body, error_text)
    assert new_body is not None
    import json
    new_data = json.loads(new_body)
    assert new_data["resolution"] == "2K" # and not "2K\"}"


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

