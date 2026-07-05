# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""edited_context: successful apply_document_content edits echo the touched paragraph(s).

The echo must be right or absent, never wrong: _paragraph_window_text returns None whenever the
paragraph walk fails, and _attach_edited_context only adds the field when a snippet came back.
No LibreOffice required — fakes implement the minimal XText/XParagraphCursor protocol."""
from unittest.mock import MagicMock, patch

from plugin.tests.testing_utils import setup_uno_mocks
setup_uno_mocks()

from plugin.writer.content import (
    _EDITED_CONTEXT_MAX_CHARS,
    _attach_edited_context,
    _collapsed_anchor,
    _paragraph_window_text,
)


class FakeCursor:
    """Minimal XTextCursor + XParagraphCursor stand-in over a paragraph list."""

    def __init__(self, text, para, at_end=False):
        self.text = text
        self.para = para          # paragraph index of the position
        self.at_end = at_end      # False -> at paragraph start
        self.span_from = None     # set by gotoRange(expand=True)

    # --- XTextRange-ish ---
    def getText(self):
        return self.text

    def getStart(self):
        return FakeCursor(self.text, self.para, at_end=False if self.span_from is None else False)

    def getEnd(self):
        return FakeCursor(self.text, self.para, at_end=True)

    # --- XParagraphCursor ---
    def gotoStartOfParagraph(self, expand):
        self.at_end = False
        return True

    def gotoEndOfParagraph(self, expand):
        self.at_end = True
        return True

    def gotoPreviousParagraph(self, expand):
        if self.para == 0:
            return False
        self.para -= 1
        return True

    def gotoNextParagraph(self, expand):
        if self.para >= len(self.text.paragraphs) - 1:
            return False
        self.para += 1
        return True

    def gotoRange(self, other, expand):
        assert expand is True
        self.span_from = min(self.para, self.para)
        self.span_to = other.para
        return True

    def getString(self):
        if self.span_from is None:
            return self.text.paragraphs[self.para]
        return "\n".join(self.text.paragraphs[self.para:self.span_to + 1])


class FakeText:
    def __init__(self, paragraphs):
        self.paragraphs = paragraphs

    def createTextCursorByRange(self, rng):
        return FakeCursor(self, rng.para, at_end=rng.at_end)


def _anchor_at(text, para):
    return FakeCursor(text, para)


def test_window_is_paragraph_plus_neighbors():
    text = FakeText(["P0", "P1-EDITED", "P2", "P3"])
    s = _paragraph_window_text(_anchor_at(text, 1))
    assert s == "P0\nP1-EDITED\nP2"


def test_window_clamps_at_document_edges():
    text = FakeText(["FIRST", "SECOND"])
    assert _paragraph_window_text(_anchor_at(text, 0)) == "FIRST\nSECOND"
    assert _paragraph_window_text(_anchor_at(text, 1)) == "FIRST\nSECOND"


def test_window_truncates_around_the_middle():
    text = FakeText(["A" * 600, "B" * 600, "C" * 600])
    s = _paragraph_window_text(_anchor_at(text, 1))
    assert len(s) <= _EDITED_CONTEXT_MAX_CHARS
    assert " [...] " in s
    assert s.startswith("A") and s.endswith("C")


def test_failure_yields_none_not_garbage():
    assert _paragraph_window_text(None) is None

    class Broken:
        def getText(self):
            raise RuntimeError("nested exotic text")

    assert _paragraph_window_text(Broken()) is None


def test_blank_window_yields_none():
    text = FakeText(["", "", ""])
    assert _paragraph_window_text(_anchor_at(text, 1)) is None


def test_attach_only_when_snippet_exists():
    text = FakeText(["P0", "P1", "P2"])
    ok = _attach_edited_context({"status": "ok"}, _anchor_at(text, 1))
    assert ok["edited_context"] == "P0\nP1\nP2"
    no = _attach_edited_context({"status": "ok"}, None)
    assert "edited_context" not in no


def test_collapsed_anchor_is_best_effort():
    class NoCursorText:
        def createTextCursorByRange(self, rng):
            raise RuntimeError("unsupported")

    class Range:
        def getText(self):
            return NoCursorText()

        def getStart(self):
            return self

    assert _collapsed_anchor(Range()) is None


def test_apply_document_content_edited_context_on_success():
    from plugin.writer import format as format_support
    from plugin.writer.content import ApplyDocumentContent

    found = MagicMock()
    found.getString.return_value = "old"
    ctx = MagicMock()
    ctx.doc.getUndoManager.return_value.isLocked.return_value = False
    ctx.services.get.return_value = MagicMock()
    anchor = MagicMock()
    with patch("plugin.writer.content._find_first_range", return_value=found), \
         patch("plugin.writer.content._collapsed_anchor", return_value=anchor), \
         patch("plugin.writer.content._attach_edited_context", side_effect=lambda r, a: {**r, "edited_context": "echo"}), \
         patch.object(format_support, "content_has_markup", return_value=False), \
         patch("plugin.writer.content._record_preserve_replace"):
        res = ApplyDocumentContent().execute(
            ctx, content="new", target="search", old_content="old")
    assert res["status"] == "ok" and res.get("edited_context") == "echo"
