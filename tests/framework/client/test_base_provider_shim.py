from plugin.framework.client.base_provider_shim import adjust_image_body_for_rejection

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
