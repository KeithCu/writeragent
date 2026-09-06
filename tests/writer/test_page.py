from unittest.mock import MagicMock

from plugin.tests.testing_utils import TestingFactory, setup_uno_mocks

setup_uno_mocks()

# Set up BreakType PAGE_BEFORE constant explicitly if needed for the test
import sys

setattr(sys.modules["com.sun.star.style.BreakType"], "PAGE_BEFORE", 4)

from plugin.writer.page import (
    PageGetHeaderFooterText,
    PageGetStyleProperties,
    PageSetStyleProperties,
    PageSetHeaderFooterText,
    PageSetColumns,
    PageInsertBreak,
)


def test_get_page_style_properties():
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
            "PageStyleLayout": MagicMock(value=0),
        }
        return props[name]

    style.getPropertyValue.side_effect = get_prop

    ctx = TestingFactory.create_context(doc=doc, doc_type="writer")
    tool = PageGetStyleProperties()
    res = tool.execute(ctx, style="Standard")

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

    ctx = TestingFactory.create_context(doc=doc, doc_type="writer")
    tool = PageSetStyleProperties()
    res = tool.execute(ctx, style="Standard", width_mm=300, is_landscape=True, header_is_on=False)

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

    ctx = TestingFactory.create_context(doc=doc, doc_type="writer")
    tool = PageSetHeaderFooterText()
    res = tool.execute(
        ctx,
        style="Standard",
        region="header",
        content="My Header Content",
        auto_height=True,
    )

    assert res["status"] == "ok"
    assert res["region"] == "header"
    assert res["auto_height"] is True

    style.setPropertyValue.assert_any_call("HeaderIsOn", True)
    style.setPropertyValue.assert_any_call("HeaderIsDynamicHeight", True)
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

    ctx = TestingFactory.create_context(doc=doc, doc_type="writer")
    tool = PageSetColumns()
    res = tool.execute(ctx, style="Standard", column_count=2, spacing_mm=5)

    assert res["status"] == "ok"
    text_columns.setColumnCount.assert_called_with(2)

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

    ctx = TestingFactory.create_context(doc=doc, doc_type="writer")
    tool = PageInsertBreak()
    res = tool.execute(ctx)

    assert res["status"] == "ok"
    text_cursor.setPropertyValue.assert_called_with("BreakType", 4)  # PAGE_BEFORE
    text_obj.insertControlCharacter.assert_called_with(text_cursor, 0, False)


def test_page_tools_shortened_style_param():
    doc = MagicMock()
    families = MagicMock()
    page_styles = MagicMock()
    style = MagicMock()
    doc.getStyleFamilies.return_value = families
    families.getByName.return_value = page_styles
    page_styles.hasByName.return_value = True
    page_styles.getByName.return_value = style

    ctx = TestingFactory.create_context(doc=doc, doc_type="writer")
    res = PageGetStyleProperties().execute(ctx, style="Standard")
    assert res["status"] == "ok"
    assert "properties" in res

    res_set = PageSetStyleProperties().execute(ctx, style="Standard", width_mm=210)
    assert res_set["status"] == "ok"


# --- header/footer: what plain text cannot carry ------------------------------------------


def _enum_of(items):
    """UNO-style enumeration over *items* (hasMoreElements/nextElement)."""
    e = MagicMock()
    rest = list(items)
    e.hasMoreElements.side_effect = lambda: True if rest else False
    e.nextElement.side_effect = lambda: rest.pop(0)
    return e


def _portion(kind, field=None):
    p = MagicMock()
    p.getPropertyValue.side_effect = lambda n: kind if n == "TextPortionType" else field
    return p


def _paragraph(portions):
    para = MagicMock()
    para.createEnumeration.side_effect = lambda: _enum_of(portions)
    return para


def _page_style_doc(text_obj, shapes=()):
    doc = MagicMock()
    families, page_styles, style = MagicMock(), MagicMock(), MagicMock()
    doc.getStyleFamilies.return_value = families
    families.getByName.return_value = page_styles
    page_styles.hasByName.return_value = True
    page_styles.getByName.return_value = style
    style.getPropertyValue.side_effect = lambda n: True if n.endswith("IsOn") else text_obj
    draw_page = MagicMock()
    draw_page.getCount.return_value = len(shapes)
    draw_page.getByIndex.side_effect = lambda i: shapes[i]
    doc.getDrawPage.return_value = draw_page
    return doc, style


def _logo_anchored_in(text_obj, name="TIMBRE"):
    shape = MagicMock()
    shape.getName.return_value = name
    shape.getAnchor.return_value.getText.return_value = text_obj
    return shape


def _page_number_field():
    field = MagicMock()
    field.getPresentation.side_effect = lambda cmd: "Page Number" if cmd is True else "1"
    return field


def test_get_header_footer_reports_the_logo_and_field_text_alone_hides():
    """getString() renders a logo as an empty line and a page-number field as its digits, so a
    caller reading only `content` cannot see what an overwrite would destroy."""
    text_obj = MagicMock()
    text_obj.getString.return_value = "\nESCRITORIO ZOLET"
    text_obj.createEnumeration.side_effect = lambda: _enum_of([
        _paragraph([_portion("Frame")]),
        _paragraph([_portion("Text"), _portion("TextField", _page_number_field())]),
    ])
    doc, _style = _page_style_doc(text_obj, shapes=[_logo_anchored_in(text_obj)])

    res = PageGetHeaderFooterText().execute(
        TestingFactory.create_context(doc=doc, doc_type="writer"), style="Standard", region="header")

    assert res["status"] == "ok"
    assert res["images"] == ["TIMBRE"]
    assert res["fields"] == [{"presentation": "1", "content": "Page Number"}]
    assert res["paragraph_count"] == 2
    assert "apply_document_content" in res["warning"]


def test_set_header_footer_refuses_to_silently_delete_a_logo():
    """The old behaviour returned ok and destroyed the letterhead with no way back."""
    text_obj = MagicMock()
    text_obj.createEnumeration.side_effect = lambda: _enum_of([_paragraph([_portion("Frame")])])
    doc, _style = _page_style_doc(text_obj, shapes=[_logo_anchored_in(text_obj)])

    res = PageSetHeaderFooterText().execute(
        TestingFactory.create_context(doc=doc, doc_type="writer"),
        style="Standard", region="header", content="ESCRITORIO ZOLET")

    assert res["status"] == "error"
    assert "TIMBRE" in res["message"]
    assert "apply_document_content" in res["message"]
    text_obj.setString.assert_not_called()


def test_set_header_footer_refusal_leaves_the_document_untouched():
    """A refusal must not have already enabled the region or resized it on the way in."""
    text_obj = MagicMock()
    text_obj.createEnumeration.side_effect = lambda: _enum_of([_paragraph([_portion("Frame")])])
    doc, style = _page_style_doc(text_obj, shapes=[_logo_anchored_in(text_obj)])

    res = PageSetHeaderFooterText().execute(
        TestingFactory.create_context(doc=doc, doc_type="writer"),
        style="Standard", region="header", content="ESCRITORIO ZOLET", auto_height=True)

    assert res["status"] == "error"
    style.setPropertyValue.assert_not_called()


def test_set_header_footer_skips_the_scan_when_the_region_is_off():
    """Nothing to destroy in a region that is not on yet — enable it and write."""
    text_obj = MagicMock()
    text_obj.createEnumeration.side_effect = lambda: _enum_of([])
    doc, style = _page_style_doc(text_obj)
    style.getPropertyValue.side_effect = lambda n: False if n.endswith("IsOn") else text_obj

    res = PageSetHeaderFooterText().execute(
        TestingFactory.create_context(doc=doc, doc_type="writer"),
        style="Standard", region="footer", content="Rua Exemplo, 123")

    assert res["status"] == "ok"
    style.setPropertyValue.assert_any_call("FooterIsOn", True)
    text_obj.setString.assert_called_with("Rua Exemplo, 123")


def test_set_header_footer_force_deletes_and_says_what_it_deleted():
    text_obj = MagicMock()
    text_obj.createEnumeration.side_effect = lambda: _enum_of([_paragraph([_portion("Frame")])])
    doc, _style = _page_style_doc(text_obj, shapes=[_logo_anchored_in(text_obj)])

    res = PageSetHeaderFooterText().execute(
        TestingFactory.create_context(doc=doc, doc_type="writer"),
        style="Standard", region="header", content="ESCRITORIO ZOLET", force=True)

    assert res["status"] == "ok"
    assert res["deleted"]["images"] == ["TIMBRE"]
    text_obj.setString.assert_called_with("ESCRITORIO ZOLET")


def test_set_header_footer_plain_text_still_goes_straight_through():
    """No images, no fields -> unchanged behaviour, no new friction."""
    text_obj = MagicMock()
    text_obj.createEnumeration.side_effect = lambda: _enum_of([_paragraph([_portion("Text")])])
    doc, _style = _page_style_doc(text_obj)

    res = PageSetHeaderFooterText().execute(
        TestingFactory.create_context(doc=doc, doc_type="writer"),
        style="Standard", region="footer", content="Rua Exemplo, 123")

    assert res["status"] == "ok"
    assert "deleted" not in res
    text_obj.setString.assert_called_with("Rua Exemplo, 123")


def test_set_header_footer_reports_paragraphs_it_dropped():
    """setString collapses the region to the content given; say so instead of losing a line quietly."""
    text_obj = MagicMock()
    text_obj.createEnumeration.side_effect = lambda: _enum_of([
        _paragraph([_portion("Text")]), _paragraph([_portion("Text")]), _paragraph([_portion("Text")])])
    doc, _style = _page_style_doc(text_obj)

    res = PageSetHeaderFooterText().execute(
        TestingFactory.create_context(doc=doc, doc_type="writer"),
        style="Standard", region="footer", content="uma linha so")

    assert res["paragraphs_dropped"] == 2


def test_first_page_region_targets_its_own_text_object():
    """A 'different first page' letterhead lives in HeaderTextFirst; the shared HeaderText never
    reaches it."""
    from plugin.writer.page import _REGION_PROPS

    assert _REGION_PROPS["header_first"] == ("HeaderIsOn", "HeaderTextFirst")
    assert _REGION_PROPS["footer_first"] == ("FooterIsOn", "FooterTextFirst")
    assert _REGION_PROPS["header_left"] == ("HeaderIsOn", "HeaderTextLeft")


def test_first_page_region_uses_the_header_height_properties():
    """_height_props must key off header/footer, not the exact region name."""
    from plugin.writer.page import _height_props

    assert _height_props("header_first")[0] == "HeaderIsDynamicHeight"
    assert _height_props("footer_left")[0] == "FooterIsDynamicHeight"


def test_set_page_style_properties_writes_first_is_shared():
    doc = MagicMock()
    families, page_styles, style = MagicMock(), MagicMock(), MagicMock()
    doc.getStyleFamilies.return_value = families
    families.getByName.return_value = page_styles
    page_styles.hasByName.return_value = True
    page_styles.getByName.return_value = style

    res = PageSetStyleProperties().execute(
        TestingFactory.create_context(doc=doc, doc_type="writer"),
        style="Standard", first_is_shared=False)

    assert res["status"] == "ok"
    assert "first_is_shared" in res["updated"]
    style.setPropertyValue.assert_any_call("FirstIsShared", False)
