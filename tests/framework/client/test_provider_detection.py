import pytest
from plugin.framework.client.provider_detection import get_provider_from_endpoint, is_local_host

def test_get_provider_from_endpoint():
    assert get_provider_from_endpoint("https://api.together.xyz/v1") == "together"
    assert get_provider_from_endpoint("https://api.together.ai/v1") == "together"

def test_is_local_host():
    assert is_local_host("localhost") is True
    assert is_local_host("127.0.0.1") is True
    assert is_local_host("192.168.1.5") is True
    assert is_local_host("100.64.0.1") is True
    assert is_local_host("100.127.255.254") is True
    assert is_local_host("8.8.8.8") is False
