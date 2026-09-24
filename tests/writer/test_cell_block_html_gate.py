# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""R3: pins the block-vs-inline classification that gates the clear "rich HTML in a table cell"
error in replace_single_range_with_content. Inline content takes the in-cell path that works;
block/rich content is what triggers the nested-XText RuntimeException, so only it should get the
clear-error treatment. No LibreOffice required (pure markup classification)."""
from plugin.tests.testing_utils import setup_uno_mocks
setup_uno_mocks()

from plugin.writer.format import _content_has_block_markup


def test_plain_text_is_not_block():
    assert _content_has_block_markup("hello world") is False


def test_inline_tags_are_not_block():
    # <b>/<span>/<i> are inline -> they go through the in-cell path that works; the cell-error
    # gate must NOT fire for them.
    assert _content_has_block_markup("<b>bold</b> text") is False


def test_paragraph_is_block():
    # <p> is block -> the path that can raise inside a table cell; the clear-error gate fires here.
    assert _content_has_block_markup("<p>a paragraph</p>") is True


def test_heading_is_block():
    assert _content_has_block_markup("<h1>title</h1>") is True
