# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Unit tests to verify math preservation during string expansion."""

from unittest.mock import MagicMock, patch
import sys

# Mock UNO modules before any imports
sys.modules['uno'] = MagicMock()
sys.modules['com'] = MagicMock()
sys.modules['com.sun.star'] = MagicMock()
sys.modules['com.sun.star.text'] = MagicMock()

from plugin.writer.format import (
    _insert_mixed_html_and_math_at_cursor,
    _insert_mixed_or_plain_html
)
from plugin.writer.content import ApplyDocumentContent

class TestWriterMathPreservation:
    def test_mixed_math_preservation(self):
        """Verify that \nabla in TeX is preserved while \n in HTML is expanded."""
        ctx = MagicMock()
        model = MagicMock()
        cursor = MagicMock()
        
        # r"\nabla" has 1 backslash, r"\n" has 1 backslash.
        test_content = r"<p>Equation: \[\nabla \cdot \mathbf{E}\]</p> and a newline: \nHello."
        
        with patch('plugin.writer.html_import._insert_starwriter_html_at_cursor') as mock_insert_html:
            with patch('plugin.writer.html_import.convert_latex_to_starmath') as mock_convert_tex:
                mock_convert_tex.return_value = MagicMock(ok=True, starmath="mocked")
                
                _insert_mixed_html_and_math_at_cursor(model, ctx, cursor, test_content)
                
                # Check that HTML segment 2 (with the newline) was expanded
                calls = mock_insert_html.call_args_list
                last_html_chunk = calls[-1][0][2]
                assert ("\nHello.") in (last_html_chunk)
                assert ("\\nHello.") not in (last_html_chunk)
                
                # Verify TeX segment 1: \nabla was NOT expanded
                tex_chunk = mock_convert_tex.call_args[0][1]
                assert (tex_chunk) == (r"\nabla \cdot \mathbf{E}")

    def test_brl_currency_never_becomes_a_formula(self):
        """End-to-end insert path: ``R$ 1.234,56`` pairs must not build a Math object.

        Regression for the Writer corruption where the text between two ``R$``
        amounts was swallowed into a formula (italic serif, spaces eaten).
        """
        ctx = MagicMock()
        model = MagicMock()
        cursor = MagicMock()

        content = (
            "<p>Requer a condena\u00e7\u00e3o em R$ 12.798,82, como montante "
            "indenizat\u00f3rio por ventura arbitrado, evitando-se, assim, o "
            "enriquecimento sem causa, al\u00e9m de R$ 500,00 de custas.</p>"
        )

        with patch('plugin.writer.html_import._insert_starwriter_html_at_cursor') as mock_insert_html:
            with patch('plugin.writer.html_import.convert_latex_to_starmath') as mock_convert_tex:
                with patch('plugin.writer.html_import.insert_writer_math_formula') as mock_formula:
                    _insert_mixed_or_plain_html(model, ctx, cursor, content)

        mock_convert_tex.assert_not_called()
        mock_formula.assert_not_called()
        inserted = "".join(c[0][2] for c in mock_insert_html.call_args_list)
        assert ("R$ 12.798,82") in (inserted)
        assert ("R$ 500,00") in (inserted)
        assert ("evitando-se, assim") in (inserted)

    def test_plain_html_expansion(self):
        """Verify that expansion still happens when no math is present."""
        ctx = MagicMock()
        model = MagicMock()
        cursor = MagicMock()
        
        test_content = "Line 1\\nLine 2"
        
        with patch('plugin.writer.html_import._insert_starwriter_html_at_cursor') as mock_insert_html:
            _insert_mixed_or_plain_html(model, ctx, cursor, test_content)
            
            inserted_html = mock_insert_html.call_args[0][2]
            assert ("Line 1") in (inserted_html)
            assert ("Line 2") in (inserted_html)
            assert ("Line 1\\n") not in (inserted_html)

    def test_apply_document_content_plain_text_expansion(self):
        """Verify that ApplyDocumentContent expands \\n for plain text."""
        tool = ApplyDocumentContent()
        ctx = MagicMock()
        ctx.doc = MagicMock()
        # The edit path is atomic in ALL modes now (R4 fix): it opens a grouped undo context and
        # refuses when the undo manager is unusable/locked — a bare MagicMock's isLocked() would be
        # truthy, so pin a usable manager for the fake document.
        ctx.doc.getUndoManager.return_value.isLocked.return_value = False
        ctx.ctx = MagicMock()
        ctx.services = MagicMock()
        
        # Plain text content (no markup)
        content = "Line 1\\nLine 2"
        
        with patch('plugin.writer.format.replace_full_document') as mock_replace:
            tool.execute(ctx, content=content, target="full_document")
            
            passed_content = mock_replace.call_args[0][2]
            assert (passed_content) == ("Line 1\nLine 2")

