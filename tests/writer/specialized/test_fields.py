import pytest
import sys
from unittest.mock import MagicMock

# Ensure UNO is mocked if running outside LibreOffice
try:
    import uno  # noqa: F401
except ImportError:
    sys.modules["uno"] = MagicMock()
    sys.modules["unohelper"] = MagicMock()
    com_mock = MagicMock()
    sys.modules["com"] = com_mock
    sys.modules["com.sun.star.text"] = MagicMock()

from plugin.tests.testing_utils import TestingFactory
from plugin.writer.specialized.fields import FieldsInsert, FieldsList, FieldsDelete


@pytest.fixture
def mock_ctx():
    # Fields APIs (getTextFields, etc.) stay MagicMock; factory owns ToolContext wiring.
    return TestingFactory.create_context(doc=MagicMock(), doc_type="writer")


def test_fields_list_empty(mock_ctx):
    # Setup mock document
    doc = mock_ctx.doc
    mock_fields = MagicMock()
    mock_enum = MagicMock()
    mock_enum.hasMoreElements.return_value = False
    mock_fields.createEnumeration.return_value = mock_enum
    doc.getTextFields.return_value = mock_fields

    tool = FieldsList()
    res = tool.execute(mock_ctx)
    assert res["status"] == "ok"
    assert res["field_count"] == 0
    assert len(res["fields"]) == 0


def test_fields_list_with_items(mock_ctx):
    doc = mock_ctx.doc
    mock_fields = MagicMock()
    mock_enum = MagicMock()

    # Mock field object
    mock_field = MagicMock()
    mock_field.getPresentation.side_effect = ["Page 1", "1"]

    # Setup a mock property set info for properties extraction
    mock_prop_set_info = MagicMock()
    mock_prop_set_info.hasPropertyByName.side_effect = lambda name: name == "NumberingType"
    mock_field.getPropertySetInfo.return_value = mock_prop_set_info
    mock_field.getPropertyValue.return_value = 4

    mock_enum.hasMoreElements.side_effect = [True, False]
    mock_enum.nextElement.return_value = mock_field
    mock_fields.createEnumeration.return_value = mock_enum
    doc.getTextFields.return_value = mock_fields

    tool = FieldsList()
    res = tool.execute(mock_ctx)
    assert res["status"] == "ok"
    assert res["field_count"] == 1
    assert res["fields"][0]["presentation"] == "Page 1"
    assert res["fields"][0]["content"] == "1"
    assert res["fields"][0]["properties"]["NumberingType"] == 4


def test_fields_insert(mock_ctx):
    doc = mock_ctx.doc

    # Setup mock document controller and view cursor
    mock_controller = MagicMock()
    mock_view_cursor = MagicMock()
    mock_text = MagicMock()

    doc.getCurrentController.return_value = mock_controller
    mock_controller.getViewCursor.return_value = mock_view_cursor
    mock_view_cursor.getText.return_value = mock_text

    # Setup mock field instance
    mock_field = MagicMock()
    doc.createInstance.return_value = mock_field

    tool = FieldsInsert()
    props = {"NumberingType": 4, "IsDate": True}
    res = tool.execute(mock_ctx, field="PageNumber", properties=props, target="selection")

    assert res["status"] == "ok"
    doc.createInstance.assert_called_with("com.sun.star.text.textfield.PageNumber")
    assert mock_field.setPropertyValue.call_count == 2

    # Text insertion uses the cursor returned by resolve_target_cursor. Since the cell-safe fix,
    # the selection cursor is built in the SELECTION's own text object (rng.getText()), not the
    # body — the old body-cursor pin validated the cross-text bug.
    rng = mock_controller.getSelection().getByIndex()
    used_cursor = rng.getText().createTextCursorByRange()
    used_cursor.getText().insertTextContent.assert_called_with(used_cursor, mock_field, False)


def test_fields_delete(mock_ctx):
    doc = mock_ctx.doc
    mock_fields = MagicMock()
    mock_enum = MagicMock()

    # Mock field objects
    mock_field_1 = MagicMock()
    mock_field_2 = MagicMock()
    mock_anchor_1 = MagicMock()
    mock_anchor_2 = MagicMock()
    mock_field_1.getAnchor.return_value = mock_anchor_1
    mock_field_2.getAnchor.return_value = mock_anchor_2

    mock_enum.hasMoreElements.side_effect = [True, True, False]
    mock_enum.nextElement.side_effect = [mock_field_1, mock_field_2]
    mock_fields.createEnumeration.return_value = mock_enum
    doc.getTextFields.return_value = mock_fields

    tool = FieldsDelete()
    # Delete only the second field (ID 2)
    res = tool.execute(mock_ctx, ids=[2])

    assert res["status"] == "ok"
    assert res["deleted_count"] == 1
    mock_anchor_1.setString.assert_not_called()
    mock_anchor_2.setString.assert_called_with("")


# --- where a field lives decides whether deleting it is safe -------------------------------


def _one_field_doc(doc, field):
    fields, enum = MagicMock(), MagicMock()
    enum.hasMoreElements.side_effect = [True, False]
    enum.nextElement.return_value = field
    fields.createEnumeration.return_value = enum
    doc.getTextFields.return_value = fields


def test_fields_list_reports_where_each_field_is(mock_ctx, monkeypatch):
    """getTextFields() mixes header and footer fields in with the body's, and a page number in a
    footer is indistinguishable from one in the text without this."""
    from plugin.writer import search as search_mod

    field = MagicMock()
    field.getPresentation.side_effect = ["Page 1", "1"]
    field.getPropertySetInfo.return_value.hasPropertyByName.return_value = False
    _one_field_doc(mock_ctx.doc, field)
    monkeypatch.setattr(search_mod, "describe_match_location", lambda *a, **k: "footer (page style 'Standard')")

    res = FieldsList().execute(mock_ctx)

    assert res["fields"][0]["location"] == "footer (page style 'Standard')"


def test_fields_list_location_falls_back_when_it_cannot_be_resolved(mock_ctx, monkeypatch):
    """An unlabelable anchor must not sink the whole listing."""
    from plugin.writer import search as search_mod

    field = MagicMock()
    field.getPresentation.side_effect = ["Page 1", "1"]
    field.getPropertySetInfo.return_value.hasPropertyByName.return_value = False
    _one_field_doc(mock_ctx.doc, field)

    def boom(*_a, **_k):
        raise RuntimeError("no anchor")

    monkeypatch.setattr(search_mod, "describe_match_location", boom)

    res = FieldsList().execute(mock_ctx)

    assert res["status"] == "ok"
    assert res["fields"][0]["location"] == "unknown"


def test_fields_delete_says_what_it_removed_and_from_where(mock_ctx, monkeypatch):
    """Reported before the anchor is emptied — afterwards the two are indistinguishable."""
    from plugin.writer import search as search_mod

    field = MagicMock()
    field.getPresentation.return_value = "Page Number"
    anchor = field.getAnchor.return_value
    _one_field_doc(mock_ctx.doc, field)
    monkeypatch.setattr(search_mod, "describe_match_location", lambda *a, **k: "footer (page style 'Standard')")

    res = FieldsDelete().execute(mock_ctx, ids=[1])

    assert res["deleted_count"] == 1
    assert res["deleted"] == [{"content": "Page Number", "location": "footer (page style 'Standard')"}]
    anchor.setString.assert_called_with("")
