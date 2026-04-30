import sys
import types
from typing import Any
from unittest.mock import MagicMock
import pytest

# Mock UNO modules before any plugin imports
sys.modules["uno"] = MagicMock()
sys.modules["unohelper"] = MagicMock()

# Mock com.sun.star hierarchy
star = types.ModuleType("com.sun.star")
setattr(star, "text", types.ModuleType("com.sun.star.text"))

class MockBase: pass
setattr(getattr(star, "text"), "Footnote", MockBase)
setattr(getattr(star, "text"), "Endnote", MockBase)

sys.modules["com"] = types.ModuleType("com")
setattr(sys.modules["com"], "sun", types.ModuleType("com.sun"))
setattr(sys.modules["com"].sun, "star", star)

# Now we can safely import our module
from plugin.modules.writer.footnotes import (
    FootnotesInsert, FootnotesList, FootnotesEdit, FootnotesDelete,
    FootnotesSettingsGet, FootnotesSettingsUpdate
)

# Fake Context
class FakeCtx:
    def __init__(self, doc):
        self.doc = doc

def test_footnotes_insert():
    doc = MagicMock()
    doc.supportsService.return_value = True

    ctrl = MagicMock()
    v_cursor = MagicMock()
    t_cursor = MagicMock()

    ctrl.getViewCursor.return_value = v_cursor
    v_cursor.getText.return_value.createTextCursorByRange.return_value = t_cursor

    doc.getCurrentController.return_value = ctrl

    note = MagicMock()
    note.getLabel.return_value = "*"
    doc.createInstance.return_value = note

    tool = FootnotesInsert()
    ctx = FakeCtx(doc)

    res = tool.execute(ctx, note_type="footnote", text="My footnote text", label="*")

    assert res["status"] == "ok"
    doc.createInstance.assert_called_with("com.sun.star.text.Footnote")
    note.setLabel.assert_called_with("*")
    note.setString.assert_called_with("My footnote text")
    t_cursor.getText.return_value.insertTextContent.assert_called_with(t_cursor, note, False)


def test_footnotes_insert_with_insert_after_text():
    doc = MagicMock()
    doc.supportsService.return_value = True

    sd = MagicMock()
    doc.createSearchDescriptor.return_value = sd

    found_range = MagicMock()
    text_mock = MagicMock()
    t_cursor = MagicMock()
    insert_surface = MagicMock()
    found_range.getText.return_value = text_mock
    text_mock.createTextCursorByRange.return_value = t_cursor
    t_cursor.getText.return_value = insert_surface

    doc.findFirst.return_value = found_range

    note = MagicMock()
    note.getLabel.return_value = ""
    doc.createInstance.return_value = note

    tool = FootnotesInsert()
    ctx = FakeCtx(doc)

    res = tool.execute(
        ctx,
        note_type="footnote",
        text="This sentence is only a test.",
        insert_after_text="This is a test.",
    )

    assert res["status"] == "ok"
    doc.createSearchDescriptor.assert_called_once()
    assert sd.SearchString == "This is a test."
    assert sd.SearchCaseSensitive is True
    doc.findFirst.assert_called_once_with(sd)
    text_mock.createTextCursorByRange.assert_called_once_with(found_range)
    t_cursor.collapseToEnd.assert_called_once()
    doc.getCurrentController.assert_not_called()
    insert_surface.insertTextContent.assert_called_once_with(t_cursor, note, False)
    note.setString.assert_called_once_with("This sentence is only a test.")


def test_footnotes_insert_anchor_no_match():
    doc = MagicMock()
    doc.supportsService.return_value = True
    doc.createSearchDescriptor.return_value = MagicMock()
    doc.findFirst.return_value = None

    res = FootnotesInsert().execute(
        FakeCtx(doc),
        note_type="footnote",
        text="x",
        insert_after_text="not in document",
    )
    assert res["status"] == "error"
    assert "No match" in res["message"]


def test_footnotes_insert_anchor_occurrence_too_high():
    doc = MagicMock()
    doc.supportsService.return_value = True
    doc.createSearchDescriptor.return_value = MagicMock()
    m1 = MagicMock()
    doc.findFirst.return_value = m1
    doc.findNext.return_value = None

    res = FootnotesInsert().execute(
        FakeCtx(doc),
        note_type="footnote",
        text="x",
        insert_after_text="foo",
        occurrence=1,
    )
    assert res["status"] == "error"
    assert "not enough occurrences" in res["message"]


def test_footnotes_list():
    doc = MagicMock()
    doc.supportsService.return_value = True

    supplier = MagicMock()
    supplier.getCount.return_value = 2

    note1 = MagicMock()
    note1.getLabel.return_value = ""
    note1.getString.return_value = "Auto numbered note"

    note2 = MagicMock()
    note2.getLabel.return_value = "*"
    note2.getString.return_value = "Custom marked note"

    def side_effect(idx):
        if idx == 0: return note1
        if idx == 1: return note2

    supplier.getByIndex.side_effect = side_effect
    doc.getFootnotes.return_value = supplier

    tool = FootnotesList()
    ctx = FakeCtx(doc)

    res = tool.execute(ctx, note_type="footnote")

    assert res["status"] == "ok"
    assert res["count"] == 2
    assert len(res["notes"]) == 2
    assert res["notes"][0]["label"] == ""
    assert res["notes"][1]["label"] == "*"

def test_footnotes_edit():
    doc = MagicMock()
    doc.supportsService.return_value = True

    supplier = MagicMock()
    supplier.getCount.return_value = 1

    note = MagicMock()
    supplier.getByIndex.return_value = note
    doc.getFootnotes.return_value = supplier

    tool = FootnotesEdit()
    ctx = FakeCtx(doc)

    res = tool.execute(ctx, note_type="footnote", index=0, text="New text", label="")

    assert res["status"] == "ok"
    note.setString.assert_called_with("New text")
    note.setLabel.assert_called_with("")

def test_footnotes_delete():
    doc = MagicMock()
    doc.supportsService.return_value = True

    supplier = MagicMock()
    supplier.getCount.return_value = 1

    note = MagicMock()
    anchor = MagicMock()
    note.getAnchor.return_value = anchor

    supplier.getByIndex.return_value = note
    doc.getFootnotes.return_value = supplier

    tool = FootnotesDelete()
    ctx = FakeCtx(doc)

    res = tool.execute(ctx, note_type="footnote", index=0)

    assert res["status"] == "ok"
    anchor.setString.assert_called_with("")

def test_invalid_note_type():
    doc = MagicMock()
    tool = FootnotesList()
    ctx = FakeCtx(doc)

    res = tool.execute(ctx, note_type="invalid_type")
    assert res["status"] == "error"
    assert "Invalid note_type" in res["message"]

def test_footnotes_settings_get():
    doc = MagicMock()
    doc.supportsService.return_value = True

    settings = MagicMock()

    def get_property_value(name):
        return f"value_for_{name}"

    settings.getPropertyValue.side_effect = get_property_value
    doc.getFootnoteSettings.return_value = settings

    tool = FootnotesSettingsGet()
    ctx = FakeCtx(doc)

    res = tool.execute(ctx, note_type="footnote")

    assert res["status"] == "ok"
    assert res["settings"]["Prefix"] == "value_for_Prefix"
    assert res["settings"]["BeginNotice"] == "value_for_BeginNotice"

def test_footnotes_settings_update():
    doc = MagicMock()
    doc.supportsService.return_value = True

    settings = MagicMock()
    doc.getFootnoteSettings.return_value = settings

    tool = FootnotesSettingsUpdate()
    ctx = FakeCtx(doc)

    res = tool.execute(ctx, note_type="footnote", properties={"Prefix": "[", "Suffix": "]"})

    assert res["status"] == "ok"
    assert "Prefix" in res["updated_properties"]
    assert "Suffix" in res["updated_properties"]
    settings.setPropertyValue.assert_any_call("Prefix", "[")
    settings.setPropertyValue.assert_any_call("Suffix", "]")
