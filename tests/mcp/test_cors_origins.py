# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu

import pytest

from plugin.mcp.cors import is_safe_origin
from plugin.mcp.cors_origins import (
    is_private_browser_origin,
    normalize_cors_origin,
    normalize_origins_list,
    set_allow_private_origins,
    set_extra_allowed_origins,
)


def setup_function():
    set_extra_allowed_origins([])
    set_allow_private_origins(True)


def teardown_function():
    set_extra_allowed_origins([])
    set_allow_private_origins(True)


def test_normalize_cors_origin_strips_slash():
    assert normalize_cors_origin("https://localai.local/") == "https://localai.local"


def test_normalize_cors_origin_rejects_invalid():
    assert normalize_cors_origin("localai.local") is None
    assert normalize_cors_origin("") is None


def test_normalize_origins_list_dedupes():
    assert normalize_origins_list(["https://a.com", "https://a.com/"]) == ["https://a.com"]


def test_is_private_browser_origin_suffixes():
    assert is_private_browser_origin("https://localai.local")
    assert is_private_browser_origin("http://nas.lan:8080")
    assert is_private_browser_origin("https://app.home.arpa")
    assert is_private_browser_origin("https://tool.internal")


def test_is_private_browser_origin_private_ip():
    assert is_private_browser_origin("http://192.168.1.50:3000")
    assert is_private_browser_origin("http://10.0.0.5:8080")


def test_is_private_browser_origin_rejects_public():
    assert not is_private_browser_origin("https://evil.com")
    assert not is_private_browser_origin("https://evil.localhost")
    assert not is_private_browser_origin("https://localai.local.com")
    assert not is_private_browser_origin("http://localhost.attacker.com")


def test_is_safe_origin_private_when_enabled():
    set_allow_private_origins(True)
    assert is_safe_origin("https://localai.local")
    assert is_safe_origin("http://192.168.0.2:8123")


def test_is_safe_origin_private_when_disabled():
    set_allow_private_origins(False)
    assert not is_safe_origin("https://localai.local")
    assert is_safe_origin("http://localhost:3000")


def test_is_safe_origin_explicit_list_when_private_disabled():
    set_allow_private_origins(False)
    set_extra_allowed_origins(["https://app.company.com"])
    assert is_safe_origin("https://app.company.com")
    assert not is_safe_origin("https://localai.local")
