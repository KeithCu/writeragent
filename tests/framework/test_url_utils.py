import unittest
from plugin.framework.url_utils import (
    _is_zai_host,
    dispatch_command_from_url,
    get_api_version_suffix,
    get_url_query_dict,
    matches_librepy_dispatch_url,
    normalize_endpoint_url,
    path_to_file_url,
)

class TestNormalizeEndpointUrl():

    def test_strips_trailing_v1(self):
        assert (normalize_endpoint_url('https://api.example.com/v1') == 'https://api.example.com')
        assert (normalize_endpoint_url('https://api.example.com/v1/') == 'https://api.example.com')
        assert (normalize_endpoint_url('https://openrouter.ai/api/v1') == 'https://openrouter.ai/api')

    def test_google_normalization(self):
        assert normalize_endpoint_url("https://generativelanguage.googleapis.com/v1beta/openai") == "https://generativelanguage.googleapis.com"
        assert normalize_endpoint_url("https://generativelanguage.googleapis.com/v1beta") == "https://generativelanguage.googleapis.com"
        assert normalize_endpoint_url("https://generativelanguage.googleapis.com/v1") == "https://generativelanguage.googleapis.com"
        assert normalize_endpoint_url("https://generativelanguage.googleapis.com") == "https://generativelanguage.googleapis.com"

    def test_google_chat_url_roundtrip(self):
        stored = normalize_endpoint_url("https://generativelanguage.googleapis.com/v1beta/openai")
        suffix = get_api_version_suffix(stored)
        assert stored + suffix + "/chat/completions" == "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions"
        assert stored + suffix + "/models" == "https://generativelanguage.googleapis.com/v1beta/openai/models"

    def test_empty_and_whitespace(self):
        assert (normalize_endpoint_url('') == '')
        assert (normalize_endpoint_url('  ') == '')

    def test_zai_normalization(self):
        # Legacy bare-host /v4 strips to host-only storage
        assert normalize_endpoint_url("https://api.z.ai/v4") == "https://api.z.ai"
        assert normalize_endpoint_url("https://z.ai/v4") == "https://z.ai"
        # General OpenAI-compatible base
        assert normalize_endpoint_url("https://api.z.ai/api/paas/v4") == "https://api.z.ai/api/paas"
        # Z.ai coding-plan endpoint
        assert normalize_endpoint_url("https://api.z.ai/api/coding/paas/v4") == "https://api.z.ai/api/coding/paas"
        # /v1 fallback strip for Z.ai
        assert normalize_endpoint_url("https://api.z.ai/v1") == "https://api.z.ai"

    def test_zai_chat_url_roundtrip(self):
        stored = normalize_endpoint_url("https://api.z.ai/api/paas/v4")
        suffix = get_api_version_suffix(stored)
        assert stored + suffix + "/chat/completions" == "https://api.z.ai/api/paas/v4/chat/completions"

    def test_is_zai_host_uses_hostname_not_path(self):
        assert _is_zai_host("https://api.z.ai/v1") is True
        assert _is_zai_host("https://z.ai/api") is True
        assert _is_zai_host("https://evil.example/z.ai") is False
        assert _is_zai_host("https://notz.ai/") is False

    def test_openwebui_normalization(self):
        # /api is stripped when is_openwebui is True (re-appended as get_api_version_suffix)
        assert normalize_endpoint_url("http://localhost:3000/api", is_openwebui=True) == "http://localhost:3000"
        # /api is NOT stripped when is_openwebui is False (OpenRouter-style bases keep /api)
        assert normalize_endpoint_url("http://localhost:3000/api", is_openwebui=False) == "http://localhost:3000/api"
        # Pasted /api/v1 must become host-only in one pass (avoid stored .../api → .../api/api/...)
        assert normalize_endpoint_url("http://localhost:3000/api/v1", is_openwebui=True) == "http://localhost:3000"
        assert normalize_endpoint_url("http://localhost:3000/api/v1/", is_openwebui=True) == "http://localhost:3000"
        assert normalize_endpoint_url("http://localhost:3000/v1", is_openwebui=True) == "http://localhost:3000"

    def test_openwebui_normalize_idempotent(self):
        for raw in (
            "http://localhost:3000",
            "http://localhost:3000/api",
            "http://localhost:3000/api/v1",
            "http://localhost:3000/v1",
            "http://localhost:3000/api/",
        ):
            once = normalize_endpoint_url(raw, is_openwebui=True)
            twice = normalize_endpoint_url(once, is_openwebui=True)
            assert twice == once
            # Round-trip to chat URL uses /api once
            assert once + get_api_version_suffix(once, is_openwebui=True) + "/chat/completions" == "http://localhost:3000/api/chat/completions"

class TestApiVersionSuffix():

    def test_google_suffix(self):
        assert get_api_version_suffix("https://generativelanguage.googleapis.com") == "/v1beta/openai"

    def test_zai_suffix(self):
        assert get_api_version_suffix("https://api.z.ai") == "/api/paas/v4"
        assert get_api_version_suffix("https://z.ai") == "/api/paas/v4"
        assert get_api_version_suffix("https://api.z.ai/api/paas") == "/v4"
        assert get_api_version_suffix("https://api.z.ai/api/coding/paas") == "/v4"
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


class TestLibrePyDispatchUrl(unittest.TestCase):
    class _Url:
        def __init__(self, complete="", path="", protocol=""):
            self.Complete = complete
            self.Path = path
            self.Protocol = protocol

    def test_command_from_path(self):
        url = self._Url(path="main.settings")
        assert dispatch_command_from_url(url) == "main.settings"

    def test_command_from_complete_when_path_empty(self):
        url = self._Url(complete="org.extension.librepy:scripting.run_python_dialog", path="")
        assert dispatch_command_from_url(url) == "scripting.run_python_dialog"

    def test_matches_protocol_and_complete(self):
        assert matches_librepy_dispatch_url(self._Url(protocol="org.extension.librepy:", path="main.settings"))
        assert matches_librepy_dispatch_url(self._Url(complete="org.extension.librepy:main.settings"))
        assert not matches_librepy_dispatch_url(self._Url(protocol="org.extension.writeragent:", path="main.settings"))


class TestPathToFileUrl:
    def test_posix_absolute_uses_three_slashes(self):
        url = path_to_file_url("/home/user/Writing/Test.odt")
        assert url.startswith("file:///")
        assert url.endswith("/home/user/Writing/Test.odt") or "Writing" in url
