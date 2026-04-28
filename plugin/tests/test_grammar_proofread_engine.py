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


def test_cache_key_includes_slice_bounds() -> None:
    """Identical slice text at different (start, end) must not share one cache entry."""
    from dataclasses import asdict

    eng.cache_clear()
    doc_id = "doc_test_123"
    loc = "en_US"
    full = "aa Sentence one. bb Sentence one. cc"
    # Same substring "Sentence one." at two windows
    w0, w1 = 3, 16
    w2, w3 = 20, 33
    slice_a = full[w0:w1]
    slice_b = full[w2:w3]
    assert slice_a == slice_b
    fp = eng.fingerprint_for_text(slice_a)
    key_a = eng.make_cache_key(doc_id, loc, fingerprint=fp, slice_start=w0, slice_end=w1)
    key_b = eng.make_cache_key(doc_id, loc, fingerprint=fp, slice_start=w2, slice_end=w3)
    assert key_a != key_b

    errors = [{"wrong": "Sentence", "correct": "Phrasence", "type": "style", "reason": "test"}]
    norms_a = eng.normalize_errors_for_text(full, w0, w1, errors)
    eng.cache_put(key_a, fp, [asdict(n) for n in norms_a])
    assert eng.cache_get(key_a, fp) is not None
    assert eng.cache_get(key_b, fp) is None
    eng.cache_clear()


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
