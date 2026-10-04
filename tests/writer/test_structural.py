"""clone_heading_block must not clone paragraph 0 when the locator misses."""

from unittest.mock import MagicMock

import pytest

from plugin.doc.document_helpers import DocumentService
from plugin.framework.errors import ToolExecutionError
from plugin.tests.testing_utils import ElementStub, WriterDocStub
from plugin.writer.structural import CloneHeadingBlock, _resolve_para_index


def _doc():
    return WriterDocStub(
        [
            ElementStub("Preamble"),
            ElementStub("Alpha", outline_level=1),
            ElementStub("body"),
        ]
    )


def _ctx(doc):
    ctx = MagicMock()
    ctx.doc = doc
    ctx.services.document = DocumentService()
    return ctx


def test_resolve_para_index_heading_text_is_not_paragraph_zero():
    doc = _doc()
    assert _resolve_para_index(_ctx(doc), {"locator": "heading_text:Alpha"}) == 1


def test_resolve_para_index_missing_heading_text_raises():
    with pytest.raises(ToolExecutionError, match="No heading matching 'Missing'"):
        _resolve_para_index(_ctx(_doc()), {"locator": "heading_text:Missing"})


def test_clone_heading_block_missing_locator_does_not_insert():
    doc = _doc()
    inserted = []
    original_get_text = doc.getText

    def tracking_get_text():
        text = original_get_text()
        text.insertString = lambda *_args, **_kwargs: inserted.append(True)
        text.insertControlCharacter = lambda *_args, **_kwargs: inserted.append(True)
        return text

    doc.getText = tracking_get_text
    result = CloneHeadingBlock().execute(_ctx(doc), locator="heading_text:Missing")
    assert result["status"] == "error"
    assert "No heading matching 'Missing'" in result["message"]
    assert inserted == []
