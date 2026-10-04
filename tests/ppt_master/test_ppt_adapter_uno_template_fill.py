from unittest.mock import MagicMock, patch
import pytest

from plugin.ppt_master.adapter.uno_template_fill import apply_fill_plan_to_doc

def test_apply_fill_plan_to_doc_actually_writes_text():
    doc = MagicMock()

    plan = {
        "slides": [
            {
                "replacements": {
                    "title": "New Title",
                    "body": "New Body"
                }
            }
        ]
    }

    shape_title = MagicMock()
    shape_body = MagicMock()
    shape_title.setString = MagicMock()
    shape_body.setString = MagicMock()

    page_mock = MagicMock()
    page_mock.getCount.return_value = 2
    page_mock.getByIndex.side_effect = [shape_title, shape_body]

    def mock_hasattr(obj, name):
        if name == "setString":
            return True
        return getattr(obj, name, None) is not None

    with patch("plugin.ppt_master.adapter.uno_template_fill.DrawBridge") as DrawBridgeMock, \
         patch("plugin.ppt_master.adapter.uno_template_fill._find_placeholder", side_effect=lambda p, role: (shape_title if role == "title" else shape_body, 0)), \
         patch("builtins.hasattr", side_effect=mock_hasattr):
        bridge_instance = DrawBridgeMock.return_value
        bridge_instance.create_slide.return_value = (page_mock, 0)

        result = apply_fill_plan_to_doc(doc, plan)

        assert result["status"] == "ok"
        assert result["fills_recorded"] == 2
        shape_title.setString.assert_called_with("New Title")
        shape_body.setString.assert_called_with("New Body")

def test_apply_fill_plan_to_doc_fails_if_no_set_string():
    doc = MagicMock()

    plan = {
        "slides": [
            {
                "replacements": {
                    "title": "New Title"
                }
            }
        ]
    }

    shape_title = MagicMock()
    # Does not have setString

    page_mock = MagicMock()
    page_mock.getCount.return_value = 1
    page_mock.getByIndex.return_value = shape_title

    def mock_hasattr(obj, name):
        if name == "setString":
            return False
        return getattr(obj, name, None) is not None

    with patch("plugin.ppt_master.adapter.uno_template_fill.DrawBridge") as DrawBridgeMock, \
         patch("plugin.ppt_master.adapter.uno_template_fill._find_placeholder", return_value=(shape_title, 0)), \
         patch("builtins.hasattr", side_effect=mock_hasattr):
        bridge_instance = DrawBridgeMock.return_value
        bridge_instance.create_slide.return_value = (page_mock, 0)

        result = apply_fill_plan_to_doc(doc, plan)

        assert result["status"] == "error"
        assert "unsupported" in result["message"]
