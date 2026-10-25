import sys
import types
from unittest.mock import MagicMock

# Mock UNO modules before any plugin imports
sys.modules.setdefault("uno", MagicMock())
if "unohelper" not in sys.modules or not hasattr(sys.modules["unohelper"], "Base") or isinstance(sys.modules["unohelper"].Base, MagicMock):
    uh = types.ModuleType("unohelper")
    uh.Base = type("MockUnohelperBase", (object,), {})
    sys.modules["unohelper"] = uh

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
from plugin.writer.specialized.footnotes import (
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

    res = tool.execute(ctx, note="footnote", text="My footnote text", label="*")

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
        note="footnote",
        text="This sentence is only a test.",
        insert_after="This is a test.",
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
        note="footnote",
        text="x",
        insert_after="not in document",
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
        note="footnote",
        text="x",
        insert_after="foo",
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

    res = tool.execute(ctx, note="footnote")

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

    res = tool.execute(ctx, note="footnote", index=0, text="New text", label="")

    assert res["status"] == "ok"
    note.setString.assert_called_with("New text")
    note.setLabel.assert_called_with("")


def test_footnotes_edit_missing_text_and_label_reports_error():
    doc = MagicMock()
    doc.supportsService.return_value = True
    tool = FootnotesEdit()
    ctx = FakeCtx(doc)

    res = tool.execute(ctx, note="footnote", index=0)
    assert res["status"] == "error"
    assert "Provide text and/or label." in res["message"]


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

    res = tool.execute(ctx, note="footnote", index=0)

    assert res["status"] == "ok"
    anchor.setString.assert_called_with("")

def test_invalid_note_type():
    tool = FootnotesList()
    ok, message = tool.validate(note="invalid_type")
    assert ok is False
    assert message is not None and "note" in message

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

    res = tool.execute(ctx, note="footnote")

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

    res = tool.execute(ctx, note="footnote", properties={"Prefix": "[", "Suffix": "]"})

    assert res["status"] == "ok"
    assert "Prefix" in res["updated_properties"]
    assert "Suffix" in res["updated_properties"]
    settings.setPropertyValue.assert_any_call("Prefix", "[")
    settings.setPropertyValue.assert_any_call("Suffix", "]")


def test_footnotes_shortened_type_param():
    doc = MagicMock()
    doc.supportsService.return_value = True
    settings = MagicMock()
    doc.getFootnoteSettings.return_value = settings
    res = FootnotesSettingsGet().execute(FakeCtx(doc), note="footnote")
    assert res["status"] == "ok"


def test_footnotes_disposal_is_document_disposed():
    from plugin.framework.errors import DocumentDisposedError
    tool = FootnotesList()
    ctx = FakeCtx(MagicMock())
    ctx.doc.getFootnotes.side_effect = DocumentDisposedError("gone")
    res = tool.execute_safe(ctx, note="footnote")
    assert res["status"] == "error"
    assert res["code"] == "DOCUMENT_DISPOSED"


def test_footnotes_settings_update_partial_failure():
    doc = MagicMock()
    doc.supportsService.return_value = True

    settings = MagicMock()

    def fake_set_property_value(name, val):
        if name == "BadProp":
            raise RuntimeError("Unknown property")

    settings.setPropertyValue.side_effect = fake_set_property_value
    doc.getFootnoteSettings.return_value = settings

    tool = FootnotesSettingsUpdate()
    ctx = FakeCtx(doc)

    res = tool.execute(ctx, note="footnote", properties={"Prefix": "[", "BadProp": "invalid"})

    assert res["status"] == "error"
    assert "Prefix" in res["updated_properties"]
    assert "Prefix" in res["message"]
    assert "BadProp" in res["message"]


def test_footnotes_out_of_range_index():
    doc = MagicMock()
    doc.supportsService.return_value = True

    supplier = MagicMock()
    supplier.getCount.return_value = 2
    doc.getFootnotes.return_value = supplier

    ctx = FakeCtx(doc)

    res_edit = FootnotesEdit().execute(ctx, note="footnote", index=5, text="abc")
    assert res_edit["status"] == "error"
    assert "Invalid index 5. The document has 2 footnote(s)." in res_edit["message"]

    res_delete = FootnotesDelete().execute(ctx, note="footnote", index=-1)
    assert res_delete["status"] == "error"
    assert "Invalid index -1. The document has 2 footnote(s)." in res_delete["message"]


def test_footnotes_schema_validation():
    tool = FootnotesEdit()
    ok, message = tool.validate(note="footnote", index="non_numeric", text="abc")
    assert ok is False
    assert message is not None and "index" in message


def test_footnotes_missing_note_raises():
    import pytest

    tool = FootnotesList()
    ctx = FakeCtx(MagicMock())
    with pytest.raises(KeyError):
        tool.execute(ctx)

