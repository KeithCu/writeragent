# WriterAgent — unit tests for Impress speaker notes tools
# SPDX-License-Identifier: GPL-3.0-or-later

from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from plugin.draw.notes import GetSpeakerNotes, SetSpeakerNotes


class _DisposedException(Exception):
    pass


class _XText:
    def __init__(self, value=""):
        self.value = value

    def getString(self):
        return self.value

    def setString(self, value):
        self.value = value

    def createTextCursor(self):
        return MagicMock()

    def insertString(self, cursor, value, absorb):
        # UNO turns the CR we send into a paragraph break; getString()
        # reports a paragraph break as "\n".
        self.value += value.replace("\r", "\n")


class _NotesShape:
    def __init__(self, text=""):
        self.text = _XText(text)

    def getShapeType(self):
        return "com.sun.star.presentation.NotesShape"

    def getString(self):
        return self.text.value

    def setString(self, value):
        self.text.value = value

    def getText(self):
        return self.text


class _ChromeShape:
    def getShapeType(self):
        return "com.sun.star.presentation.HeaderShape"

    def getString(self):
        return "header"

    def setString(self, value):
        pass


class _NotesPage:
    def __init__(self, *shapes):
        self.shapes = list(shapes)

    def getCount(self):
        return len(self.shapes)

    def getByIndex(self, index):
        return self.shapes[index]


class _Slide:
    def __init__(self, notes_page):
        self.notes_page = notes_page

    def getNotesPage(self):
        return self.notes_page


class _DisposedSlide(_Slide):
    def __init__(self):
        super().__init__(None)

    def getNotesPage(self):
        raise _DisposedException("notes page object disposed")


class _FakeContext:
    def __init__(self, doc=None, active_page_index=None):
        self.doc = doc or MagicMock()
        self.active_page_index = active_page_index


def _ctx(active_page_index: int | None = None, doc: Any = None) -> Any:
    return _FakeContext(doc=doc, active_page_index=active_page_index)


def test_get_returns_empty_and_ok_when_notes_page_missing():
    slide = _Slide(None)
    ctx = _ctx()
    with patch("plugin.draw.bridge.DrawBridge.get_slide_for_tool", return_value=slide):
        res = GetSpeakerNotes().execute(ctx, page=0)
    assert res == {"status": "ok", "page": 0, "notes": ""}


def test_get_reads_notes_body_behind_chrome():
    notes_page = _NotesPage(_ChromeShape(), _NotesShape("body"))
    slide = _Slide(notes_page)
    ctx = _ctx()
    with patch("plugin.draw.bridge.DrawBridge.get_slide_for_tool", return_value=slide):
        res = GetSpeakerNotes().execute(ctx, page=0)
    assert res == {"status": "ok", "page": 0, "notes": "body"}


def test_set_missing_notes_page_names_slide():
    slide = _Slide(None)
    ctx = _ctx()
    with patch("plugin.draw.bridge.DrawBridge.get_slide_for_tool", return_value=slide):
        out = SetSpeakerNotes().execute(ctx, text="hello", page=0)
    assert out["status"] == "error"
    assert out["code"] == "NOTES_UNAVAILABLE"
    assert out.get("page", out.get("details", {}).get("page")) == 0
    assert "Slide 0" in out["message"]
    assert "no notes page" in out["message"]


def test_set_missing_notes_shape_names_slide():
    notes_page = _NotesPage(_ChromeShape())
    slide = _Slide(notes_page)
    ctx = _ctx()
    with patch("plugin.draw.bridge.DrawBridge.get_slide_for_tool", return_value=slide):
        out = SetSpeakerNotes().execute(ctx, text="hello", page=0)
    assert out["status"] == "error"
    assert out["code"] == "NOTES_UNAVAILABLE"
    assert out.get("page", out.get("details", {}).get("page")) == 0
    assert "Slide 0" in out["message"]
    assert "no notes body" in out["message"]


def test_set_replaces_and_normalizes_crlf():
    notes_shape = _NotesShape("initial")
    notes_page = _NotesPage(notes_shape)
    slide = _Slide(notes_page)
    ctx = _ctx()
    with patch("plugin.draw.bridge.DrawBridge.get_slide_for_tool", return_value=slide):
        out = SetSpeakerNotes().execute(ctx, text="one\r\ntwo\r\nthree", page=0)
    assert out["status"] == "ok"
    body = notes_shape.getString()
    assert body == "one\ntwo\nthree"
    assert "\r" not in body
    assert body.splitlines() == ["one", "two", "three"]


def test_append_adds_one_paragraph():
    notes_shape = _NotesShape("one\ntwo")
    notes_page = _NotesPage(notes_shape)
    slide = _Slide(notes_page)
    ctx = _ctx()
    with patch("plugin.draw.bridge.DrawBridge.get_slide_for_tool", return_value=slide):
        out = SetSpeakerNotes().execute(ctx, text="three", page=0, append=True)
    assert out["status"] == "ok"
    assert notes_shape.getString() == "one\ntwo\nthree"


def test_append_after_trailing_newline_does_not_add_blank_paragraph():
    notes_shape = _NotesShape("one\n")
    notes_page = _NotesPage(notes_shape)
    slide = _Slide(notes_page)
    ctx = _ctx()
    with patch("plugin.draw.bridge.DrawBridge.get_slide_for_tool", return_value=slide):
        out = SetSpeakerNotes().execute(ctx, text="two", page=0, append=True)
    assert out["status"] == "ok"
    body = notes_shape.getString()
    assert body == "one\ntwo"
    assert "\n\n" not in body


def test_append_to_whitespace_only_body_replaces_it():
    notes_shape = _NotesShape("\n")
    notes_page = _NotesPage(notes_shape)
    slide = _Slide(notes_page)
    ctx = _ctx()
    with patch("plugin.draw.bridge.DrawBridge.get_slide_for_tool", return_value=slide):
        out = SetSpeakerNotes().execute(ctx, text="add", page=0, append=True)
    assert out["status"] == "ok"
    assert notes_shape.getString() == "add"


def test_append_default_is_replace():
    notes_shape = _NotesShape("old")
    notes_page = _NotesPage(notes_shape)
    slide = _Slide(notes_page)
    ctx = _ctx()
    with patch("plugin.draw.bridge.DrawBridge.get_slide_for_tool", return_value=slide):
        out = SetSpeakerNotes().execute(ctx, text="new", page=0)
    assert out["status"] == "ok"
    assert notes_shape.getString() == "new"


def test_resolution_explicit_page():
    slide = _Slide(_NotesPage(_NotesShape("ok")))
    ctx = _ctx()
    with patch("plugin.draw.bridge.DrawBridge.get_slide_for_tool", return_value=slide) as mock_get_slide:
        GetSpeakerNotes().execute(ctx, page=2)
    mock_get_slide.assert_called_once_with(ctx.doc, 2)


def test_resolution_omitted_page_with_active_page_index():
    slide = _Slide(_NotesPage(_NotesShape("ok")))
    ctx = _ctx(active_page_index=1)
    with patch("plugin.draw.bridge.DrawBridge.get_slide_for_tool", return_value=slide) as mock_get_slide:
        GetSpeakerNotes().execute(ctx)
    mock_get_slide.assert_called_once_with(ctx.doc, 1)


def test_resolution_omitted_page_falls_back_to_bridge():
    slide = _Slide(_NotesPage(_NotesShape("ok")))
    ctx = _ctx(active_page_index=None)
    with patch("plugin.draw.bridge.DrawBridge.get_active_page_index", return_value=4), \
         patch("plugin.draw.bridge.DrawBridge.get_slide_for_tool", return_value=slide) as mock_get_slide:
        GetSpeakerNotes().execute(ctx)
    mock_get_slide.assert_called_once_with(ctx.doc, 4)


def test_disposed_notes_page_propagates():
    slide = _DisposedSlide()
    ctx = _ctx()
    with patch("plugin.draw.bridge.DrawBridge.get_slide_for_tool", return_value=slide):
        with pytest.raises(_DisposedException):
            GetSpeakerNotes().execute(ctx, page=0)
        with pytest.raises(_DisposedException):
            SetSpeakerNotes().execute(ctx, text="hi", page=0)
