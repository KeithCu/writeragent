from unittest.mock import MagicMock
from plugin.ppt_master.adapter.uno_template_fill import apply_fill_plan_to_doc

from unittest.mock import patch

@patch('plugin.ppt_master.adapter.uno_template_fill._find_placeholder')
@patch('plugin.ppt_master.adapter.uno_template_fill.DrawBridge')
def test_apply_fill_plan_to_doc_writes_text(bridge_mock, find_placeholder_mock):
    doc = MagicMock()
    page = MagicMock()

    bridge_instance = bridge_mock.return_value
    bridge_instance.get_slide_for_tool.return_value = page

    shape_mock = MagicMock()
    shape_mock.setString = MagicMock()

    # We mock _find_placeholder to always return our shape_mock
    find_placeholder_mock.return_value = (shape_mock, 0)

    plan = {
        "slides": [
            {
                "replacements": {
                    "title": "My Title",
                    "body": "My Body"
                }
            }
        ]
    }

    result = apply_fill_plan_to_doc(doc, plan)

    assert result["status"] == "ok"
    assert result["slides_created"] == 1
    assert result["fills_recorded"] == 2
    assert shape_mock.setString.call_count == 2
    shape_mock.setString.assert_any_call("My Title")
    shape_mock.setString.assert_any_call("My Body")

@patch('plugin.ppt_master.adapter.uno_template_fill._find_placeholder')
@patch('plugin.ppt_master.adapter.uno_template_fill.DrawBridge')
def test_apply_fill_plan_to_doc_handles_missing_placeholder(bridge_mock, find_placeholder_mock):
    doc = MagicMock()
    page = MagicMock()

    bridge_instance = bridge_mock.return_value
    bridge_instance.get_slide_for_tool.return_value = page

    shape_mock = MagicMock()
    shape_mock.setString = MagicMock()

    def side_effect(p, role):
        if role == "title":
            return shape_mock, 0
        return None, None

    find_placeholder_mock.side_effect = side_effect

    plan = {
        "slides": [
            {
                "replacements": {
                    "title": "My Title",
                    "missing_role": "Will not be written"
                }
            }
        ]
    }

    result = apply_fill_plan_to_doc(doc, plan)

    assert result["status"] == "ok"
    assert result["fills_recorded"] == 1
    assert shape_mock.setString.call_count == 1
    shape_mock.setString.assert_called_with("My Title")
