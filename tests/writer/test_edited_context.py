# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""edited_context: successful apply_document_content edits echo the touched paragraph(s).

The echo must be right or absent, never wrong: paragraph_window_text returns None whenever the
paragraph walk fails, and attach_edited_context only adds the field when a snippet came back.
No LibreOffice required — fakes implement the minimal XText/XParagraphCursor protocol."""
import pytest
from unittest.mock import MagicMock, patch


from plugin.writer.edit_review import (
    _EDITED_CONTEXT_MAX_CHARS,
    attach_edited_context,
    collapsed_anchor,
    paragraph_window_text,
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

    def getPropertyValue(self, _name):
        return None

    def createEnumeration(self):
        # Outline-hyperlink walk uses enum_has_more, which requires UNO True; a bare MagicMock would loop forever.
        return MagicMock(hasMoreElements=MagicMock(return_value=False))

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

    def getStart(self):
        return FakeCursor(self, 0)

    def getEnd(self):
        return FakeCursor(self, max(0, len(self.paragraphs) - 1), at_end=True)

    def insertString(self, _cursor, _s, _absorb):
        pass


def _anchor_at(text, para):
    return FakeCursor(text, para)


def test_window_is_paragraph_plus_neighbors():
    text = FakeText(["P0", "P1-EDITED", "P2", "P3"])
    s = paragraph_window_text(_anchor_at(text, 1))
    assert s == "P0\nP1-EDITED\nP2"


def test_window_clamps_at_document_edges():
    text = FakeText(["FIRST", "SECOND"])
    assert paragraph_window_text(_anchor_at(text, 0)) == "FIRST\nSECOND"
    assert paragraph_window_text(_anchor_at(text, 1)) == "FIRST\nSECOND"


def test_window_truncates_around_the_middle():
    text = FakeText(["A" * 600, "B" * 600, "C" * 600])
    s = paragraph_window_text(_anchor_at(text, 1))
    assert len(s) <= _EDITED_CONTEXT_MAX_CHARS
    assert " [...] " in s
    assert s.startswith("A") and s.endswith("C")


def test_failure_yields_none_not_garbage():
    assert paragraph_window_text(None) is None

    class Broken:
        def getText(self):
            raise RuntimeError("nested exotic text")

    assert paragraph_window_text(Broken()) is None


def test_blank_window_yields_none():
    text = FakeText(["", "", ""])
    assert paragraph_window_text(_anchor_at(text, 1)) is None


def test_attach_only_when_snippet_exists():
    text = FakeText(["P0", "P1", "P2"])
    ok = attach_edited_context({"status": "ok"}, _anchor_at(text, 1))
    assert ok["edited_context"] == "P0\nP1\nP2"
    no = attach_edited_context({"status": "ok"}, None)
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

    assert collapsed_anchor(Range()) is None


@pytest.mark.timeout(5)
@pytest.mark.parametrize(
    "mode, kwargs",
    [
        ("shape_edit", {"content": "new", "target": "search", "old_content": "old"}),
        ("end", {"content": "new", "target": "end"}),
        ("beginning", {"content": "new", "target": "beginning"}),
        ("selection", {"content": "new", "target": "selection"}),
        ("all_matches", {"content": "new", "target": "search", "old_content": "old", "all_matches": True}),
        ("position_before", {"content": "new", "target": "search", "old_content": "old", "position": "before"}),
        ("replace_one", {"content": "new", "target": "search", "old_content": "old"}),
    ],
)
def test_apply_document_content_edited_context_wired(mode, kwargs):
    """All six attach_edited_context call sites in apply_document_content are wired.

    The 5s timeout guards against MagicMock.hasMoreElements() being truthy in the
    outline-hyperlink walk (enum_has_more requires UNO True). Review mode is patched
    because this file does not init config and get_agent_edit_review_mode reads it first.
    """
    from contextlib import ExitStack
    from plugin.writer import html_import, html_import_styles
    from plugin.writer.apply_document_content import ApplyDocumentContent

    text = FakeText(["P0", "old", "P2"])
    found = FakeCursor(text, 1)
    ctx = MagicMock()
    ctx.doc.getUndoManager.return_value.isLocked.return_value = False
    ctx.doc.getText.return_value = text
    controller = ctx.doc.getCurrentController.return_value
    controller.getSelection.return_value = None
    controller.getViewCursor.return_value = FakeCursor(text, 1)
    ctx.services.get.return_value = MagicMock()

    with ExitStack() as stack:
        stack.enter_context(patch.object(html_import, "content_has_markup", return_value=False))
        stack.enter_context(patch.object(html_import, "content_has_block_markup", return_value=False))
        stack.enter_context(patch.object(html_import, "insert_content_at_position", return_value=html_import_styles.WriteReport()))
        stack.enter_context(patch("plugin.writer.apply_document_content.record_preserve_replace"))
        stack.enter_context(patch("plugin.writer.apply_document_content.get_agent_edit_review_mode", return_value="off"))
        stack.enter_context(patch("plugin.writer.apply_document_content.review_recording_enabled", return_value=False))

        if mode == "shape_edit":
            shape = MagicMock()
            stack.enter_context(patch("plugin.writer.search.find_first_range", return_value=None))
            stack.enter_context(patch("plugin.writer.search.drawing_shape_object_containing", return_value=shape))
            stack.enter_context(patch("plugin.writer.search.shape_display_name", return_value="Box1"))
            stack.enter_context(patch("plugin.writer.apply_document_content.replace_text_in_shape", return_value=True))
        elif mode == "all_matches":
            stack.enter_context(patch("plugin.writer.search.find_all_ranges", return_value=[found]))
        elif mode in ("position_before", "replace_one"):
            stack.enter_context(patch("plugin.writer.search.find_first_range", return_value=found))

        res = ApplyDocumentContent().execute(ctx, **kwargs)

    assert res["status"] == "ok"
    if mode == "shape_edit":
        # Shape edits (apply_document_content.py:706) pass anchor=None by design: no paragraph window.
        assert res["review_bypassed"] is True and "edited_context" not in res
    else:
        expected = {"end": "old\nP2", "beginning": "P0\nold"}.get(mode, "P0\nold\nP2")
        assert res.get("edited_context") == expected


def test_uno_read_probes_raise_on_disposal_and_default_otherwise():
    from plugin.framework.errors import DocumentDisposedError
    from plugin.writer.edit_review import _is_blocking_redline, collapsed_anchor, paragraph_window_text

    class DisposedException(Exception):
        pass

    class RuntimeException(Exception):
        pass

    closed = MagicMock()
    closed.getText.side_effect = DisposedException("closed")
    with pytest.raises(DocumentDisposedError):
        collapsed_anchor(closed)
    with pytest.raises(DocumentDisposedError):
        paragraph_window_text(closed)

    odd = MagicMock()
    odd.getText.side_effect = RuntimeException("uno")
    assert collapsed_anchor(odd) is None
    assert paragraph_window_text(odd) is None

    # An unreadable RedlineType on a redline portion still fails closed.
    node = MagicMock()
    node.getPropertyValue.side_effect = lambda name: "Redline" if name == "TextPortionType" else (_ for _ in ()).throw(RuntimeException("uno"))
    assert _is_blocking_redline(node) is True


def test_window_text_leaves_out_tracked_deletions():
    """In review mode the replaced text is still in the document as a tracked deletion;
    the echo reads the paragraphs without it, matching other reads."""
    text = FakeText(["P0", "P1", "P2"])
    with patch("plugin.writer.edit_review.get_string_without_tracked_deletions", return_value="visible") as visible:
        assert paragraph_window_text(_anchor_at(text, 1)) == "visible"
    assert visible.call_args[0][0].getString() == "P0\nP1\nP2"
