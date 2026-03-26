import pytest
import sys
from unittest.mock import MagicMock

# Ensure UNO is mocked if running outside LibreOffice
try:
    import uno
except ImportError:
    sys.modules["uno"] = MagicMock()
    sys.modules["unohelper"] = MagicMock()
    com_mock = MagicMock()
    sys.modules["com"] = com_mock
    sys.modules["com.sun.star.text"] = MagicMock()

from plugin.modules.writer.fields import FieldsInsert, FieldsList, FieldsDelete


@pytest.fixture
def mock_ctx():
    # Provide an ad-hoc ToolContext-like object
    class DummyContext:
        def __init__(self):
            self.doc = MagicMock()
            self.doc_type = "writer"
            self.services = {}
            self.ctx = MagicMock()

        def get(self, key, default=None):
            return self.services.get(key, default)
    return DummyContext()


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
    mock_prop = MagicMock()
    mock_prop.Name = "NumberingType"
    mock_prop_set_info.getProperties.return_value = [mock_prop]
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
    res = tool.execute(mock_ctx, field_type="PageNumber", properties=props, target="selection")

    assert res["status"] == "ok"
    doc.createInstance.assert_called_with("com.sun.star.text.textfield.PageNumber")
    assert mock_field.setPropertyValue.call_count == 2

    # Text insertion uses the cursor returned by resolve_target_cursor (doc.getText().createTextCursor)
    mock_cursor = doc.getText().createTextCursor()
    mock_cursor.getText().insertTextContent.assert_called_with(mock_cursor, mock_field, False)


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
