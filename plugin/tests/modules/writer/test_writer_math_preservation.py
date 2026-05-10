# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Unit tests to verify math preservation during string expansion."""

import unittest
from unittest.mock import MagicMock, patch
import sys

# Mock UNO modules before any imports
sys.modules['uno'] = MagicMock()
sys.modules['com'] = MagicMock()
sys.modules['com.sun.star'] = MagicMock()
sys.modules['com.sun.star.text'] = MagicMock()

from plugin.modules.writer.format_support import (
    _insert_mixed_html_and_math_at_cursor,
    _insert_mixed_or_plain_html
)
from plugin.modules.writer.content import ApplyDocumentContent

class TestWriterMathPreservation(unittest.TestCase):
    def test_mixed_math_preservation(self):
        """Verify that \nabla in TeX is preserved while \n in HTML is expanded."""
        ctx = MagicMock()
        model = MagicMock()
        cursor = MagicMock()
        
        # r"\nabla" has 1 backslash, r"\n" has 1 backslash.
        test_content = r"<p>Equation: \[\nabla \cdot \mathbf{E}\]</p> and a newline: \nHello."
        
        with patch('plugin.modules.writer.format_support._insert_starwriter_html_at_cursor') as mock_insert_html:
            with patch('plugin.modules.writer.format_support.convert_latex_to_starmath') as mock_convert_tex:
                mock_convert_tex.return_value = MagicMock(ok=True, starmath="mocked")
                
                _insert_mixed_html_and_math_at_cursor(model, ctx, cursor, test_content)
                
                # Check that HTML segment 2 (with the newline) was expanded
                calls = mock_insert_html.call_args_list
                last_html_chunk = calls[-1][0][2]
                self.assertIn("\nHello.", last_html_chunk)
                self.assertNotIn("\\nHello.", last_html_chunk)
                
                # Verify TeX segment 1: \nabla was NOT expanded
                tex_chunk = mock_convert_tex.call_args[0][1]
                self.assertEqual(tex_chunk, r"\nabla \cdot \mathbf{E}")

    def test_plain_html_expansion(self):
        """Verify that expansion still happens when no math is present."""
        ctx = MagicMock()
        model = MagicMock()
        cursor = MagicMock()
        
        test_content = "Line 1\\nLine 2"
        
        with patch('plugin.modules.writer.format_support._insert_starwriter_html_at_cursor') as mock_insert_html:
            _insert_mixed_or_plain_html(model, ctx, cursor, test_content)
            
            inserted_html = mock_insert_html.call_args[0][2]
            self.assertIn("Line 1", inserted_html)
            self.assertIn("Line 2", inserted_html)
            self.assertNotIn("Line 1\\n", inserted_html)

    def test_apply_document_content_plain_text_expansion(self):
        """Verify that ApplyDocumentContent expands \\n for plain text."""
        tool = ApplyDocumentContent()
        ctx = MagicMock()
        ctx.doc = MagicMock()
        ctx.ctx = MagicMock()
        ctx.services = MagicMock()
        
        # Plain text content (no markup)
        content = "Line 1\\nLine 2"
        
        with patch('plugin.modules.writer.format_support.replace_full_document') as mock_replace:
            tool.execute(ctx, content=content, target="full_document")
            
            passed_content = mock_replace.call_args[0][2]
            self.assertEqual(passed_content, "Line 1\nLine 2")

if __name__ == "__main__":
    unittest.main()
