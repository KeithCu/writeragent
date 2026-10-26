# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later

import json
import pytest

from scripts.bench_broker import _endpoint_parts, parse_stream


def _lines(*chunks):
    return [b"data: " + json.dumps(c).encode() + b"\n\n" for c in chunks]


def test_endpoint_parts_default():
    assert _endpoint_parts("http://localhost:11434") == (False, "localhost", 11434, "/v1/chat/completions")


def test_endpoint_parts_bare_host_port_and_trailing_slash():
    assert _endpoint_parts("127.0.0.1:11434/") == (False, "127.0.0.1", 11434, "/v1/chat/completions")


def test_endpoint_parts_https_v1_base():
    assert _endpoint_parts("https://api.example.com/v1/") == (True, "api.example.com", 443, "/v1/chat/completions")


def test_endpoint_parts_custom_base_path():
    assert _endpoint_parts("http://example.com:8080/openai/v1") == (False, "example.com", 8080, "/openai/v1/chat/completions")


def test_parse_stream_ttft_text_stops_at_done():
    lines = _lines(
        {"choices": [{"delta": {"content": "Hello "}}]},
        {"choices": [{"delta": {"content": "world"}}]},
    ) + [b"data: [DONE]\n\n"] + _lines(
        {"choices": [{"delta": {"content": " extra"}}]},
    )
    ttft, text, tc_count = parse_stream(lines, started_at=1.0, clock=lambda: 1.25)
    assert ttft == 250.0
    assert text == "Hello world"
    assert tc_count == 0


def test_parse_stream_counts_unique_tool_indexes():
    lines = _lines(
        {"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"name": "f1"}}]}}]},
        {"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"arguments": "{}"}}]}}]},
        {"choices": [{"delta": {"tool_calls": [{"index": 1, "function": {"name": "f2"}}]}}]},
    )
    ttft, text, tc_count = parse_stream(lines, started_at=0.0, clock=lambda: 0.1)
    assert ttft == 100.0
    assert text == ""
    assert tc_count == 2


def test_parse_stream_raises_on_error_chunk():
    lines = _lines({"error": {"message": "prompt is too long"}})
    with pytest.raises(RuntimeError, match="prompt is too long"):
        parse_stream(lines, 0.0)


def test_parse_stream_skips_malformed_json():
    lines = [b"data: {not json}\n\n"] + _lines({"choices": [{"delta": {"content": "ok"}}]})
    ttft, text, tc_count = parse_stream(lines, 0.0)
    assert text == "ok"


def test_parse_stream_empty_has_no_ttft():
    assert parse_stream([b"data: [DONE]\n\n"], 0.0) == (None, "", 0)
