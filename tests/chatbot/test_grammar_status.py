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
    assert format_grammar_status({"phase": "join", "preview": "sample text", "length": 11}) == "Grammar: waiting 'sample tex…' len 11"
    assert format_grammar_status({"phase": "complete", "preview": "sample", "length": 6, "result": "clean", "elapsed_ms": 42}) == "Grammar: done 'sample' len 6: clean, 42ms"
    assert format_grammar_status({"phase": "timeout", "preview": "sample", "length": 6, "result": "timed out"}) == "Grammar: still running 'sample' len 6: timed out"
    assert format_grammar_status({"phase": "skipped", "preview": "sample", "length": 6, "result": "cache hit"}) == "Grammar: skipped 'sample' len 6: cache hit"
    assert format_grammar_status({"phase": "failed", "preview": "sample", "length": 6, "result": "API error"}) == "Grammar: failed 'sample' len 6: API error"
    clipped = format_grammar_status({"phase": "failed", "preview": "Language d\u2026", "length": 19, "result": "TimeoutError"})
    assert clipped == "Language: failed 'Language d…' len 19: TimeoutError"
