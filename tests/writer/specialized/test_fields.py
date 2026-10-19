import pytest
import sys
from types import SimpleNamespace
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
    doc = MagicMock()
    mgr = MagicMock()
    mgr.isLocked.return_value = False
    entered = {}
    mgr.enterUndoContext.side_effect = lambda t: entered.__setitem__("title", t)
    mgr.getAllUndoActionTitles.side_effect = lambda: (entered.get("title"),)
    doc.getUndoManager.return_value = mgr
    return TestingFactory.create_context(doc=doc, doc_type="writer")


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
    mock_prop_set_info.getProperties.return_value = [SimpleNamespace(Name="NumberingType")]
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


def test_fields_insert_reports_the_failing_property(mock_ctx):
    doc = mock_ctx.doc
    mock_field = MagicMock()
    mock_field.setPropertyValue.side_effect = [None, ValueError("no such property")]
    doc.createInstance.return_value = mock_field

    res = FieldsInsert().execute(mock_ctx, field="PageNumber", properties={"NumberingType": 4, "Bogus": 1}, target="selection")

    assert res["status"] == "error"
    assert res["message"] == "Failed to set property 'Bogus' to '1': no such property"
    mock_controller = doc.getCurrentController.return_value
    mock_controller.getSelection().getByIndex().getText().createTextCursorByRange().getText().insertTextContent.assert_not_called()


def test_fields_insert_property_disposal_propagates(mock_ctx):
    class DisposedException(Exception):
        pass

    mock_field = MagicMock()
    mock_field.setPropertyValue.side_effect = DisposedException("closed")
    mock_ctx.doc.createInstance.return_value = mock_field

    with pytest.raises(DisposedException):
        FieldsInsert().execute(mock_ctx, field="PageNumber", properties={"NumberingType": 4}, target="selection")


def test_fields_delete(mock_ctx):
    doc = mock_ctx.doc
    mock_fields = MagicMock()
    mock_enum = MagicMock()

    # Mock field objects
    mock_field_1 = MagicMock()
    mock_field_2 = MagicMock()
    mock_field_1.supportsService.return_value = False
    mock_field_2.supportsService.return_value = False
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
    assert res["requested_count"] == 1
    assert res["missing_ids"] == []
    mock_anchor_1.setString.assert_not_called()
    mock_anchor_2.setString.assert_called_once_with("")


def test_fields_delete_validation_and_missing_ids(mock_ctx):
    tool = FieldsDelete()
    # Non-list
    res = tool.execute(mock_ctx, ids="1")
    assert res["status"] == "error"
    assert "must be a list of integers" in res["message"]

    # Boolean rejected
    res = tool.execute(mock_ctx, ids=[True])
    assert res["status"] == "error"
    assert "no booleans" in res["message"]

    # Empty list
    res = tool.execute(mock_ctx, ids=[])
    assert res["status"] == "error"

    # Missing ID returns error mentioning comment_delete
    doc = mock_ctx.doc
    mock_fields = MagicMock()
    mock_enum = MagicMock()
    mock_enum.hasMoreElements.return_value = False
    mock_fields.createEnumeration.return_value = mock_enum
    doc.getTextFields.return_value = mock_fields

    res = tool.execute(mock_ctx, ids=[1, 1, 99])
    assert res["status"] == "error"
    assert res["code"] == "FIELD_NOT_FOUND"
    assert res["deleted_count"] == 0
    assert res["requested_count"] == 2
    assert res["missing_ids"] == [1, 99]
    assert "comment_delete" in res["message"]


def test_fields_delete_partial_missing_ids(mock_ctx):
    doc = mock_ctx.doc
    mock_fields = MagicMock()
    mock_enum = MagicMock()

    field1 = MagicMock()
    field1.supportsService.return_value = False
    field1.getAnchor.return_value = MagicMock()

    mock_enum.hasMoreElements.side_effect = [True, False]
    mock_enum.nextElement.return_value = field1
    mock_fields.createEnumeration.return_value = mock_enum
    doc.getTextFields.return_value = mock_fields

    tool = FieldsDelete()
    res = tool.execute(mock_ctx, ids=[1, 42])
    assert res["status"] == "ok"
    assert res["deleted_count"] == 1
    assert res["requested_count"] == 2
    assert res["missing_ids"] == [42]
    field1.getAnchor().setString.assert_called_once_with("")


def test_fields_skip_annotations_in_list_and_delete(mock_ctx):
    doc = mock_ctx.doc
    mock_fields = MagicMock()
    mock_enum = MagicMock()

    ann_field = MagicMock()
    ann_field.supportsService.side_effect = lambda s: s == "com.sun.star.text.textfield.Annotation"

    page_field = MagicMock()
    page_field.supportsService.return_value = False
    page_field.getPresentation.side_effect = ["Page 1", "1"]
    page_field.getPropertySetInfo.return_value.hasPropertyByName.return_value = False

    mock_enum.hasMoreElements.side_effect = [True, True, False]
    mock_enum.nextElement.side_effect = [ann_field, page_field]
    mock_fields.createEnumeration.return_value = mock_enum
    doc.getTextFields.return_value = mock_fields

    # fields_list skips annotation; page_field gets ID 1
    res_list = FieldsList().execute(mock_ctx)
    assert res_list["status"] == "ok"
    assert res_list["field_count"] == 1
    assert res_list["fields"][0]["id"] == 1

    # fields_delete skips annotation; ID 1 deletes page_field
    mock_enum.hasMoreElements.side_effect = [True, True, False]
    mock_enum.nextElement.side_effect = [ann_field, page_field]
    res_del = FieldsDelete().execute(mock_ctx, ids=[1])
    assert res_del["status"] == "ok"
    assert res_del["deleted_count"] == 1
    page_field.getAnchor().setString.assert_called_once_with("")
    ann_field.getAnchor().setString.assert_not_called()


def test_fields_insert_search_rolls_back_failed_insert(mock_ctx, monkeypatch):
    doc = mock_ctx.doc
    mgr = MagicMock()
    mgr.isLocked.return_value = False
    entered = {}
    mgr.enterUndoContext.side_effect = lambda t: entered.__setitem__("title", t)
    mgr.getAllUndoActionTitles.side_effect = lambda: (entered.get("title"),)
    doc.getUndoManager.return_value = mgr

    mock_cursor = MagicMock()
    mock_text = MagicMock()
    mock_cursor.getText.return_value = mock_text
    mock_text.insertTextContent.side_effect = RuntimeError("insert failed")

    monkeypatch.setattr("plugin.writer.specialized.fields.resolve_target_cursor", lambda ctx, t, o: mock_cursor)
    mock_field = MagicMock()
    doc.createInstance.return_value = mock_field

    res = FieldsInsert().execute(mock_ctx, field="PageNumber", target="search", old_content="TARGET")
    assert res["status"] == "error"
    assert "rolled back" in res["message"]
    assert "insert failed" in res["message"]
    mock_cursor.setString.assert_called_once_with("")
    mgr.undo.assert_called_once()


def test_fields_insert_search_locked_undo_does_not_delete(mock_ctx, monkeypatch):
    doc = mock_ctx.doc
    mgr = MagicMock()
    mgr.isLocked.return_value = True
    doc.getUndoManager.return_value = mgr

    mock_cursor = MagicMock()
    monkeypatch.setattr("plugin.writer.specialized.fields.resolve_target_cursor", lambda ctx, t, o: mock_cursor)
    mock_field = MagicMock()
    doc.createInstance.return_value = mock_field

    res = FieldsInsert().execute(mock_ctx, field="PageNumber", target="search", old_content="TARGET")
    assert res["status"] == "error"
    assert res["code"] == "UNDO_UNAVAILABLE"
    mock_cursor.setString.assert_not_called()
    mgr.enterUndoContext.assert_not_called()


# --- where a field lives decides whether deleting it is safe -------------------------------


def _one_field_doc(doc, field):
    fields = MagicMock()

    def make_enum():
        enum = MagicMock()
        enum.hasMoreElements.side_effect = [True, False]
        enum.nextElement.return_value = field
        return enum

    fields.createEnumeration.side_effect = make_enum
    doc.getTextFields.return_value = fields



def test_fields_list_reports_where_each_field_is(mock_ctx, monkeypatch):
    """getTextFields() mixes header and footer fields in with the body's, and a page number in a
    footer is indistinguishable from one in the text without this."""
    from plugin.writer import search as search_mod

    field = MagicMock()
    field.supportsService.return_value = False
    field.getPresentation.side_effect = ["Page 1", "1"]
    field.getPropertySetInfo.return_value.getProperties.return_value = ()
    _one_field_doc(mock_ctx.doc, field)
    monkeypatch.setattr(search_mod, "describe_match_location", lambda *a, **k: "footer (page style 'Standard')")

    res = FieldsList().execute(mock_ctx)

    assert res["fields"][0]["location"] == "footer (page style 'Standard')"


def test_fields_list_location_falls_back_when_it_cannot_be_resolved(mock_ctx, monkeypatch):
    """An unlabelable anchor must not sink the whole listing."""
    from plugin.writer import search as search_mod

    field = MagicMock()
    field.supportsService.return_value = False
    field.getPresentation.side_effect = ["Page 1", "1"]
    field.getPropertySetInfo.return_value.getProperties.return_value = ()
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
    field.supportsService.return_value = False
    field.getPresentation.return_value = "Page Number"
    anchor = field.getAnchor.return_value
    _one_field_doc(mock_ctx.doc, field)
    monkeypatch.setattr(search_mod, "describe_match_location", lambda *a, **k: "footer (page style 'Standard')")

    res = FieldsDelete().execute(mock_ctx, ids=[1])

    assert res["deleted_count"] == 1
    assert res["deleted"] == [{"content": "Page Number", "location": "footer (page style 'Standard')"}]
    anchor.setString.assert_called_with("")


def test_fields_disposal_is_document_disposed(mock_ctx):
    from plugin.framework.errors import DocumentDisposedError

    mock_ctx.doc.getTextFields.side_effect = DocumentDisposedError("gone")
    res = FieldsList().execute_safe(mock_ctx)
    assert res["status"] == "error"
    assert res["code"] == "DOCUMENT_DISPOSED"


def test_fields_delete_with_matching_snapshot_succeeds(mock_ctx):
    field = MagicMock()
    field.supportsService.return_value = False
    field.getPresentation.return_value = "Page 1"
    field.getPropertySetInfo.return_value.hasPropertyByName.return_value = False
    anchor = field.getAnchor.return_value
    _one_field_doc(mock_ctx.doc, field)

    list_res = FieldsList().execute(mock_ctx)
    assert list_res["status"] == "ok"
    snapshot = list_res["snapshot"]

    del_res = FieldsDelete().execute(mock_ctx, ids=[1], snapshot=snapshot)
    assert del_res["status"] == "ok"
    assert del_res["deleted_count"] == 1
    anchor.setString.assert_called_with("")


def test_fields_delete_with_stale_snapshot_fails(mock_ctx):
    field1 = MagicMock()
    field1.supportsService.return_value = False
    field1.getPresentation.return_value = "Page 1"
    field1.getPropertySetInfo.return_value.hasPropertyByName.return_value = False
    anchor1 = field1.getAnchor.return_value

    _one_field_doc(mock_ctx.doc, field1)
    list_res = FieldsList().execute(mock_ctx)
    assert list_res["status"] == "ok"
    snapshot = list_res["snapshot"]

    # Simulate document modification by having 2 fields now
    field2 = MagicMock()
    field2.supportsService.return_value = False
    field2.getPresentation.return_value = "Title"
    field2.getPropertySetInfo.return_value.hasPropertyByName.return_value = False

    fields = MagicMock()

    def make_two_enum():
        enum = MagicMock()
        enum.hasMoreElements.side_effect = [True, True, False]
        enum.nextElement.side_effect = [field1, field2]
        return enum

    fields.createEnumeration.side_effect = make_two_enum
    mock_ctx.doc.getTextFields.return_value = fields

    del_res = FieldsDelete().execute(mock_ctx, ids=[1], snapshot=snapshot)
    assert del_res["status"] == "error"
    assert del_res["code"] == "FIELD_LIST_STALE"
    assert del_res["deleted_count"] == 0
    anchor1.setString.assert_not_called()


def test_fields_delete_without_snapshot_succeeds(mock_ctx):
    field = MagicMock()
    field.supportsService.return_value = False
    field.getPresentation.return_value = "Page 1"
    field.getPropertySetInfo.return_value.hasPropertyByName.return_value = False
    anchor = field.getAnchor.return_value
    _one_field_doc(mock_ctx.doc, field)

    del_res = FieldsDelete().execute(mock_ctx, ids=[1])
    assert del_res["status"] == "ok"
    assert del_res["deleted_count"] == 1
    anchor.setString.assert_called_with("")


def test_fields_list_snapshot_equals_delete_computed_hash(mock_ctx):
    field1 = MagicMock()
    field1.supportsService.return_value = False
    field1.getPresentation.return_value = "Page 1"
    field1.getPropertySetInfo.return_value.getProperties.return_value = ()

    field2 = MagicMock()
    field2.supportsService.return_value = False
    field2.getPresentation.return_value = "Page 2"
    field2.getPropertySetInfo.return_value.getProperties.return_value = ()

    fields = MagicMock()

    def make_enum():
        enum = MagicMock()
        enum.hasMoreElements.side_effect = [True, True, False]
        enum.nextElement.side_effect = [field1, field2]
        return enum

    fields.createEnumeration.side_effect = make_enum
    mock_ctx.doc.getTextFields.return_value = fields

    list_res = FieldsList().execute(mock_ctx)
    snapshot = list_res["snapshot"]

    # Passing that exact snapshot succeeds because delete computes the same hash
    del_res = FieldsDelete().execute(mock_ctx, ids=[1], snapshot=snapshot)
    assert del_res["status"] == "ok"
    assert del_res["deleted_count"] == 1


def test_fields_delete_rollback_when_setString_raises(mock_ctx):
    field1 = MagicMock()
    field1.supportsService.return_value = False
    field1.getPresentation.return_value = "F1"
    field1.getPropertySetInfo.return_value.hasPropertyByName.return_value = False
    anchor1 = MagicMock()
    field1.getAnchor.return_value = anchor1

    field2 = MagicMock()
    field2.supportsService.return_value = False
    field2.getPresentation.return_value = "F2"
    field2.getPropertySetInfo.return_value.hasPropertyByName.return_value = False
    anchor2 = MagicMock()
    anchor2.setString.side_effect = RuntimeError("anchor clear failed")
    field2.getAnchor.return_value = anchor2

    field3 = MagicMock()
    field3.supportsService.return_value = False
    field3.getPresentation.return_value = "F3"
    field3.getPropertySetInfo.return_value.hasPropertyByName.return_value = False
    anchor3 = MagicMock()
    field3.getAnchor.return_value = anchor3

    fields = MagicMock()

    def make_enum():
        enum = MagicMock()
        enum.hasMoreElements.side_effect = [True, True, True, False]
        enum.nextElement.side_effect = [field1, field2, field3]
        return enum

    fields.createEnumeration.side_effect = make_enum
    mock_ctx.doc.getTextFields.return_value = fields

    mgr = mock_ctx.doc.getUndoManager()
    del_res = FieldsDelete().execute(mock_ctx, ids=[1, 2, 3])
    assert del_res["status"] == "error"
    assert del_res.get("details", {}).get("deleted_count") == 0
    assert "anchor clear failed" in del_res["message"]
    mgr.undo.assert_called_once()


def test_fields_delete_locked_undo_does_not_delete(mock_ctx):
    field = MagicMock()
    field.supportsService.return_value = False
    field.getPresentation.return_value = "Page 1"
    field.getPropertySetInfo.return_value.hasPropertyByName.return_value = False
    anchor = field.getAnchor.return_value
    _one_field_doc(mock_ctx.doc, field)

    mock_ctx.doc.getUndoManager().isLocked.return_value = True

    del_res = FieldsDelete().execute(mock_ctx, ids=[1])
    assert del_res["status"] == "error"
    assert del_res["code"] == "UNDO_UNAVAILABLE"
    anchor.setString.assert_not_called()
