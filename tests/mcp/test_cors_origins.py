# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu

import pytest

from plugin.mcp.cors import is_safe_origin
from plugin.mcp.cors_origins import (
    first_origin_for_ui,
    merge_ui_origin_into_list,
    normalize_cors_origin,
    normalize_origins_list,
    set_extra_allowed_origins,
)


def test_normalize_cors_origin_strips_slash():
    assert normalize_cors_origin("https://localai.local/") == "https://localai.local"


def test_normalize_cors_origin_rejects_invalid():
    assert normalize_cors_origin("localai.local") is None
    assert normalize_cors_origin("") is None


def test_normalize_origins_list_dedupes():
    assert normalize_origins_list(["https://a.com", "https://a.com/"]) == ["https://a.com"]


def test_merge_ui_origin_replaces_first_keeps_tail():
    assert merge_ui_origin_into_list(["https://old.com", "https://b.com"], "https://new.com") == [
        "https://new.com",
        "https://b.com",
    ]


def test_merge_ui_origin_clear_first_keeps_tail():
    assert merge_ui_origin_into_list(["https://old.com", "https://b.com"], "") == ["https://b.com"]


def test_merge_ui_origin_empty_list_adds_first():
    assert merge_ui_origin_into_list([], "https://localai.local") == ["https://localai.local"]


def test_first_origin_for_ui():
    assert first_origin_for_ui(["https://a.com", "https://b.com"]) == "https://a.com"
    assert first_origin_for_ui([]) == ""


def test_is_safe_origin_extra_allowed():
    set_extra_allowed_origins(["https://localai.local"])
    assert is_safe_origin("https://localai.local")
    assert not is_safe_origin("https://example.com")
    set_extra_allowed_origins([])
