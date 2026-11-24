import pytest

from plugin.framework.client.provider_detection import get_provider_from_endpoint, is_local_host

def test_get_provider_from_endpoint():
    assert get_provider_from_endpoint("https://api.together.xyz/v1") == "together"
    assert get_provider_from_endpoint("https://api.together.ai/v1") == "together"

def test_is_local_host():
    assert is_local_host("localhost") is True
    assert is_local_host("127.0.0.1") is True
    assert is_local_host("192.168.1.5") is True
    assert is_local_host("100.64.0.1") is False
    assert is_local_host("100.127.255.254") is False
    assert is_local_host("8.8.8.8") is False


@pytest.mark.parametrize(
    ("endpoint", "secrets"),
    [
        (
            "https://user:Hunter2pw@h.example:1a34/s/AbCdEfGhIjKlMnOpQr/v1",
            ("hunter2pw", "abcdefghijklmnopqr"),
        ),
        ("https://user:Hunter2pw@[::1/v1", ("hunter2pw",)),
        ("https://user:Hunter2pw@ho\u2100st/x", ("hunter2pw",)),
    ],
)
def test_invalid_endpoint_details_hide_secrets(endpoint, secrets):
    import traceback
    from plugin.framework.errors import ConfigError

    with pytest.raises(ConfigError) as exc_info:
        get_provider_from_endpoint(endpoint)
    assert exc_info.value.code == "CONFIG_INVALID_URL"
    shown = exc_info.value.details["endpoint"].lower()
    tb = "".join(traceback.format_exception(exc_info.value)).lower()
    for secret in secrets:
        assert secret not in shown
        assert secret not in tb
