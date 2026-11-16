# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Unit tests for pure MCP client helpers (plugin/mcp/client_helpers.py)."""

from __future__ import annotations

import json
import logging

import pytest

from plugin.mcp.client import render_call_result
from plugin.mcp.client_helpers import (
    annotation_read_only_hint,
    format_approval_arguments,
    make_tool_filter,
    normalize_server_trust,
    parse_boolish,
    safe_numeric,
    sanitize_error,
    server_enabled,
    strip_bearer_prefix,
    strip_reserved_meta_keys,
    strip_tool_metadata_text,
    strip_unicode_tags,
    strip_unicode_tags_deep,
    truncate_head_tail,
    unresolved_env_refs,
    validate_remote_url,
    warn_hidden_whitespace,
)


def test_structured_content_and_meta_rendering():
    # Text only
    assert render_call_result({"content": [{"type": "text", "text": "hello"}]}) == {"result": "hello"}

    # None structured falls back to text
    assert render_call_result({"content": [{"type": "text", "text": "hello"}], "structuredContent": None}) == {
        "result": "hello"
    }

    # Empty text plus structured promotes structuredContent to result
    assert render_call_result({"content": [], "structuredContent": {"a": 1}}) == {"result": '{"a": 1}'}

    # Vendor _meta passes, reserved _meta dropped, all-reserved omits _meta
    res_vendor = render_call_result(
        {
            "content": [{"type": "text", "text": "ok"}],
            "_meta": {"com.example/trace": "123", "modelcontextprotocol.org/token": "drop", "mcp.dev/y": "drop"},
        }
    )
    assert res_vendor["_meta"] == {"com.example/trace": "123"}

    res_all_reserved = render_call_result(
        {
            "content": [{"type": "text", "text": "ok"}],
            "_meta": {"modelcontextprotocol.org/foo": 1, "mcp.dev/bar": 2},
        }
    )
    assert "_meta" not in res_all_reserved

    # Dual-emit suppresses structuredContent
    dual = render_call_result(
        {
            "content": [{"type": "text", "text": '{"b": 2, "a": 1}'}],
            "structuredContent": {"a": 1, "b": 2},
        }
    )
    assert "structuredContent" not in dual

    # Summary plus data keeps structuredContent
    summary = render_call_result(
        {
            "content": [{"type": "text", "text": "Found 2 items."}],
            "structuredContent": {"a": 1, "b": 2},
        }
    )
    assert summary["result"] == "Found 2 items."
    assert summary["structuredContent"] == {"a": 1, "b": 2}


def test_strip_reserved_meta_keys_prefixes():
    assert strip_reserved_meta_keys({"com.example.mcp/ok": 1, "plain": 2}) == {"com.example.mcp/ok": 1, "plain": 2}
    assert strip_reserved_meta_keys({"mcp.example/bad": 1, "a.modelcontextprotocol.b/bad": 2}) is None


def test_truncate_head_tail():
    assert truncate_head_tail("abc", 5) == "abc"
    assert truncate_head_tail("abcde", 5) == "abcde"
    out = truncate_head_tail("A" * 40 + "M" * 20 + "Z" * 60, 100, label="MCP RESULT")
    assert out.startswith("A" * 40)
    assert out.endswith("Z" * 60)
    assert "20 chars omitted out of 120 total" in out
    assert "MCP RESULT TRUNCATED" in out


def test_validate_remote_url():
    assert validate_remote_url("srv", "  https://example.com/mcp  ") == "https://example.com/mcp"
    assert validate_remote_url("srv", "http://localhost:8000/mcp") == "http://localhost:8000/mcp"
    for bad in (None, "", "ftp://example.com/mcp", "file:///etc/passwd", "http://:8080/mcp"):
        with pytest.raises(ValueError, match="srv"):
            validate_remote_url("srv", bad)


def test_safe_numeric():
    assert safe_numeric("120", 60.0, float) == 120.0
    assert safe_numeric("bad", 60.0, float) == 60.0
    assert safe_numeric(float("nan"), 60.0, float) == 60.0
    assert safe_numeric(float("inf"), 60.0, float) == 60.0
    assert safe_numeric(-5, 60.0, float) == 1.0


def test_boolish_enabled_and_trust():
    assert parse_boolish("false") is False
    assert parse_boolish("0") is False
    assert parse_boolish("YES") is True
    assert server_enabled({"enabled": "false"}) is False
    assert server_enabled({"enabled": 0}) is False
    assert server_enabled({}) is True

    assert normalize_server_trust(None) == "untrusted"
    assert normalize_server_trust("bogus") == "untrusted"
    assert normalize_server_trust("FULL") == "full"
    assert normalize_server_trust("untrusted") == "untrusted"


def test_annotation_read_only_hint():
    assert annotation_read_only_hint({"annotations": {"readOnlyHint": True}}) is True
    assert annotation_read_only_hint({"annotations": {"read_only_hint": True}}) is True
    assert annotation_read_only_hint({"annotations": {"readOnlyHint": "true"}}) is False
    assert annotation_read_only_hint({}) is False


def test_sanitize_error_redacts_credentials():
    for secret in ("ghp_abcdef123456", "sk-proj-abc123", "Bearer my-secret-jwt", "token=xyz123"):
        cleaned = sanitize_error(f"failed with {secret} here")
        assert secret not in cleaned
        assert "[REDACTED]" in cleaned


def test_strip_unicode_tags_and_bidi():
    scotland_flag = "\U0001F3F4\U000E0067\U000E0062\U000E0073\U000E0063\U000E0074\U000E007F"
    smuggled = "\U000E0069\U000E0067\U000E006E"
    assert strip_unicode_tags(f"hi {scotland_flag} {smuggled}!") == f"hi {scotland_flag} !"
    assert strip_tool_metadata_text("a\u202eb\u2066c\U000E0041") == "abc"
    assert strip_unicode_tags_deep({"k\U000E0041": ["v\U000E0042", 1]}) == {"k": ["v", 1]}


def test_name_filters():
    empty_inc = make_tool_filter("s", {"tools": {"include": []}})
    assert empty_inc("anything") is False

    glob_inc = make_tool_filter("s", {"tools": {"include": ["nextcloud_*"], "exclude": ["nextcloud_list"]}})
    assert glob_inc("nextcloud_list") is True
    assert glob_inc("weather") is False

    exc_only = make_tool_filter("s", {"tools": {"exclude": ["*_secret"]}})
    assert exc_only("read_secret") is False
    assert exc_only("read_public") is True


def test_warn_hidden_whitespace(caplog):
    with caplog.at_level(logging.WARNING):
        paths = warn_hidden_whitespace("home", {"url": "http://x\n", "headers": {"X-Key": " abc"}})
    assert paths == ["url", "headers.X-Key"]
    assert any("headers.X-Key" in r.message for r in caplog.records)


def test_strip_bearer_prefix():
    assert strip_bearer_prefix("  Bearer abc123  ") == "abc123"
    assert strip_bearer_prefix("bearer xyz") == "xyz"
    assert strip_bearer_prefix("abc123") == "abc123"


def test_unresolved_env_refs():
    assert unresolved_env_refs({"url": "https://${HOST}/mcp", "headers": {"Authorization": "Bearer ${env:TOKEN}"}}) == [
        "HOST",
        "env:TOKEN",
    ]


def test_format_approval_arguments():
    args = {"msg": "hi\u202e\x85there", "n": 1}
    card = format_approval_arguments(args)
    assert "\\u202e" in card
    assert "\\u0085" in card
    assert json.loads(card) == args

    big = format_approval_arguments({"big": "x" * 5000})
    assert len(big) <= 1200
    assert big.endswith("\n... [arguments truncated]")
