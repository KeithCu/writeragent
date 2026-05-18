# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Unit tests for the AI grammar proofread and language detect JSON parsing routines."""

from __future__ import annotations

from plugin.writer.locale import grammar_proofread_json as gj


def test_parse_grammar_json_empty() -> None:
    assert gj.parse_grammar_json("") == []
    assert gj.parse_grammar_json("not json") == []


def test_parse_grammar_json_valid() -> None:
    raw = '{"errors": [{"wrong": "they is", "correct": "they are", "type": "grammar", "reason": "agreement"}]}'
    items = gj.parse_grammar_json(raw)
    assert len(items) == 1
    assert items[0]["wrong"] == "they is"
    assert items[0]["correct"] == "they are"
    assert items[0]["type"] == "grammar"
    assert items[0]["reason"] == "agreement"


def test_parse_grammar_batch_json_empty() -> None:
    assert gj.parse_grammar_batch_json("") == []
    assert gj.parse_grammar_batch_json("not json") == []


def test_parse_grammar_batch_json_valid() -> None:
    raw = '{"results": [{"errors": [{"wrong": "they is", "correct": "they are", "type": "grammar", "reason": "agreement"}]}]}'
    items = gj.parse_grammar_batch_json(raw)
    assert len(items) == 1
    assert len(items[0]) == 1
    assert items[0][0]["wrong"] == "they is"
    assert items[0][0]["correct"] == "they are"


def test_parse_language_detect_json() -> None:
    raw = '{"detected_language_bcp47": "fr-FR", "errors": []}'
    lang = gj.parse_language_detect_json(raw)
    assert lang == "fr-FR"

    assert gj.parse_language_detect_json('{"other": 1}') is None


def test_parse_language_detect_batch_json() -> None:
    raw = '{"detected_language_bcp47": "es-ES", "results": [{"detected_language_bcp47": "es-ES"}]}'
    langs = gj.parse_language_detect_batch_json(raw)
    assert len(langs) == 1
    assert langs[0] == "es-ES"


def test_compress_and_decompress_error() -> None:
    original = {
        "n_error_start": 10,
        "n_error_length": 5,
        "suggestions": ["hello", "hi"],
        "short_comment": "comment",
        "full_comment": "long comment",
        "rule_identifier": "rule1",
        "extra_key": "stays_same",
    }
    compressed = gj.compress_error(original)
    assert compressed == {
        "s": 10,
        "l": 5,
        "g": ["hello", "hi"],
        "c": "comment",
        "f": "long comment",
        "r": "rule1",
        "extra_key": "stays_same",
    }
    decompressed = gj.decompress_error(compressed)
    assert decompressed == original


def test_fingerprint_for_text() -> None:
    text1 = "This is a sentence."
    text2 = "This is another sentence."
    fp1 = gj.fingerprint_for_text(text1)
    fp2 = gj.fingerprint_for_text(text2)
    assert len(fp1) == 24
    assert len(fp2) == 24
    assert fp1 != fp2
    # Deterministic check
    assert fp1 == gj.fingerprint_for_text(text1)

