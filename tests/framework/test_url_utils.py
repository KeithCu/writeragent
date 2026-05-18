import pytest
import unittest
from plugin.framework.url_utils import normalize_endpoint_url, get_api_version_suffix, get_url_query_dict

class TestNormalizeEndpointUrl():

    def test_strips_trailing_v1(self):
        assert (normalize_endpoint_url('https://api.example.com/v1') == 'https://api.example.com')
        assert (normalize_endpoint_url('https://api.example.com/v1/') == 'https://api.example.com')
        assert (normalize_endpoint_url('https://openrouter.ai/api/v1') == 'https://openrouter.ai/api')

    def test_preserves_v1beta_and_similar(self):
        u = 'https://generativelanguage.googleapis.com/v1beta/openai'
        assert (normalize_endpoint_url(u) == u)

    def test_empty_and_whitespace(self):
        assert (normalize_endpoint_url('') == '')
        assert (normalize_endpoint_url('  ') == '')

    def test_zai_normalization(self):
        # Test that /v4 is stripped for Z.ai
        assert normalize_endpoint_url("https://api.z.ai/v4") == "https://api.z.ai"
        assert normalize_endpoint_url("https://z.ai/v4") == "https://z.ai"
        # Test that /v4 is stripped even for deeper paths (Z.ai coding endpoint)
        assert normalize_endpoint_url("https://api.z.ai/api/coding/paas/v4") == "https://api.z.ai/api/coding/paas"
        # Test that /v1 is also stripped for Z.ai (as a fallback)
        assert normalize_endpoint_url("https://api.z.ai/v1") == "https://api.z.ai"

    def test_openwebui_normalization(self):
        # Test that /api is stripped when is_openwebui is True
        assert normalize_endpoint_url("http://localhost:3000/api", is_openwebui=True) == "http://localhost:3000"
        # Test that /api is NOT stripped when is_openwebui is False
        assert normalize_endpoint_url("http://localhost:3000/api", is_openwebui=False) == "http://localhost:3000/api"

class TestApiVersionSuffix():

    def test_zai_suffix(self):
        assert get_api_version_suffix("https://api.z.ai") == "/v4"
        assert get_api_version_suffix("https://z.ai") == "/v4"
        assert get_api_version_suffix("https://other-api.com") == "/v1"

    def test_openwebui_suffix(self):
        assert get_api_version_suffix("http://localhost:3000", is_openwebui=True) == "/api"
        assert get_api_version_suffix("http://localhost:3000", is_openwebui=False) == "/v1"

class TestGetUrlQueryDict:
    def test_normal_query(self):
        url = "https://example.com?a=1&b=2"
        assert get_url_query_dict(url) == {'a': ['1'], 'b': ['2']}

    def test_multiple_values(self):
        url = "https://example.com?a=1&a=2"
        assert get_url_query_dict(url) == {'a': ['1', '2']}

    def test_no_query(self):
        url = "https://example.com"
        assert get_url_query_dict(url) == {}

    def test_encoded_characters(self):
        url = "https://example.com?q=hello%20world"
        assert get_url_query_dict(url) == {'q': ['hello world']}

    def test_empty_input(self):
        assert get_url_query_dict("") == {}
