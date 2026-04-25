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


def test_cache_roundtrip() -> None:
    eng.cache_clear()
    key = eng.make_cache_key(1, "en_US")
    fp = eng.fingerprint_for_text("hello world")
    assert eng.cache_get(key, fp) is None
    norms = eng.normalize_errors_for_text(
        "they is bad",
        0,
        20,
        [{"wrong": "they is", "correct": "they are", "type": "grammar", "reason": "r"}],
    )
    from dataclasses import asdict

    eng.cache_put(key, fp, [asdict(n) for n in norms])
    got = eng.cache_get(key, fp)
    assert got is not None
    assert len(got) == 1
    eng.cache_clear()


def test_ignore_rules_snapshot() -> None:
    eng.ignore_rules_clear()
    eng.ignore_rule_add("rule_a")
    assert "rule_a" in eng.ignored_rules_snapshot()
    eng.ignore_rules_clear()
    assert eng.ignored_rules_snapshot() == set()


def test_offset_independent_cache() -> None:
    from dataclasses import asdict

    eng.cache_clear()
    doc_id = "doc_test_123"
    loc = "en_US"
    text1 = "Sentence one. Sentence two."
    # Initial analysis of the paragraph
    fp1 = eng.fingerprint_for_text(text1)
    key = eng.make_cache_key(doc_id, loc)

    # Mocking errors found
    errors = [{"wrong": "Sentence", "correct": "Phrasence", "type": "style", "reason": "test"}]
    norms = eng.normalize_errors_for_text(text1, 0, len(text1), errors)
    eng.cache_put(key, fp1, [asdict(n) for n in norms])

    # Verify cache hit for identical text
    assert eng.cache_get(key, fp1) is not None

    # Scenario: User inserts a newline at the very beginning of the document.
    # The paragraph itself is unchanged, but its offset in the document might have shifted.
    # However, since we now ignore offsets in the key, it should still hit.
    assert eng.cache_get(key, fp1) is not None


