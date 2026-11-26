# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""HTML entities on apply_content's format-preserving (plain text) path."""
from unittest.mock import MagicMock, patch

from plugin.writer.apply_document_content import ApplyDocumentContent
from plugin.writer.html_import_styles import (
    decode_complete_character_references,
    normalize_character_references,
)


class TestPlainTextEntityGuard:
    """The decoder that handles HTML character references on the plain-text path.

    Regression for the petition that received the six literal characters
    ``&#36;`` instead of ``$``: the import path unescapes, this one did not.
    """

    def test_matches_the_escaped_dollar_that_broke_petitions(self):
        assert decode_complete_character_references("Valor de R&#36;5.000,00") == "Valor de R$5.000,00"
        assert decode_complete_character_references("R&#x24;52,15") == "R$52,15"
        assert decode_complete_character_references("Banco &amp; Cia") == "Banco & Cia"

    def test_leaves_a_bare_ampersand_alone(self):
        for text in ("Banco & Cia", "A & B", "salário & benefícios", "P&D"):
            assert decode_complete_character_references(text) == text


class TestOnlyCompleteReferencesAreDecoded:
    """html.unescape on a whole string also expands semicolon-less names
    ("&sect" -> "§", "&not" -> "¬"). The plain-text path decodes only what
    decode_complete_character_references matched, so legal text that merely looks like an entity name
    survives."""

    def _decode(self, text):
        return decode_complete_character_references(text)

    def test_semicolon_less_names_are_left_alone(self):
        for text, want in (
            ("Valor R&#36;5,00 conforme &sect 2o", "Valor R$5,00 conforme &sect 2o"),
            ("R&#36;10,00 &para cada parcela", "R$10,00 &para cada parcela"),
            ("R&#36;1.000,00 &not incluidos", "R$1.000,00 &not incluidos"),
        ):
            assert self._decode(text) == want

    def test_complete_references_still_decode(self):
        assert self._decode("A &amp; B &#x24;5 &sect;2") == "A & B $5 §2"


def test_plain_path_params_decode_entities_and_expand_escapes():
    with patch("plugin.writer.apply_document_content.review_recording_enabled", return_value=False):
        tool = ApplyDocumentContent()
        params = tool._parse_edit_params(
            MagicMock(),
            {"content": "Valor R&#36;5,00 &sect 2o, Banco & Cia\\tok", "target": "end"},
        )
    assert params.use_preserve is True
    assert params.content == "Valor R$5,00 &sect 2o, Banco & Cia\tok"


def test_normalize_character_references_keeps_html_specials_escaped():
    raw = "&lt;b&gt;R&#36;5 &amp; &quot;x&quot; &#x27;y&#x27; &sect 2o&lt;/b&gt;"
    norm = normalize_character_references(raw)
    assert norm == "&lt;b&gt;R$5 &amp; &quot;x&quot; &#x27;y&#x27; &sect 2o&lt;/b&gt;"
    assert normalize_character_references(norm) == norm


def test_parse_edit_params_escaped_table_by_target():
    with patch("plugin.writer.apply_document_content.review_recording_enabled", return_value=False):
        tool = ApplyDocumentContent()
        end_params = tool._parse_edit_params(MagicMock(), {"content": "&lt;table&gt;", "target": "end"})
        after_params = tool._parse_edit_params(
            MagicMock(),
            {"content": "&lt;table&gt;", "target": "search", "old_content": "x", "position": "after"},
        )
        replace_params = tool._parse_edit_params(
            MagicMock(),
            {"content": "&lt;table&gt;", "target": "search", "old_content": "x", "position": "replace"},
        )
    assert end_params.content == "&lt;table&gt;"
    assert end_params.has_table is False
    assert after_params.content == "&lt;table&gt;"
    assert after_params.has_table is False
    assert replace_params.content == "<table>"
    assert replace_params.has_table is False


def test_insert_next_to_match_allows_entity_encoded_math():
    from plugin.writer.html_import_styles import WriteReport

    with patch("plugin.writer.apply_document_content.review_recording_enabled", return_value=False):
        tool = ApplyDocumentContent()
        params = tool._parse_edit_params(
            MagicMock(),
            {"content": "<p>&#36;x+1&#36;</p>", "target": "search", "old_content": "x", "position": "after"},
        )
    assert params.content == "<p>&#36;x+1&#36;</p>"

    cursor = MagicMock()
    cursor.getPropertyValue.return_value = None  # not in a table cell
    text_obj = MagicMock()
    text_obj.createTextCursorByRange.return_value = cursor
    found = MagicMock()
    found.getText.return_value = text_obj
    ctx = MagicMock()
    with (
        patch("plugin.writer.apply_document_content._paragraph_boundary_cursor", return_value=(cursor, True)),
        patch(
            "plugin.writer.apply_document_content.record_html_atomically",
            side_effect=lambda session, doc, mutate, *rest, **kw: mutate(),
        ),
        patch(
            "plugin.writer.apply_document_content.html_import.insert_html_after_paragraph",
            return_value=WriteReport(),
        ) as mock_insert,
    ):
        res = tool._edit_insert_next_to_match(ctx, MagicMock(), params, found)
    assert res["status"] == "ok"
    assert res["inserted"] is True
    assert mock_insert.call_args.args[4] == "<p>$x+1$</p>"
