# WriterAgent - Native Grammar Status Tests
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later

from plugin.chatbot.grammar_status import (
    _clip_grammar_status_preview,
    _grammar_status_area,
    format_grammar_status,
)


def test_clip_grammar_status_preview_empty():
    assert _clip_grammar_status_preview("") == "(empty)"
    assert _clip_grammar_status_preview("   ") == "(empty)"


def test_clip_grammar_status_preview_short_and_long():
    assert _clip_grammar_status_preview("hello", max_len=10) == "hello"
    assert _clip_grammar_status_preview("hello world this is long", max_len=10) == "hello worl…"


def test_grammar_status_area():
    assert _grammar_status_area("request", "Detecting language", "") == "language"
    assert _grammar_status_area("failed", "error", "Language detection") == "language"
    # Bus clip: slice_preview_debug("Language detection", 10) == "Language d…"
    assert _grammar_status_area("failed", "error", "Language d\u2026") == "language"
    assert _grammar_status_area("request", "checking", "") == "grammar"


def test_format_grammar_status_lifecycle():
    assert format_grammar_status({"phase": "start", "preview": "sample text", "length": 11}) == "Grammar: queued 'sample tex…' len 11"
    assert format_grammar_status({"phase": "failed", "preview": "sample", "length": 6, "result": "API error"}) == "Grammar: failed 'sample' len 6: API error"
    clipped = format_grammar_status({"phase": "failed", "preview": "Language d\u2026", "length": 19, "result": "TimeoutError"})
    assert clipped == "Language: failed 'Language d…' len 19: TimeoutError"


def test_format_grammar_status_multiline_result_collapsed() -> None:
    assert (
        format_grammar_status(
            {"phase": "failed", "preview": "x", "length": 1, "result": "line1\n\nline2\tend"}
        )
        == "Grammar: failed 'x' len 1: line1 line2 end"
    )


def test_format_grammar_status_long_result_clipped() -> None:
    long_res = "a" * 200
    res = format_grammar_status(
        {"phase": "failed", "preview": "x", "length": 1, "result": long_res}
    )
    result_part = res.rsplit(": ", 1)[1]
    assert res.endswith("…")
    assert len(result_part) == 121
    assert result_part == ("a" * 120) + "…"


def test_format_grammar_status_done_matches_complete_line() -> None:
    """Worker batch completion uses phase ``done``; sidebar should show result suffix like ``complete``."""
    text = format_grammar_status(
        {
            "phase": "done",
            "preview": "The quick brown fox jumps.",
            "length": 120,
            "result": "2 issues, 2 sentences",
            "elapsed_ms": 400,
        }
    )
    assert text == "Grammar: done 'The quick …' len 120: 2 issues, 2 sentences, 400ms"


def test_format_grammar_status_request_language_detect() -> None:
    text = format_grammar_status(
        {
            "phase": "request",
            "preview": "Hello world",
            "length": 11,
            "result": "Detecting language",
        }
    )
    assert text == "Language: detecting 'Hello worl…' len 11"


def test_format_grammar_status_request_grammar_checking() -> None:
    text = format_grammar_status(
        {
            "phase": "request",
            "preview": "Hello world",
            "length": 11,
            "result": "LLM request",
        }
    )
    assert text == "Grammar: checking 'Hello worl…' len 11"


def test_format_grammar_status_request_harper_detail() -> None:
    text = format_grammar_status(
        {
            "phase": "request",
            "preview": "They is here.",
            "length": 13,
            "result": "Downloading harper-ls v2.7.0…",
        }
    )
    assert text == "Grammar: Downloading harper-ls v2.7.0…"


def test_format_grammar_status_failed_language_vs_grammar() -> None:
    lang = format_grammar_status(
        {
            "phase": "failed",
            "preview": "Language detection",
            "length": 19,
            "result": "TimeoutError",
        }
    )
    assert lang == "Language: failed 'Language d…' len 19: TimeoutError"

    grm = format_grammar_status(
        {
            "phase": "failed",
            "preview": "Grammar check",
            "length": 13,
            "result": "ValueError",
        }
    )
    assert grm == "Grammar: failed 'Grammar ch…' len 13: ValueError"
