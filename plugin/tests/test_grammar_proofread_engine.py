# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Unit tests for grammar proofread engine (JSON, offsets, cache)."""

from __future__ import annotations

from plugin.modules.writer import grammar_proofread_engine as eng


def test_parse_grammar_json_empty() -> None:
    assert eng.parse_grammar_json("") == []
    assert eng.parse_grammar_json("not json") == []


def test_parse_grammar_json_valid() -> None:
    raw = '{"errors": [{"wrong": "they is", "correct": "they are", "type": "grammar", "reason": "agreement"}]}'
    items = eng.parse_grammar_json(raw)
    assert len(items) == 1
    assert items[0]["wrong"] == "they is"
    assert items[0]["correct"] == "they are"


def test_normalize_errors_for_text() -> None:
    full = "Hello they is here."
    items = [{"wrong": "they is", "correct": "they are", "type": "grammar", "reason": "agr"}]
    norms = eng.normalize_errors_for_text(full, 0, len(full), items)
    assert len(norms) == 1
    assert full[norms[0].n_error_start : norms[0].n_error_start + norms[0].n_error_length] == "they is"


def test_normalize_errors_respects_slice() -> None:
    full = "xx they is yy"
    items = [{"wrong": "they is", "correct": "they are", "type": "grammar", "reason": ""}]
    norms = eng.normalize_errors_for_text(full, 3, 12, items)
    assert len(norms) == 1
    assert norms[0].n_error_start >= 3


def test_sentence_cache_roundtrip() -> None:
    eng.cache_clear()
    assert eng.cache_get_sentence("en-US", "Hello world.") is None
    errors = [{"n_error_start": 0, "n_error_length": 5, "rule_identifier": "wa_test"}]
    eng.cache_put_sentence("en-US", "Hello world.", errors)
    got = eng.cache_get_sentence("en-US", "Hello world.")
    assert got is not None
    assert len(got) == 1
    assert got[0]["n_error_start"] == 0
    assert eng.cache_get_sentence("fr-FR", "Hello world.") is None
    eng.cache_clear()


def test_sentence_cache_trailing_whitespace() -> None:
    """'Hello.' and 'Hello. ' share the same cache key."""
    eng.cache_clear()
    eng.cache_put_sentence("en-US", "Hello.", [{"n_error_start": 0, "n_error_length": 5}])
    got = eng.cache_get_sentence("en-US", "Hello. ")
    assert got is not None
    assert len(got) == 1
    eng.cache_clear()


def test_ignore_rules_snapshot() -> None:
    eng.ignore_rules_clear()
    eng.ignore_rule_add("rule_a")
    assert "rule_a" in eng.ignored_rules_snapshot()
    eng.ignore_rules_clear()
    assert eng.ignored_rules_snapshot() == set()


def test_overlap_forward_expansion() -> None:
    full = "I went to the store."
    # LLM flagged "to" but correct was "to the" (which overlaps with " the store.")
    items = [{"wrong": "to", "correct": "to the", "type": "grammar"}]
    norms = eng.normalize_errors_for_text(full, 0, len(full), items)
    assert len(norms) == 0, "Should be dropped as a no-op because expanding 'to' -> 'to the' matches 'correct'"

    items2 = [{"wrong": "to", "correct": "into the", "type": "grammar"}]
    norms2 = eng.normalize_errors_for_text(full, 0, len(full), items2)
    assert len(norms2) == 1
    err = norms2[0]
    expanded_wrong = full[err.n_error_start : err.n_error_start + err.n_error_length]
    assert expanded_wrong == "to the", f"Expected 'to the' but got {expanded_wrong}"


def test_overlap_backward_expansion() -> None:
    full = "He is a good man."
    # LLM flagged "good" but correct was "a good"
    items = [{"wrong": "good", "correct": "a good", "type": "grammar"}]
    norms = eng.normalize_errors_for_text(full, 0, len(full), items)
    assert len(norms) == 0, "Should be dropped as a no-op because 'a good' was already there"

    items2 = [{"wrong": "good", "correct": "a very good", "type": "grammar"}]
    norms2 = eng.normalize_errors_for_text(full, 0, len(full), items2)
    assert len(norms2) == 1
    err = norms2[0]
    expanded_wrong = full[err.n_error_start : err.n_error_start + err.n_error_length]
    assert expanded_wrong == "a good"
    assert err.suggestions == ("a very good",)
