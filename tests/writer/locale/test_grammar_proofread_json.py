# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Unit tests for the AI grammar proofread and language detect JSON parsing routines."""

from __future__ import annotations

import builtins
import importlib
import sys

import pytest

from plugin.writer.locale import grammar_proofread_json as gj


def test_module_imports_without_json_repair(monkeypatch) -> None:
    """Module must load even when vendored json_repair is not importable yet."""
    real_import = builtins.__import__

    def fake_import(name, globals=None, locals=None, fromlist=(), level=0):
        if name == "json_repair":
            raise ImportError("no json_repair")
        return real_import(name, globals, locals, fromlist, level)

    sys.modules.pop("json_repair", None)
    monkeypatch.setattr(builtins, "__import__", fake_import)
    importlib.reload(gj)

    # The module must not need json_repair at import time.
    with pytest.raises(ValueError):
        gj.parse_grammar_json("not json")


def test_parse_grammar_json_repairs_trailing_comma() -> None:
    raw = '{"errors": [{"wrong": "they is", "correct": "they are", "type": "grammar", "reason": "agreement"},]}'
    items = gj.parse_grammar_json(raw)
    assert len(items) == 1
    assert items[0]["wrong"] == "they is"
    assert items[0]["correct"] == "they are"


def test_parse_grammar_json_empty() -> None:
    assert gj.parse_grammar_json("") == []
    with pytest.raises(ValueError):
        gj.parse_grammar_json("not json")


def test_parse_grammar_json_unparseable_raises() -> None:
    for invalid in ("I cannot check this.", '{"other": 1}', '{"errors": "not a list"}'):
        with pytest.raises(ValueError):
            gj.parse_grammar_json(invalid)


def test_parse_grammar_json_valid() -> None:
    raw = '{"errors": [{"wrong": "they is", "correct": "they are", "type": "grammar", "reason": "agreement"}]}'
    items = gj.parse_grammar_json(raw)
    assert len(items) == 1
    assert items[0]["wrong"] == "they is"
    assert items[0]["correct"] == "they are"
    assert items[0]["type"] == "grammar"
    assert items[0]["reason"] == "agreement"


def test_parse_grammar_json_wire_field_validation() -> None:
    assert gj.parse_grammar_json('{"errors": [{"wrong": 5, "correct": "five"}]}') == []
    assert gj.parse_grammar_json('{"errors": [{"wrong": "a", "correct": ["b"]}]}') == []

    raw = (
        '{"errors": ['
        '{"wrong": "w1", "correct": "c1", "reason": null, "type": "STYLE"}, '
        '{"wrong": "w2", "correct": "c2", "type": "nonsense"}, '
        '{"wrong": "w3", "correct": "c3", "type": 3}, '
        '{"wrong": "5", "correct": "five"}'
        ']}'
    )
    items = gj.parse_grammar_json(raw)
    assert len(items) == 4
    assert items[0] == {"wrong": "w1", "correct": "c1", "type": "style", "reason": ""}
    assert items[1] == {"wrong": "w2", "correct": "c2", "type": "grammar", "reason": ""}
    assert items[2] == {"wrong": "w3", "correct": "c3", "type": "grammar", "reason": ""}
    assert items[3] == {"wrong": "5", "correct": "five", "type": "grammar", "reason": ""}


def test_parse_grammar_batch_json_empty() -> None:
    assert gj.parse_grammar_batch_json("") == []
    with pytest.raises(ValueError):
        gj.parse_grammar_batch_json("not json")


def test_parse_grammar_batch_json_unparseable_raises() -> None:
    for invalid in ("I cannot check this.", '{"other": 1}', '{"errors": "not a list"}'):
        with pytest.raises(ValueError):
            gj.parse_grammar_batch_json(invalid)


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


def test_parse_language_detect_json_whitespace_is_none() -> None:
    assert gj.parse_language_detect_json('{"detected_language_bcp47": "   "}') is None


def test_parse_language_detect_batch_json() -> None:
    raw = '{"detected_language_bcp47": "es-ES", "results": [{"detected_language_bcp47": "es-ES"}]}'
    langs = gj.parse_language_detect_batch_json(raw)
    assert len(langs) == 1
    assert langs[0] == "es-ES"


def test_coerce_json_object() -> None:
    assert gj._coerce_json_object("") is None
    assert gj._coerce_json_object("   ") is None
    assert gj._coerce_json_object("not json at all") is None
    assert gj._coerce_json_object('```json\n{"key": "value"}\n```') == {"key": "value"}
    # Repairable JSON with trailing comma
    assert gj._coerce_json_object('{"key": "value",}') == {"key": "value"}


def test_coerce_json_object_extracts_span() -> None:
    assert gj._coerce_json_object('Here you go: {"errors": []}') == {"errors": []}
    assert gj._coerce_json_object('{"errors": []} trailing') == {"errors": []}


def test_grammar_batch_parser_robustness() -> None:
    raw = (
        '{"results": ['
        '"not a mapping", '
        '{"errors": "not a list"}, '
        '{"errors": ['
        '"not a mapping", '
        '{"wrong": "foo"}, '
        '{"wrong": "they is", "correct": "they are", "type": "grammar", "reason": "agreement"}'
        ']}'
        ']}'
    )
    items = gj.parse_grammar_batch_json(raw)
    assert items == [[], [], [{"wrong": "they is", "correct": "they are", "type": "grammar", "reason": "agreement"}]]


def test_language_detect_batch_parser_robustness() -> None:
    raw = '{"results": ["not a mapping", {"detected_language_bcp47": "fr-FR"}, {"detected_language_bcp47": 1}]}'
    assert gj.parse_language_detect_batch_json(raw) == [None, "fr-FR", None]


def test_language_detect_single_non_string() -> None:
    assert gj.parse_language_detect_json('{"detected_language_bcp47": 1}') is None
    assert gj.parse_language_detect_json('{"detected_language_bcp47": ["fr-FR"]}') is None


def test_batch_parsers_missing_results() -> None:
    with pytest.raises(ValueError):
        gj.parse_grammar_batch_json('{"other": 1}')
    assert gj.parse_language_detect_batch_json('{"other": 1}') == []
