# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
"""Tests for plugin.mcp.http_trace."""

from __future__ import annotations

import email.message
import logging
from typing import Any

import pytest

import plugin.mcp.http_trace as http_trace


class _FakeHandler:
    def __init__(
        self,
        headers: Any = None,
        client_address: tuple[str, int] = ("127.0.0.1", 53112),
        command: str = "POST",
        path: str = "/mcp",
    ) -> None:
        self.headers = headers
        self.client_address = client_address
        self.command = command
        self.path = path


def test_header_case_insensitivity_and_none(caplog: pytest.LogCaptureFixture) -> None:
    msg = email.message.Message()
    msg["origin"] = "  https://app.example  "
    handler_msg = _FakeHandler(headers=msg)
    with caplog.at_level(logging.INFO, logger="writeragent.mcp.http"):
        http_trace.log_http_request(handler_msg, "POST", "/mcp")
    assert "origin='https://app.example'" in caplog.text

    caplog.clear()
    handler_none = _FakeHandler(headers=None)
    with caplog.at_level(logging.INFO, logger="writeragent.mcp.http"):
        http_trace.log_http_request(handler_none, "POST", "/mcp")
    assert "origin=None" in caplog.text


def test_log_forbidden_origin_strips_query_and_escapes(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING, logger="writeragent.mcp.http"):
        # path with secret in query string
        handler_query = _FakeHandler(headers={"Origin": "https://evil.com"}, path="/x?token=secret")
        http_trace.log_forbidden_origin(handler_query)
        assert "'/x'" in caplog.text
        assert "secret" not in caplog.text

        # path with terminal escape sequences
        caplog.clear()
        handler_escape = _FakeHandler(headers={"Origin": "https://evil.com"}, path="/x\x1b[31m")
        http_trace.log_forbidden_origin(handler_escape)
        assert "\\x1b" in caplog.text


def test_log_mcp_transport_entry_casing_and_defaults(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.INFO, logger="writeragent.mcp.http"):
        msg = email.message.Message()
        msg["mcp-protocol-version"] = "2025-06-18"
        handler = _FakeHandler(headers=msg)
        http_trace.log_mcp_transport_entry(handler, "mcp")
        assert "protocol_version='2025-06-18'" in caplog.text
        assert "content_length=0" in caplog.text


def test_cors_preflight_skips_policy_work_when_info_disabled(monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    called = False

    def spy(*args: Any, **kwargs: Any) -> bool:
        nonlocal called
        called = True
        return True

    monkeypatch.setattr(http_trace, "is_safe_origin", spy)
    handler = _FakeHandler(headers={"Origin": "https://app.example"})

    with caplog.at_level(logging.WARNING, logger="writeragent.mcp.http"):
        http_trace.log_cors_preflight(handler, "/mcp")

    assert not called
    assert len(caplog.records) == 0
