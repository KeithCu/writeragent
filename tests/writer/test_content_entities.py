# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""HTML entities on apply_document_content's format-preserving (plain text) path."""

import unittest

from plugin.writer.content import _ENTITY_RE


class TestPlainTextEntityGuard(unittest.TestCase):
    """The guard that decides whether the plain-text path runs html.unescape.

    Regression for the petition that received the six literal characters
    ``&#36;`` instead of ``$``: the import path unescapes, this one did not.
    """

    def test_matches_the_escaped_dollar_that_broke_petitions(self):
        self.assertTrue(_ENTITY_RE.search("Valor de R&#36;5.000,00"))
        self.assertTrue(_ENTITY_RE.search("R&#x24;52,15"))
        self.assertTrue(_ENTITY_RE.search("Banco &amp; Cia"))

    def test_leaves_a_bare_ampersand_alone(self):
        for text in ("Banco & Cia", "A & B", "salário & benefícios", "P&D"):
            with self.subTest(text=text):
                self.assertIsNone(_ENTITY_RE.search(text))

    def test_decoded_text_is_what_reaches_the_document(self):
        import html as html_mod

        content = "Valor de R&#36;5.000,00 e R&#36;52,15."
        self.assertTrue(_ENTITY_RE.search(content))
        self.assertEqual(
            html_mod.unescape(content), "Valor de R$5.000,00 e R$52,15."
        )


class TestOnlyCompleteReferencesAreDecoded(unittest.TestCase):
    """html.unescape on a whole string also expands semicolon-less names
    ("&sect" -> "§", "&not" -> "¬"). The plain-text path decodes only what
    _ENTITY_RE matched, so legal text that merely looks like an entity name
    survives."""

    def _decode(self, text):
        import html as html_mod

        return _ENTITY_RE.sub(lambda m: html_mod.unescape(m.group(0)), text)

    def test_semicolon_less_names_are_left_alone(self):
        for text, want in (
            ("Valor R&#36;5,00 conforme &sect 2o", "Valor R$5,00 conforme &sect 2o"),
            ("R&#36;10,00 &para cada parcela", "R$10,00 &para cada parcela"),
            ("R&#36;1.000,00 &not incluidos", "R$1.000,00 &not incluidos"),
        ):
            with self.subTest(text=text):
                self.assertEqual(self._decode(text), want)

    def test_complete_references_still_decode(self):
        self.assertEqual(self._decode("A &amp; B &#x24;5 &sect;2"), "A & B $5 §2")
