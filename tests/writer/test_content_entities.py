# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""HTML entities on apply_document_content's format-preserving (plain text) path."""
from unittest.mock import MagicMock, patch

from plugin.writer.apply_document_content import ApplyDocumentContent
from plugin.writer.html_import_styles import decode_complete_character_references


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

    def test_decoded_text_is_what_reaches_the_document(self):
        content = "Valor de R&#36;5.000,00 e R&#36;52,15."
        assert decode_complete_character_references(content) == "Valor de R$5.000,00 e R$52,15."


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
            assert (self._decode(text)) == (want)

    def test_complete_references_still_decode(self):
        assert (self._decode("A &amp; B &#x24;5 &sect;2")) == ("A & B $5 §2")


def test_plain_path_params_decode_entities_and_expand_escapes():
    with patch("plugin.writer.apply_document_content.review_recording_enabled", return_value=False):
        tool = ApplyDocumentContent()
        params = tool._parse_edit_params(
            MagicMock(),
            {"content": "Valor R&#36;5,00 &sect 2o, Banco & Cia\\tok", "target": "end"},
        )
    assert params.use_preserve is True
    assert params.content == "Valor R$5,00 &sect 2o, Banco & Cia\tok"
