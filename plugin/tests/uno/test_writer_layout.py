import pytest
from typing import Any
from unittest.mock import MagicMock

# Mock UNO imports required for testing layout tools outside of LibreOffice environment
import sys
import types
sys.modules['uno'] = MagicMock()
sys.modules['unohelper'] = MagicMock()
sys.modules['com'] = MagicMock()

com_sun_star = types.ModuleType('com.sun.star')
sys.modules['com.sun.star'] = com_sun_star
com_sun_star_text = types.ModuleType('com.sun.star.text')
sys.modules['com.sun.star.text'] = com_sun_star_text
com_sun_star_style = types.ModuleType('com.sun.star.style')
sys.modules['com.sun.star.style'] = com_sun_star_style

# Mock the BreakType module itself
com_sun_star_style_breaktype = types.ModuleType('com.sun.star.style.BreakType')
sys.modules['com.sun.star.style.BreakType'] = com_sun_star_style_breaktype
setattr(com_sun_star_style_breaktype, "PAGE_BEFORE", 4)


from plugin.modules.writer.layout import (
    GetPageStyleProperties,
    SetPageStyleProperties,
    GetHeaderFooterText,
    SetHeaderFooterText,
    GetPageColumns,
    SetPageColumns,
    InsertPageBreak
)

class MockToolContext:
    def __init__(self, doc):
        self.doc = doc
        self.services = MagicMock()

def test_get_page_style_properties():
    # Setup mock doc
    doc = MagicMock()
    families = MagicMock()
    page_styles = MagicMock()
    style = MagicMock()

    doc.getStyleFamilies.return_value = families
    families.getByName.return_value = page_styles
    page_styles.hasByName.return_value = True
    page_styles.getByName.return_value = style

    def get_prop(name):
        props = {
            "Width": 21000,
            "Height": 29700,
            "IsLandscape": False,
            "LeftMargin": 2000,
            "RightMargin": 2000,
            "TopMargin": 2000,
            "BottomMargin": 2000,
            "GutterMargin": 0,
            "HeaderIsOn": True,
            "FooterIsOn": False,
            "HeaderIsShared": True,
            "FooterIsShared": True,
            "HeaderHeight": 500,
            "FooterHeight": 500,
            "HeaderBodyDistance": 500,
            "FooterBodyDistance": 500,
            "BackColor": 16777215,
            "BackTransparent": True,
            "NumberingType": 4,
            "FootnoteHeight": 0,
            "RegisterParagraphStyle": "",
            "PageStyleLayout": MagicMock(value=0)
        }
        return props[name]
    style.getPropertyValue.side_effect = get_prop

    ctx = MockToolContext(doc)
    tool = GetPageStyleProperties()
    res = tool.execute(ctx, style_name="Standard")

    assert res["status"] == "ok"
    assert res["properties"]["width_mm"] == 210.0
    assert res["properties"]["height_mm"] == 297.0
    assert res["properties"]["header_is_on"] is True
    assert res["properties"]["footer_is_on"] is False

def test_set_page_style_properties():
    doc = MagicMock()
    families = MagicMock()
    page_styles = MagicMock()
    style = MagicMock()

    doc.getStyleFamilies.return_value = families
    families.getByName.return_value = page_styles
    page_styles.hasByName.return_value = True
    page_styles.getByName.return_value = style

    ctx = MockToolContext(doc)
    tool = SetPageStyleProperties()
    res = tool.execute(ctx, style_name="Standard", width_mm=300, is_landscape=True, header_is_on=False)

    assert res["status"] == "ok"
    assert "width" in res["updated"]
    assert "is_landscape" in res["updated"]

    style.setPropertyValue.assert_any_call("Width", 30000)
    style.setPropertyValue.assert_any_call("IsLandscape", True)
    style.setPropertyValue.assert_any_call("HeaderIsOn", False)

def test_set_header_footer_text():
    doc = MagicMock()
    families = MagicMock()
    page_styles = MagicMock()
    style = MagicMock()

    doc.getStyleFamilies.return_value = families
    families.getByName.return_value = page_styles
    page_styles.hasByName.return_value = True
    page_styles.getByName.return_value = style

    header_text_obj = MagicMock()
    style.getPropertyValue.return_value = header_text_obj

    ctx = MockToolContext(doc)
    tool = SetHeaderFooterText()
    res = tool.execute(ctx, style_name="Standard", region="header", content="My Header Content")

    assert res["status"] == "ok"
    assert res["region"] == "header"

    style.setPropertyValue.assert_called_with("HeaderIsOn", True)
    header_text_obj.setString.assert_called_with("My Header Content")

def test_set_page_columns():
    doc = MagicMock()
    families = MagicMock()
    page_styles = MagicMock()
    style = MagicMock()
    text_columns = MagicMock()

    doc.getStyleFamilies.return_value = families
    families.getByName.return_value = page_styles
    page_styles.hasByName.return_value = True
    page_styles.getByName.return_value = style
    style.getPropertyValue.return_value = text_columns

    col1 = MagicMock()
    col2 = MagicMock()
    text_columns.getColumns.return_value = (col1, col2)

    ctx = MockToolContext(doc)
    tool = SetPageColumns()
    res = tool.execute(ctx, style_name="Standard", column_count=2, spacing_mm=5)

    assert res["status"] == "ok"
    text_columns.setColumnCount.assert_called_with(2)

    # Check spacing
    assert col1.RightMargin == 250
    assert col2.LeftMargin == 250
    text_columns.setColumns.assert_called_with((col1, col2))
    style.setPropertyValue.assert_called_with("TextColumns", text_columns)

def test_insert_page_break():
    doc = MagicMock()
    controller = MagicMock()
    view_cursor = MagicMock()
    text_obj = MagicMock()
    text_cursor = MagicMock()

    doc.getCurrentController.return_value = controller
    controller.getViewCursor.return_value = view_cursor
    view_cursor.getText.return_value = text_obj
    text_obj.createTextCursorByRange.return_value = text_cursor

    ctx = MockToolContext(doc)
    tool = InsertPageBreak()
    res = tool.execute(ctx)

    assert res["status"] == "ok"
    text_cursor.setPropertyValue.assert_called_with("BreakType", 4) # PAGE_BEFORE
    text_obj.insertControlCharacter.assert_called_with(text_cursor, 0, False)
