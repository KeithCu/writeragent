import pytest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from plugin.tests.testing_utils import TestingFactory


# Set up BreakType PAGE_BEFORE constant explicitly if needed for the test
import sys

setattr(sys.modules["com.sun.star.style.BreakType"], "PAGE_BEFORE", 4)

from plugin.writer.page import (
    PageGetHeaderFooterText,
    PageGetStyleProperties,
    PageReplaceHeaderFooterText,
    PageSetStyleProperties,
    PageSetHeaderFooterText,
    PageSetColumns,
    PageInsertBreak,
    _copy_region_graphics,
    _disable_blocked_by_content,
    _region_holds_content,
    _region_mirrors_shared,
    _scan_region_content,
    copy_page_style,
)


def test_get_page_style_properties_without_header_or_footer():
    """A new Writer document has header and footer off, and LibreOffice then leaves their
    measures void (None, checked live); None / 100.0 made the whole read fail."""
    from plugin.writer.page import get_page_style_properties

    void_when_off = {"HeaderHeight", "FooterHeight", "HeaderBodyDistance", "FooterBodyDistance"}
    style = MagicMock()
    style.getPropertyValue.side_effect = lambda name: (
        None if name in void_when_off else False if name.endswith("IsOn") else 2000
    )
    doc = MagicMock()
    doc.getStyleFamilies.return_value.getByName.return_value.getByName.return_value = style

    res = get_page_style_properties(doc, "Standard")

    assert res["status"] == "ok"
    props = res["properties"]
    assert props["header_is_on"] is False and props["header_height_mm"] is None
    assert props["footer_body_distance_mm"] is None
    assert props["left_margin_mm"] == 20.0


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
    from unittest.mock import patch

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
    with patch("plugin.writer.html_import.replace_xtext_with_html") as replace_html:
        res = tool.execute(
            ctx,
            style="Standard",
            region="header",
            content="<p>My Header Content</p>",
            auto_height=True,
        )

    assert res["status"] == "ok"
    assert res["region"] == "header"
    assert res["auto_height"] is True
    assert res["format"] == "html"

    style.setPropertyValue.assert_any_call("HeaderIsOn", True)
    style.setPropertyValue.assert_any_call("HeaderIsDynamicHeight", True)
    replace_html.assert_called_once()
    assert replace_html.call_args[0][0] is header_text_obj
    assert replace_html.call_args[0][1] == "<p>My Header Content</p>"
    header_text_obj.setString.assert_not_called()


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
    start_pos = MagicMock()
    text_obj = MagicMock()
    text_cursor = MagicMock()

    doc.getCurrentController.return_value = controller
    controller.getViewCursor.return_value = view_cursor
    view_cursor.getStart.return_value = start_pos
    view_cursor.getText.return_value = text_obj
    text_cursor.getPropertyValue.return_value = None
    text_obj.createTextCursorByRange.return_value = text_cursor

    ctx = TestingFactory.create_context(doc=doc, doc_type="writer")
    tool = PageInsertBreak()
    res = tool.execute(ctx)

    assert res["status"] == "ok"
    text_obj.createTextCursorByRange.assert_called_with(start_pos)
    text_cursor.setPropertyValue.assert_called_with("BreakType", 4)  # PAGE_BEFORE
    text_obj.insertControlCharacter.assert_not_called()


def _attach_page_styles(doc, names):
    styles = doc.getStyleFamilies.return_value.getByName.return_value
    styles.hasByName.side_effect = lambda name: name in names
    styles.getElementNames.return_value = tuple(sorted(names))
    return styles


def test_page_break_view_cursor_page_style_and_restart():
    import sys
    from unittest.mock import call
    setattr(sys.modules["com.sun.star.style.BreakType"], "PAGE_BEFORE", 4)
    doc = MagicMock()
    controller = MagicMock()
    view_cursor = MagicMock()
    start_pos = MagicMock()
    text_obj = MagicMock()
    text_cursor = MagicMock()

    doc.getCurrentController.return_value = controller
    controller.getViewCursor.return_value = view_cursor
    view_cursor.getStart.return_value = start_pos
    view_cursor.getText.return_value = text_obj
    text_cursor.getPropertyValue.return_value = None
    text_obj.createTextCursorByRange.return_value = text_cursor

    _attach_page_styles(doc, {"Annex"})

    ctx = TestingFactory.create_context(doc=doc, doc_type="writer")
    tool = PageInsertBreak()
    res = tool.execute(ctx, page_style="Annex", restart_page_number=2)

    assert res["status"] == "ok"
    assert res["message"] == "Page break inserted."
    text_obj.createTextCursorByRange.assert_called_with(start_pos)
    text_cursor.setPropertyValue.assert_has_calls([
        call("BreakType", 4),
        call("PageDescName", "Annex"),
        call("PageNumberOffset", 2),
    ])
    assert text_cursor.setPropertyValue.call_count == 3
    text_obj.insertControlCharacter.assert_not_called()
    assert not hasattr(text_cursor, "gotoNextParagraph") or text_cursor.gotoNextParagraph.call_count == 0
    assert [c.args[0] for c in text_cursor.getPropertyValue.call_args_list] == ["TextTable"]


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


def test_get_header_footer_returns_html_and_still_lists_logo_and_field():
    """content is the shared XHTML pipeline, not getString(); scan extras stay for machines."""
    from unittest.mock import patch

    text_obj = MagicMock()
    text_obj.getString.return_value = "\nESCRITORIO ZOLET"
    text_obj.createEnumeration.side_effect = lambda: _enum_of([
        _paragraph([_portion("Frame")]),
        _paragraph([_portion("Text"), _portion("TextField", _page_number_field())]),
    ])
    doc, _style = _page_style_doc(text_obj, shapes=[_logo_anchored_in(text_obj)])

    with patch("plugin.writer.html_export.xtext_to_content",
               return_value='<p>ESCRITORIO ZOLET <span title="page-number"/></p>') as export:
        res = PageGetHeaderFooterText().execute(
            TestingFactory.create_context(doc=doc, doc_type="writer"),
            style="Standard", region="header")

    export.assert_called_once()
    assert res["status"] == "ok"
    assert res["format"] == "html"
    assert "page-number" in res["content"]
    assert res["images"] == ["TIMBRE"]
    assert res["fields"] == [{"presentation": "1", "content": "Page Number"}]
    assert res["paragraph_count"] == 2
    assert "warning" not in res
    assert "apply_document_content" not in str(res)


def test_get_header_propagates_export_error_and_disposal():
    from plugin.framework.errors import DocumentDisposedError, ToolExecutionError

    text_obj = MagicMock()
    doc, _style = _page_style_doc(text_obj)
    ctx = TestingFactory.create_context(doc=doc, doc_type="writer")
    with (
        patch("plugin.writer.html_export.xtext_to_content", side_effect=ToolExecutionError("Could not read the document: boom")),
        pytest.raises(ToolExecutionError, match="boom"),
    ):
        PageGetHeaderFooterText().execute(ctx, style="Standard", region="header")
    with (
        patch(
            "plugin.writer.html_export.xtext_to_content",
            side_effect=DocumentDisposedError("closed", object_type="document"),
        ),
        pytest.raises(DocumentDisposedError),
    ):
        PageGetHeaderFooterText().execute(ctx, style="Standard", region="header")


def test_set_header_footer_imports_html_even_when_a_logo_is_present():
    """HTML set with a logo must succeed — no force, no refuse-on-held."""
    from unittest.mock import patch

    text_obj = MagicMock()
    text_obj.createEnumeration.side_effect = lambda: _enum_of([_paragraph([_portion("Frame")])])
    doc, style = _page_style_doc(text_obj, shapes=[_logo_anchored_in(text_obj)])
    html = '<p>ESCRITORIO ZOLET <img src="data:image/png;base64,xx" alt="TIMBRE"/></p>'

    with patch("plugin.writer.html_import.replace_xtext_with_html") as replace_html:
        res = PageSetHeaderFooterText().execute(
            TestingFactory.create_context(doc=doc, doc_type="writer"),
            style="Standard", region="header", content=html)

    assert res["status"] == "ok"
    replace_html.assert_called_once()
    text_obj.setString.assert_not_called()
    style.setPropertyValue.assert_any_call("HeaderIsOn", True)


def test_set_header_footer_imports_field_html_without_force():
    """A footer with a page-number field is a normal HTML set, not a refuse."""
    from unittest.mock import patch

    text_obj = MagicMock()
    text_obj.createEnumeration.side_effect = lambda: _enum_of([
        _paragraph([_portion("Text"), _portion("TextField", _page_number_field())]),
    ])
    doc, _style = _page_style_doc(text_obj)
    html = '<p>Confidential | <span title="page-number"/></p>'

    with patch("plugin.writer.html_import.replace_xtext_with_html") as replace_html:
        res = PageSetHeaderFooterText().execute(
            TestingFactory.create_context(doc=doc, doc_type="writer"),
            style="Standard", region="footer", content=html)

    assert res["status"] == "ok"
    replace_html.assert_called_once()
    assert replace_html.call_args[0][1] == html
    text_obj.setString.assert_not_called()


def test_set_header_footer_imports_table_html_without_force():
    """Letterhead tables go through HTML import; do not add refuse-on-table."""
    from unittest.mock import patch

    text_obj = MagicMock()
    text_obj.createEnumeration.side_effect = lambda: _enum_of([_paragraph([_portion("Text")])])
    doc, _style = _page_style_doc(text_obj)
    html = "<table><tr><td>Logo cell</td><td>Address cell</td></tr></table>"

    with patch("plugin.writer.html_import.replace_xtext_with_html") as replace_html:
        res = PageSetHeaderFooterText().execute(
            TestingFactory.create_context(doc=doc, doc_type="writer"),
            style="Standard", region="header", content=html)

    assert res["status"] == "ok"
    replace_html.assert_called_once()
    assert replace_html.call_args[0][1] == html
    text_obj.setString.assert_not_called()


def test_set_header_footer_enables_a_region_that_is_off():
    from unittest.mock import patch

    text_obj = MagicMock()
    text_obj.createEnumeration.side_effect = lambda: _enum_of([])
    doc, style = _page_style_doc(text_obj)
    style.getPropertyValue.side_effect = lambda n: False if n.endswith("IsOn") else text_obj

    with patch("plugin.writer.html_import.replace_xtext_with_html") as replace_html:
        res = PageSetHeaderFooterText().execute(
            TestingFactory.create_context(doc=doc, doc_type="writer"),
            style="Standard", region="footer", content="Rua Exemplo, 123")

    assert res["status"] == "ok"
    style.setPropertyValue.assert_any_call("FooterIsOn", True)
    replace_html.assert_called_once()
    text_obj.setString.assert_not_called()


def test_page_header_footer_schema_has_no_force_and_descriptions_say_html():
    """force/refuse-on-held is gone: schema omits force; copy says get/set HTML, not wipe."""
    get_desc = PageGetHeaderFooterText.description.lower()
    set_desc = PageSetHeaderFooterText.description.lower()
    assert "html" in get_desc
    assert "html" in set_desc
    assert "images" in get_desc and "fields" in get_desc
    assert "force" not in PageSetHeaderFooterText.parameters["properties"]
    assert "force" not in set_desc
    assert "force" not in get_desc
    assert "wipe" not in set_desc
    assert "refuse" not in set_desc
    assert "setstring" not in set_desc


def test_scan_region_warns_when_portion_cap_is_hit(monkeypatch):
    """A header walk that hits the cap used to drop later fields with no signal."""
    from plugin.writer import page as pg

    text_obj = MagicMock()
    text_obj.createEnumeration.side_effect = lambda: _enum_of([
        _paragraph([
            _portion("TextField", _page_number_field()),
            _portion("TextField", _page_number_field()),
        ]),
    ])
    doc = MagicMock()
    doc.getDrawPage.return_value.getCount.return_value = 0
    monkeypatch.setattr(pg, "_SCAN_PORTION_LIMIT", 1)
    scan = pg._scan_region_content(doc, text_obj)

    assert "warning" in scan
    assert "cap 1" in scan["warning"]
    assert "incomplete" in scan["warning"]


def test_get_header_footer_surfaces_walk_cap_warning(monkeypatch):
    from unittest.mock import patch

    from plugin.writer import page as pg

    text_obj = MagicMock()
    extra = [_portion("Text") for _idx in range(3)]
    text_obj.createEnumeration.side_effect = lambda: _enum_of([_paragraph(extra)])
    doc, _style = _page_style_doc(text_obj)
    monkeypatch.setattr(pg, "_SCAN_PORTION_LIMIT", 1)
    with patch("plugin.writer.html_export.xtext_to_content", return_value="<p>plain</p>"):
        res = PageGetHeaderFooterText().execute(
            TestingFactory.create_context(doc=doc, doc_type="writer"),
            style="Standard", region="header")

    assert res["status"] == "ok"
    assert "cap 1" in res["warning"]


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


def test_scan_sees_logo_when_pyuno_wrappers_differ():
    """Distinct wrappers for the same header XText: bare ``!=`` would skip the logo."""
    from unittest.mock import patch

    header = MagicMock()
    header.createEnumeration.side_effect = lambda: _enum_of([_paragraph([_portion("Frame")])])
    other = MagicMock()
    assert header is not other
    assert header != other
    doc, _style = _page_style_doc(header, shapes=[_logo_anchored_in(other)])

    with patch.object(sys.modules["uno"], "isSame", side_effect=lambda a, b: {a, b} == {header, other}, create=True):
        scan = _scan_region_content(doc, header)

    assert scan["images"] == ["TIMBRE"]


def test_get_header_reports_logo_when_anchor_text_wrapper_differs():
    """page_get metadata must list the image even when ``==`` on XText would miss it."""
    from unittest.mock import patch

    text_obj = MagicMock()
    text_obj.getString.return_value = "\n"
    text_obj.createEnumeration.side_effect = lambda: _enum_of([_paragraph([_portion("Frame")])])
    other = MagicMock()
    doc, _style = _page_style_doc(text_obj, shapes=[_logo_anchored_in(other)])

    with (
        patch.object(sys.modules["uno"], "isSame", side_effect=lambda a, b: {a, b} == {text_obj, other}, create=True),
        patch("plugin.writer.html_export.xtext_to_content", return_value=""),
    ):
        res = PageGetHeaderFooterText().execute(
            TestingFactory.create_context(doc=doc, doc_type="writer"), style="Standard", region="header")

    assert res["status"] == "ok"
    assert res["images"] == ["TIMBRE"]


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


# --- header/footer off: refuse while the region still holds content ----------------------


def _empty_text_obj():
    text_obj = MagicMock()
    text_obj.getString.return_value = ""
    text_obj.createEnumeration.side_effect = lambda: _enum_of([])
    return text_obj


def test_region_holds_content_is_false_for_empty_or_whitespace():
    doc, _style = _page_style_doc(_empty_text_obj())
    assert _region_holds_content(doc, None) is False
    empty = _empty_text_obj()
    assert _region_holds_content(doc, empty) is False
    ws = MagicMock()
    ws.getString.return_value = "  \n"
    ws.createEnumeration.side_effect = lambda: _enum_of([])
    assert _region_holds_content(doc, ws) is False


def test_region_holds_content_sees_text_fields_images_and_tables():
    text_obj = MagicMock()
    text_obj.getString.return_value = "Letterhead"
    text_obj.createEnumeration.side_effect = lambda: _enum_of([])
    doc, _style = _page_style_doc(text_obj)
    assert _region_holds_content(doc, text_obj) is True

    field_obj = MagicMock()
    field_obj.getString.return_value = ""
    field_obj.createEnumeration.side_effect = lambda: _enum_of([
        _paragraph([_portion("TextField", _page_number_field())]),
    ])
    doc_f, _style_f = _page_style_doc(field_obj)
    assert _region_holds_content(doc_f, field_obj) is True

    logo_obj = MagicMock()
    logo_obj.getString.return_value = ""
    logo_obj.createEnumeration.side_effect = lambda: _enum_of([_paragraph([_portion("Frame")])])
    doc_i, _style_i = _page_style_doc(logo_obj, shapes=[_logo_anchored_in(logo_obj)])
    assert _region_holds_content(doc_i, logo_obj) is True

    table = MagicMock()
    table.supportsService.side_effect = lambda s: s == "com.sun.star.text.TextTable"
    table_obj = MagicMock()
    table_obj.getString.return_value = ""
    table_obj.createEnumeration.side_effect = lambda: _enum_of([table])
    doc_t, _style_t = _page_style_doc(table_obj)
    assert _region_holds_content(doc_t, table_obj) is True


def test_set_style_properties_refuses_header_off_while_text_remains():
    text_obj = _empty_text_obj()
    text_obj.getString.return_value = "Keep this letterhead"
    doc, style = _page_style_doc(text_obj)

    res = PageSetStyleProperties().execute(
        TestingFactory.create_context(doc=doc, doc_type="writer"),
        style="Standard", width_mm=300, header_is_on=False)

    assert res["status"] == "error"
    assert "page_set_header_footer_text" in res["message"]
    assert "header_is_on=false" in res["message"]
    style.setPropertyValue.assert_not_called()


def test_set_style_properties_refuses_footer_off_while_logo_remains():
    text_obj = MagicMock()
    text_obj.getString.return_value = ""
    text_obj.createEnumeration.side_effect = lambda: _enum_of([_paragraph([_portion("Frame")])])
    doc, style = _page_style_doc(text_obj, shapes=[_logo_anchored_in(text_obj)])

    res = PageSetStyleProperties().execute(
        TestingFactory.create_context(doc=doc, doc_type="writer"),
        style="Standard", footer_is_on=False)

    assert res["status"] == "error"
    assert "footer" in res["message"]
    style.setPropertyValue.assert_not_called()


def test_set_style_properties_allows_header_off_when_empty():
    doc, style = _page_style_doc(_empty_text_obj())

    res = PageSetStyleProperties().execute(
        TestingFactory.create_context(doc=doc, doc_type="writer"),
        style="Standard", header_is_on=False)

    assert res["status"] == "ok"
    style.setPropertyValue.assert_any_call("HeaderIsOn", False)


@pytest.mark.parametrize("broken", ["getString", "createEnumeration", "drawPage"])
def test_set_style_properties_refuses_header_off_when_read_fails(broken):
    """A failed read is not "no content": the guard fails closed."""
    text_obj = _empty_text_obj()
    doc, style = _page_style_doc(text_obj)
    if broken == "getString":
        text_obj.getString.side_effect = RuntimeError("bridge hiccup")
    elif broken == "createEnumeration":
        text_obj.createEnumeration.side_effect = RuntimeError("bridge hiccup")
    else:
        doc.getDrawPage.side_effect = RuntimeError("bridge hiccup")

    res = PageSetStyleProperties().execute(
        TestingFactory.create_context(doc=doc, doc_type="writer"),
        style="Standard", header_is_on=False)

    assert res["status"] == "error"
    assert "Cannot turn the header off" in res["message"]
    assert "bridge hiccup" in res["message"]
    style.setPropertyValue.assert_not_called()


def test_disable_guard_refuses_when_region_text_unreadable():
    doc, style = _page_style_doc(_empty_text_obj())
    style.getPropertyValue.side_effect = RuntimeError("no HeaderText")
    msg = _disable_blocked_by_content(doc, style, {"footer_is_on": False})
    assert msg is not None and "Cannot turn the footer off" in msg


def test_disable_guard_propagates_disposal():
    class DisposedException(Exception):
        pass

    text_obj = _empty_text_obj()
    text_obj.getString.side_effect = DisposedException("gone")
    doc, style = _page_style_doc(text_obj)
    with pytest.raises(DisposedException):
        _disable_blocked_by_content(doc, style, {"header_is_on": False})


def test_disable_guard_skips_page_anchored_shape():
    """A page-anchored shape has no anchor text; it must not block an empty header."""
    shape = MagicMock()
    shape.getAnchor.return_value = None
    doc, style = _page_style_doc(_empty_text_obj(), shapes=[shape])
    assert _disable_blocked_by_content(doc, style, {"header_is_on": False}) is None


def test_set_style_properties_allows_enable_while_content_remains():
    text_obj = _empty_text_obj()
    text_obj.getString.return_value = "Already there"
    doc, style = _page_style_doc(text_obj)

    res = PageSetStyleProperties().execute(
        TestingFactory.create_context(doc=doc, doc_type="writer"),
        style="Standard", header_is_on=True)

    assert res["status"] == "ok"
    style.setPropertyValue.assert_any_call("HeaderIsOn", True)


def test_disable_blocked_by_content_skips_enable_and_lists_held_regions():
    text_obj = _empty_text_obj()
    text_obj.getString.return_value = "First-page letterhead"
    doc, style = _page_style_doc(text_obj)
    assert _disable_blocked_by_content(doc, style, {"header_is_on": True}) is None
    msg = _disable_blocked_by_content(doc, style, {"header_is_on": False})
    assert msg is not None
    assert "header" in msg
    assert "page_set_header_footer_text" in msg


def test_region_mirrors_shared_is_true_only_for_bool_true():
    style = MagicMock()
    style.getPropertyValue.return_value = True
    assert _region_mirrors_shared(style, "header_first") is True
    assert _region_mirrors_shared(style, "footer_left") is True
    assert _region_mirrors_shared(style, "header") is False
    style.getPropertyValue.return_value = False
    assert _region_mirrors_shared(style, "header_first") is False
    style.getPropertyValue.return_value = MagicMock()
    assert _region_mirrors_shared(style, "header_first") is False


def test_disable_ignores_stale_first_page_mirror_when_shared():
    """GHA 35466498641: Windows HeaderTextFirst leftover while FirstIsShared."""
    shared = _empty_text_obj()
    leftover = _empty_text_obj()
    leftover.getString.return_value = "Stale first-page letterhead"
    doc, style = _page_style_doc(shared)
    style.getPropertyValue.side_effect = lambda n: {
        "HeaderIsOn": True,
        "FooterIsOn": True,
        "FirstIsShared": True,
        "HeaderIsShared": True,
        "FooterIsShared": True,
        "HeaderText": shared,
        "FooterText": shared,
        "HeaderTextFirst": leftover,
        "FooterTextFirst": leftover,
        "HeaderTextLeft": leftover,
        "FooterTextLeft": leftover,
    }[n]
    assert _disable_blocked_by_content(doc, style, {"header_is_on": False}) is None


def test_disable_still_refuses_independent_first_page_letterhead():
    shared = _empty_text_obj()
    first = _empty_text_obj()
    first.getString.return_value = "First-page letterhead"
    doc, style = _page_style_doc(shared)
    style.getPropertyValue.side_effect = lambda n: {
        "HeaderIsOn": True,
        "FooterIsOn": True,
        "FirstIsShared": False,
        "HeaderIsShared": True,
        "FooterIsShared": True,
        "HeaderText": shared,
        "FooterText": shared,
        "HeaderTextFirst": first,
        "FooterTextFirst": shared,
        "HeaderTextLeft": shared,
        "FooterTextLeft": shared,
    }[n]
    msg = _disable_blocked_by_content(doc, style, {"header_is_on": False})
    assert msg is not None
    assert "header_first" in msg


def test_page_uno_skips_windows_leftover_hidden_xtext() -> None:
    """GHA 34689136372: leftover Hidden _blank hung in xtext_to_content."""
    from pathlib import Path

    writer = Path(__file__).parent
    for name in ("test_page_header_html_uno.py", "test_page_uno.py"):
        src = (writer / name).read_text(encoding="utf-8")
        # writer_services_tool_context performs the leftover-Hidden skip.
        assert (
            "skip_windows_leftover_hidden_load" in src
            or "writer_services_tool_context" in src
        ), name
        assert "page_header Hidden _blank xtext_to_content" in src, name
        assert "34689136372" in src, name


# ---- D5: page break happy paths + last-paragraph edge ------------------------

def _pagebreak_doc(found_next=True):
    import types as _types
    import sys as _sys
    bt = _types.ModuleType("com.sun.star.style.BreakType")
    bt.PAGE_BEFORE = "PAGE_BEFORE"
    _sys.modules["com.sun.star.style.BreakType"] = bt
    doc = MagicMock()
    found = MagicMock()
    para = MagicMock()
    para.gotoNextParagraph.return_value = found_next
    para.getPropertyValue.return_value = None
    found.getText.return_value.createTextCursorByRange.return_value = para
    doc.findFirst.return_value = found
    return doc, para


def test_page_break_before_text_sets_break_on_match_paragraph():
    from plugin.writer.page import PageInsertBreak

    doc, para = _pagebreak_doc()
    res = PageInsertBreak().execute(SimpleNamespace(doc=doc), before_text="Assinaturas")
    assert res["status"] == "ok" and "before" in res["message"]
    para.setPropertyValue.assert_called_once_with("BreakType", "PAGE_BEFORE")
    para.gotoNextParagraph.assert_not_called()


def test_page_break_page_style_sets_page_desc_name_on_match_paragraph():
    from unittest.mock import call
    from plugin.writer.page import PageInsertBreak

    doc, para = _pagebreak_doc()
    _attach_page_styles(doc, {"Annex", "Standard"})
    res = PageInsertBreak().execute(
        SimpleNamespace(doc=doc),
        before_text="Assinaturas",
        page_style="Annex",
    )
    assert res["status"] == "ok"
    assert "before" in res["message"]
    assert set(res) == {"status", "message", "page_style"}
    assert res["page_style"] == "Annex"
    para.setPropertyValue.assert_has_calls([call("BreakType", "PAGE_BEFORE"), call("PageDescName", "Annex")])
    assert para.setPropertyValue.call_count == 2
    para.gotoNextParagraph.assert_not_called()
    assert [c.args[0] for c in para.getPropertyValue.call_args_list] == ["TextTable"]


def test_page_break_after_text_breaks_on_following_paragraph():
    from plugin.writer.page import PageInsertBreak

    doc, para = _pagebreak_doc(found_next=True)
    res = PageInsertBreak().execute(SimpleNamespace(doc=doc), after_text="clausula")
    assert res["status"] == "ok" and "after" in res["message"]
    para.gotoNextParagraph.assert_called_once()
    para.setPropertyValue.assert_called_once_with("BreakType", "PAGE_BEFORE")


def test_page_break_restart_sets_page_number_offset():
    from unittest.mock import call
    from plugin.writer.page import PageInsertBreak

    doc, para = _pagebreak_doc(found_next=True)
    _attach_page_styles(doc, {"Annex"})
    res = PageInsertBreak().execute(
        SimpleNamespace(doc=doc),
        after_text="clausula",
        page_style="Annex",
        restart_page_number=1,
    )
    assert res["status"] == "ok"
    assert "after" in res["message"]
    assert set(res) == {"status", "message", "page_style", "restart_page_number"}
    assert res["page_style"] == "Annex"
    assert res["restart_page_number"] == 1
    para.gotoNextParagraph.assert_called_once()
    para.setPropertyValue.assert_has_calls([
        call("BreakType", "PAGE_BEFORE"),
        call("PageDescName", "Annex"),
        call("PageNumberOffset", 1),
    ])
    assert para.setPropertyValue.call_count == 3


def test_page_break_restart_short_max_is_accepted():
    from unittest.mock import call
    from plugin.writer.page import PageInsertBreak

    doc, para = _pagebreak_doc(found_next=True)
    _attach_page_styles(doc, {"Annex"})
    res = PageInsertBreak().execute(
        SimpleNamespace(doc=doc),
        after_text="clausula",
        page_style="Annex",
        restart_page_number=32767,
    )
    assert res["status"] == "ok"
    assert res["restart_page_number"] == 32767
    para.setPropertyValue.assert_has_calls([
        call("BreakType", "PAGE_BEFORE"),
        call("PageDescName", "Annex"),
        call("PageNumberOffset", 32767),
    ])
    assert para.setPropertyValue.call_count == 3


def test_page_break_after_text_last_paragraph_errors_instead_of_wrong_direction():
    from plugin.writer.page import PageInsertBreak

    doc, para = _pagebreak_doc(found_next=False)
    res = PageInsertBreak().execute(SimpleNamespace(doc=doc), after_text="assinatura final")
    assert res["status"] == "error" and "last paragraph" in res["message"]
    para.setPropertyValue.assert_not_called()  # never the wrong-direction silent break


def test_page_break_page_style_last_paragraph_still_errors():
    from plugin.writer.page import PageInsertBreak

    doc, para = _pagebreak_doc(found_next=False)
    _attach_page_styles(doc, {"Annex"})
    res = PageInsertBreak().execute(
        SimpleNamespace(doc=doc),
        after_text="assinatura final",
        page_style="Annex",
        restart_page_number=1,
    )
    assert res["status"] == "error"
    assert "last paragraph" in res["message"]
    para.setPropertyValue.assert_not_called()

# ---- D5: insert_page_break anchored -----------------------------------------

def test_page_break_both_anchors_rejected():
    from plugin.writer.page import PageInsertBreak
    res = PageInsertBreak().execute(SimpleNamespace(doc=MagicMock()), before_text="a", after_text="b")
    assert res["status"] == "error" and "only one" in res["message"]


def test_page_break_both_anchors_still_rejected_when_page_style_set():
    from plugin.writer.page import PageInsertBreak

    doc = MagicMock()
    res = PageInsertBreak().execute(
        SimpleNamespace(doc=doc),
        before_text="a",
        after_text="b",
        page_style="Nope",
        restart_page_number=1,
    )
    assert res["status"] == "error"
    assert "only one" in res["message"]
    doc.getStyleFamilies.assert_not_called()


def test_page_break_anchor_not_found():
    from plugin.writer.page import PageInsertBreak
    doc = MagicMock()
    doc.findFirst.return_value = None
    # com.sun.star.style.BreakType is a mocked module; the import inside execute resolves via the uno mock.
    res = PageInsertBreak().execute(SimpleNamespace(doc=doc), before_text="Signature")
    assert res["status"] == "error" and "not found" in res["message"]


def test_page_break_unknown_page_style_changes_nothing():
    from plugin.writer.page import PageInsertBreak

    doc, para = _pagebreak_doc()
    _attach_page_styles(doc, {"Standard"})
    res = PageInsertBreak().execute(
        SimpleNamespace(doc=doc),
        before_text="Assinaturas",
        page_style="NoSuch",
    )
    assert res["status"] == "error"
    assert "No page style named 'NoSuch'" in res["message"]
    assert "Standard" in res["message"]
    para.setPropertyValue.assert_not_called()
    doc.findFirst.assert_not_called()


def test_page_break_restart_without_page_style_errors():
    from plugin.writer.page import PageInsertBreak

    doc, para = _pagebreak_doc()
    res = PageInsertBreak().execute(
        SimpleNamespace(doc=doc),
        before_text="Assinaturas",
        restart_page_number=1,
    )
    assert res["status"] == "error"
    assert res["message"] == "restart_page_number requires page_style."
    para.setPropertyValue.assert_not_called()
    doc.findFirst.assert_not_called()
    doc.getStyleFamilies.assert_not_called()


def test_page_break_restart_rejects_non_positive():
    from plugin.writer.page import PageInsertBreak

    for val in (0, -1, True, 1.5, "1", 32768):
        doc, para = _pagebreak_doc()
        _attach_page_styles(doc, {"Annex"})
        res = PageInsertBreak().execute(
            SimpleNamespace(doc=doc),
            before_text="Assinaturas",
            page_style="Annex",
            restart_page_number=val,
        )
        assert res["status"] == "error"
        assert res["message"] == "restart_page_number must be an integer >= 1."
        para.setPropertyValue.assert_not_called()


def test_page_break_page_style_must_be_a_string():
    from plugin.writer.page import PageInsertBreak

    doc, para = _pagebreak_doc()
    res = PageInsertBreak().execute(
        SimpleNamespace(doc=doc),
        before_text="Assinaturas",
        page_style=123,
    )
    assert res["status"] == "error"
    assert res["message"] == "page_style must be a string."
    doc.getStyleFamilies.assert_not_called()
    para.setPropertyValue.assert_not_called()


def test_page_break_empty_style_with_restart_does_not_resolve():
    from plugin.writer.page import PageInsertBreak

    doc, para = _pagebreak_doc()
    res = PageInsertBreak().execute(
        SimpleNamespace(doc=doc),
        before_text="Assinaturas",
        page_style="",
        restart_page_number=1,
    )
    assert res["status"] == "error"
    assert res["message"] == "restart_page_number requires page_style."
    doc.getStyleFamilies.assert_not_called()


def test_copy_region_graphics_skips_non_graphic():
    """AT_PARAGRAPH frames are not TextGraphicObject; they are reported, not copied."""
    src = object()
    dest = MagicMock()
    graphic = MagicMock()
    graphic.supportsService.return_value = True
    graphic.getName.return_value = "logo"
    graphic.getAnchor.return_value.getText.return_value = src
    graphic.getPropertyValue.side_effect = lambda name: "v-" + name
    frame = MagicMock()
    frame.supportsService.return_value = False
    frame.getName.return_value = "frame"
    frame.getAnchor.return_value.getText.return_value = src
    elsewhere = MagicMock()
    elsewhere.getAnchor.return_value.getText.return_value = object()
    draw = MagicMock()
    draw.getCount.return_value = 3
    draw.getByIndex.side_effect = [graphic, frame, elsewhere]
    doc = MagicMock()
    doc.getDrawPage.return_value = draw
    created = MagicMock()
    doc.createInstance.return_value = created

    copied, skipped = _copy_region_graphics(doc, src, dest)

    assert copied == 1
    assert skipped == ["frame"]
    dest.insertTextContent.assert_called_once()
    created.setPropertyValue.assert_any_call("HyperLinkURL", "v-HyperLinkURL")
    created.setPropertyValue.assert_any_call("AnchorType", "v-AnchorType")


def test_copy_page_style_inserts_without_touching_follow_style():
    """Off regions are skipped. FollowStyle is left at the new style's own default."""
    source = MagicMock()
    source.getParentStyle.return_value = ""
    source.getPropertyValue.return_value = False
    family = MagicMock()
    family.hasByName.side_effect = lambda name: name == "Src"
    family.getByName.return_value = source
    family.getElementNames.return_value = ("Src",)
    doc = MagicMock()
    doc.getStyleFamilies.return_value.getByName.return_value = family
    clone = MagicMock()
    doc.createInstance.return_value = clone

    with patch(
        "plugin.writer.styles._copy_direct_style_properties",
        return_value=(["LeftMargin"], []),
    ) as copier:
        stats = copy_page_style(doc, "Src", "Dst")

    family.insertByName.assert_called_once_with("Dst", clone)
    copier.assert_called_once_with(source, clone)
    assert stats["properties_copied"] == 1
    assert stats["shapes_copied"] == 0
    for call in clone.setPropertyValue.call_args_list:
        assert call.args[0] != "FollowStyle"


def test_copy_page_regions_does_not_export_html():
    from plugin.writer.page import _copy_page_regions

    src_text = MagicMock()
    src_text.getString.return_value = "Annex "

    dest_text = MagicMock()
    dest_text.getString.return_value = "Annex "

    def _src_get_prop(name: str):
        if name == "FooterIsOn":
            return True
        if name.endswith("IsOn"):
            return False
        if name == "FooterText":
            return src_text
        return 0

    source = MagicMock()
    source.getPropertyValue.side_effect = _src_get_prop

    def _clone_get_prop(name: str):
        if name == "FooterText":
            return dest_text
        return 0

    clone = MagicMock()
    clone.getPropertyValue.side_effect = _clone_get_prop

    doc = MagicMock()
    doc.getDrawPage.return_value.getCount.return_value = 0

    with patch("plugin.writer.html_export._copy_flow_into_xtext", return_value=1) as mock_flow, \
         patch("plugin.writer.html_export.xtext_to_content") as mock_xtext_to_content, \
         patch("plugin.writer.html_import.replace_xtext_with_html") as mock_replace_html:
        _copy_page_regions(doc, source, clone, uno_ctx=object(), services=None)

    mock_flow.assert_called_once_with(doc, src_text, dest_text)
    mock_xtext_to_content.assert_not_called()
    mock_replace_html.assert_not_called()


# --- header/footer: replace one substring, leave fields and graphics -----------


class _ReplaceCursor:
    """goRight cursor over a portion-index string. ``\\x01`` is one field step."""

    def __init__(self, hay):
        self.hay = hay
        self.pos = 0
        self.anchor = 0
        self.written = None

    def goRight(self, count, expand):
        count = int(count)
        nxt = self.pos + count
        if nxt > len(self.hay):
            return False
        if not expand:
            self.anchor = nxt
        else:
            self.anchor = self.pos
        self.pos = nxt
        return True

    def getString(self):
        if self.written is not None:
            return self.written
        start, end = (self.anchor, self.pos) if self.anchor <= self.pos else (self.pos, self.anchor)
        return self.hay[start:end]

    def setString(self, value):
        self.written = value

    def getStart(self):
        return ("start", self.anchor)

    def getEnd(self):
        return ("end", self.pos)


def _text_portion(text):
    portion = _portion("Text")
    portion.getString.return_value = text
    return portion


def _replace_doc(portions, shapes=(), hay=""):
    """Page style whose footer enumerates *portions*. Cursors walk *hay*."""
    text_obj = MagicMock()
    text_obj.createEnumeration.side_effect = lambda: _enum_of([_paragraph(portions)])
    text_obj.createTextCursorByRange.side_effect = lambda _rng: _ReplaceCursor(hay)
    doc, _style = _page_style_doc(text_obj, shapes=shapes)
    return doc, text_obj


def _cursors_for(text_obj, hay):
    """Record every cursor the replace tool builds so tests can see setString."""
    seen = []

    def make(_rng):
        cursor = _ReplaceCursor(hay)
        seen.append(cursor)
        return cursor

    text_obj.createTextCursorByRange.side_effect = make
    return seen


def _replace(doc, **kwargs):
    args = {"style": "Standard", "region": "footer", "old_content": "Annex", "content": "Appendix"}
    args.update(kwargs)
    return PageReplaceHeaderFooterText().execute(SimpleNamespace(doc=doc), **args)


def test_replace_header_footer_rejects_bad_args():
    doc = MagicMock()
    tool = PageReplaceHeaderFooterText()
    both = tool.execute(
        SimpleNamespace(doc=doc), region="footer", old_content="Annex", content="A",
        occurrence=1, all_matches=True,
    )
    assert both["status"] == "error" and "all_matches" in both["message"]
    empty = tool.execute(SimpleNamespace(doc=doc), region="footer", old_content="", content="A")
    assert empty["status"] == "error" and "old_content" in empty["message"]
    ok, err = tool.validate(region="nope", old_content="Annex", content="A")
    assert ok is False and err is not None and "region" in err
    doc.getStyleFamilies.assert_not_called()


def test_replace_header_footer_skips_mirrored_region_without_writing():
    text_obj = MagicMock()
    doc, style = _page_style_doc(text_obj)

    def props(name):
        if name == "FirstIsShared":
            return True
        if name.endswith("IsOn"):
            return True
        return text_obj

    style.getPropertyValue.side_effect = props
    res = _replace(doc, region="footer_first", old_content="Annex", content="Appendix")
    assert res["status"] == "skipped" and res["mirrored"] is True
    assert "region='footer'" in res["message"]
    text_obj.createTextCursorByRange.assert_not_called()


def test_replace_header_footer_missing_string_writes_nothing():
    doc, text_obj = _replace_doc([_text_portion("Hello")], hay="Hello")
    res = _replace(doc, old_content="Annex", content="Appendix")
    assert res["status"] == "error" and "not found" in res["message"]
    text_obj.createTextCursorByRange.assert_not_called()


def test_replace_header_footer_occurrence_and_all_matches():
    portions = [_text_portion("Annex and Annex")]
    hay = "Annex and Annex"
    doc, text_obj = _replace_doc(portions, hay=hay)
    seen = _cursors_for(text_obj, hay)
    second = _replace(doc, old_content="Annex", content="Appendix", occurrence=1)
    assert second["status"] == "ok" and second["replaced_count"] == 1
    assert len(seen) == 1 and seen[0].written == "Appendix" and seen[0].pos == 15

    doc_all, text_all = _replace_doc(portions, hay=hay)
    seen_all = _cursors_for(text_all, hay)
    res_all = _replace(doc_all, old_content="Annex", content="Appendix", all_matches=True)
    assert res_all["status"] == "ok" and res_all["replaced_count"] == 2
    assert [c.written for c in seen_all] == ["Appendix", "Appendix"]

    doc_past, _text_past = _replace_doc(portions, hay=hay)
    past = _replace(doc_past, old_content="Annex", content="Appendix", occurrence=5)
    assert past["status"] == "error" and "past the end" in past["message"]


def test_replace_header_footer_counts_a_field_as_one_cursor_step():
    """The literal after a field is indexed with \\x01, not the field's visible digits."""
    hay = "Annex \x01 of 274"
    portions = [_text_portion("Annex "), _portion("TextField", _page_number_field()), _text_portion(" of 274")]
    doc, text_obj = _replace_doc(portions, hay=hay)
    shape = _logo_anchored_in(text_obj)
    doc.getDrawPage().getCount.return_value = 1
    doc.getDrawPage().getByIndex.side_effect = lambda i: shape
    seen = _cursors_for(text_obj, hay)
    res = _replace(doc, old_content="274", content="300")
    assert res["status"] == "ok", res
    assert res["replaced_count"] == 1
    assert res["fields_preserved"] == 1
    assert res["shapes_preserved"] == 1
    assert len(seen) == 1 and seen[0].written == "300"
    # "Annex " (6) + field (1) + " of " (4) = 11. A getString() index would use the
    # field's presentation ("1") and still land here only because that happens to
    # be one character; the haystack itself is the cursor index.
    assert seen[0].anchor == 11


def test_replace_header_footer_refuses_character_anchored_image():
    from com.sun.star.text.TextContentAnchorType import AT_CHARACTER

    hay = "Annex 1: Intro"
    portions = [_text_portion(hay)]
    doc, text_obj = _replace_doc(portions, hay=hay)
    seen = _cursors_for(text_obj, hay)
    text_obj.compareRegionStarts.side_effect = lambda left, right: 0 if right == seen[0].getStart() else 1
    shape = _logo_anchored_in(text_obj, name="stamp")
    shape.getPropertyValue.side_effect = lambda name: AT_CHARACTER if name == "AnchorType" else None
    doc.getDrawPage().getCount.return_value = 1
    doc.getDrawPage().getByIndex.side_effect = lambda i: shape
    res = _replace(doc, old_content=hay, content="Annex A: Methods")
    assert res["status"] == "error" and "stamp" in res["message"]
    assert seen and seen[0].written is None


def test_mm_to_hmm_rounds():
    from plugin.writer.page import _mm_to_hmm
    assert _mm_to_hmm(2.3) == 230
    assert _mm_to_hmm(0) == 0
    with pytest.raises((TypeError, ValueError)):
        _mm_to_hmm(True)
    with pytest.raises((TypeError, ValueError)):
        _mm_to_hmm(-1)


def test_set_page_style_width_rounds():
    from plugin.writer.page import PageSetStyleProperties
    doc = MagicMock()
    style = MagicMock()
    doc.getStyleFamilies().getByName().getByName.return_value = style
    ctx = SimpleNamespace(doc=doc)

    res = PageSetStyleProperties().execute(ctx, width_mm=2.3)
    assert res["status"] == "ok"
    style.setPropertyValue.assert_called_with("Width", 230)


def test_set_page_style_stops_and_reports_updated():
    from plugin.writer.page import PageSetStyleProperties
    doc = MagicMock()
    style = MagicMock()
    def set_prop(prop, val):
        if prop == "Height":
            raise RuntimeError("Height failed")
    style.setPropertyValue.side_effect = set_prop
    doc.getStyleFamilies().getByName().getByName.return_value = style
    ctx = SimpleNamespace(doc=doc)

    res = PageSetStyleProperties().execute(ctx, width_mm=10, height_mm=20)
    assert res["status"] == "error"
    assert res["updated"] == ["width"]
    assert "height" in res["failed"]


def test_set_page_style_bad_mm_writes_nothing():
    from plugin.writer.page import PageSetStyleProperties
    doc = MagicMock()
    style = MagicMock()
    doc.getStyleFamilies().getByName().getByName.return_value = style
    ctx = SimpleNamespace(doc=doc)

    with pytest.raises(ValueError, match="non-negative"):
        PageSetStyleProperties().execute(ctx, width_mm=-1)
    style.setPropertyValue.assert_not_called()


def test_is_landscape_flag_only_restores_size():
    from plugin.writer.page import PageSetStyleProperties
    doc = MagicMock()
    style = MagicMock()
    style.getPropertyValue.side_effect = lambda prop: 21000 if prop == "Width" else (29700 if prop == "Height" else None)
    doc.getStyleFamilies().getByName().getByName.return_value = style
    ctx = SimpleNamespace(doc=doc)

    res = PageSetStyleProperties().execute(ctx, is_landscape=True)
    assert res["status"] == "ok"
    style.setPropertyValue.assert_any_call("IsLandscape", True)
    style.setPropertyValue.assert_any_call("Width", 21000)
    style.setPropertyValue.assert_any_call("Height", 29700)


def test_is_landscape_explicit_size_written_after_flag():
    from plugin.writer.page import PageSetStyleProperties
    doc = MagicMock()
    style = MagicMock()
    doc.getStyleFamilies().getByName().getByName.return_value = style
    ctx = SimpleNamespace(doc=doc)

    res = PageSetStyleProperties().execute(ctx, is_landscape=True, width_mm=297, height_mm=210)
    assert res["status"] == "ok"
    calls = [c.args for c in style.setPropertyValue.call_args_list]
    props = [c[0] for c in calls]
    assert "IsLandscape" in props
    assert props.index("IsLandscape") < props.index("Width")
    assert props.index("IsLandscape") < props.index("Height")
    style.setPropertyValue.assert_any_call("Width", 29700)
    style.setPropertyValue.assert_any_call("Height", 21000)


def test_page_style_layout_read_and_write():
    from types import SimpleNamespace
    import sys
    from plugin.writer.page import get_page_style_properties, PageSetStyleProperties

    psl = SimpleNamespace(ALL=0, LEFT=1, RIGHT=2, MIRRORED=3)
    sys.modules["com.sun.star.style.PageStyleLayout"] = psl

    doc = MagicMock()
    style = MagicMock()
    layout_enum = SimpleNamespace(value="ALL")
    style.getPropertyValue.side_effect = lambda prop: layout_enum if prop == "PageStyleLayout" else 21000
    doc.getStyleFamilies().getByName().getByName.return_value = style
    res = get_page_style_properties(doc, "Standard")
    assert res["properties"]["page_style_layout"] == 0

    ctx = SimpleNamespace(doc=doc)
    with pytest.raises(ValueError, match="page_style_layout"):
        PageSetStyleProperties().execute(ctx, page_style_layout=4)
    style.setPropertyValue.assert_not_called()

    res_ok = PageSetStyleProperties().execute(ctx, page_style_layout=1)
    assert res_ok["status"] == "ok"
    style.setPropertyValue.assert_any_call("PageStyleLayout", 1)


def test_register_paragraph_style_missing_still_reads():
    from plugin.writer.page import get_page_style_properties
    doc = MagicMock()
    style = MagicMock()
    def get_prop(prop):
        if prop == "RegisterParagraphStyle":
            raise RuntimeError("property missing")
        if prop == "Width":
            return 21000
        return None
    style.getPropertyValue.side_effect = get_prop
    doc.getStyleFamilies().getByName().getByName.return_value = style

    res = get_page_style_properties(doc, "Standard")
    assert res["status"] == "ok"
    assert res["properties"]["width_mm"] == 210.0
    assert "register_paragraph_style" not in res["properties"]


def test_set_header_first_skipped_when_shared():
    from plugin.writer.page import PageSetHeaderFooterText
    doc = MagicMock()
    style = MagicMock()
    style.getPropertyValue.side_effect = lambda prop: True if prop == "FirstIsShared" else None
    doc.getStyleFamilies().getByName().getByName.return_value = style
    ctx = SimpleNamespace(doc=doc)

    res = PageSetHeaderFooterText().execute(ctx, region="header_first", content="<p>Test</p>")
    assert res["status"] == "skipped"
    assert res["mirrored"] is True
    style.setPropertyValue.assert_not_called()


def test_set_header_rolls_back_enable_when_import_fails():
    from plugin.writer.page import PageSetHeaderFooterText
    doc = MagicMock()
    style = MagicMock()
    style.getPropertyValue.side_effect = lambda prop: False if prop == "HeaderIsOn" else MagicMock()
    doc.getStyleFamilies().getByName().getByName.return_value = style
    ctx = SimpleNamespace(doc=doc)

    with patch("plugin.writer.html_import.replace_xtext_with_html", side_effect=RuntimeError("import failed")):
        res = PageSetHeaderFooterText().execute(ctx, region="header", content="<p>Test</p>")
        assert res["status"] == "error"
        calls = [c.args for c in style.setPropertyValue.call_args_list if c.args[0] == "HeaderIsOn"]
        assert calls[0] == ("HeaderIsOn", True)
        assert calls[1] == ("HeaderIsOn", False)


def test_set_columns_spacing_zero_clears_inner_margins():
    from plugin.writer.page import PageSetColumns
    doc = MagicMock()
    style = MagicMock()
    text_columns = MagicMock()
    col1 = MagicMock(RightMargin=250, LeftMargin=0)
    col2 = MagicMock(RightMargin=0, LeftMargin=250)
    text_columns.getColumns.return_value = [col1, col2]
    style.getPropertyValue.return_value = text_columns
    doc.getStyleFamilies().getByName().getByName.return_value = style
    ctx = SimpleNamespace(doc=doc)

    res = PageSetColumns().execute(ctx, column_count=2, spacing_mm=0)
    assert res["status"] == "ok"
    assert col1.RightMargin == 0
    assert col2.LeftMargin == 0
    text_columns.setColumns.assert_called_once()

    text_columns.setColumnCount.reset_mock()
    res_err = PageSetColumns().execute(ctx, column_count=0)
    assert res_err["status"] == "error"
    assert "at least 1" in res_err["message"]
    text_columns.setColumnCount.assert_not_called()


def test_get_columns_marks_relative_width():
    from plugin.writer.page import PageGetColumns
    doc = MagicMock()
    style = MagicMock()
    text_columns = MagicMock()
    col = MagicMock(Width=100, LeftMargin=0, RightMargin=0)
    text_columns.getColumnCount.return_value = 1
    text_columns.getColumns.return_value = [col]
    style.getPropertyValue.return_value = text_columns
    doc.getStyleFamilies().getByName().getByName.return_value = style
    ctx = SimpleNamespace(doc=doc)

    res = PageGetColumns().execute(ctx)
    assert res["status"] == "ok"
    assert len(res["columns"]) == 1
    assert res["columns"][0]["width_unit"] == "relative"


def test_anchor_point_on_first_character_blocks():
    from plugin.writer.page import _anchor_point_in_match
    text = MagicMock()
    p0 = MagicMock()
    p2 = MagicMock()
    cursor = MagicMock()
    cursor.getStart.return_value = p0
    cursor.getEnd.return_value = p2

    anchor = MagicMock()
    # Case 1: anchor is on the first character (p0)
    anchor.getStart.return_value = p0
    def cmp_first(left, right):
        if left == p0 and right == p0:
            return 0
        if left == p0 and right == p2:
            return 1
        return 0
    text.compareRegionStarts.side_effect = cmp_first
    assert _anchor_point_in_match(text, anchor, cursor) is True

    # Case 2: anchor is on the match end (p2)
    anchor.getStart.return_value = p2
    def cmp_end(left, right):
        if left == p2 and right == p2:
            return 0
        if left == p2 and right == p0:
            return -1
        return 0
    text.compareRegionStarts.side_effect = cmp_end
    assert _anchor_point_in_match(text, anchor, cursor) is False

    # Case 3: anchor is strictly before the match
    p_before = MagicMock()
    anchor.getStart.return_value = p_before
    def cmp_before(left, right):
        if left == p_before and right == p0:
            return 1
        return 0
    text.compareRegionStarts.side_effect = cmp_before
    assert _anchor_point_in_match(text, anchor, cursor) is False


def test_uno_len_emoji():
    from plugin.writer.page import _uno_len
    assert _uno_len("A😀") == 3
    assert _uno_len("A") == 1


def test_lower_keeps_length_name():
    import plugin.writer.page as page_mod
    assert hasattr(page_mod, "_lower_keeps_length")
    assert not hasattr(page_mod, "_casefold_keeps_length")


def test_page_break_in_table_cell_errors():
    from plugin.writer.page import PageInsertBreak

    # Anchor path: para is in text table
    doc, para = _pagebreak_doc()
    para.getPropertyValue.side_effect = lambda prop: object() if prop == "TextTable" else None
    res = PageInsertBreak().execute(SimpleNamespace(doc=doc), before_text="Anchor")
    assert res["status"] == "error"
    assert "table cell" in res["message"]
    para.setPropertyValue.assert_not_called()

    # View-cursor path: model cursor is in text table
    doc2 = MagicMock()
    controller = MagicMock()
    view_cursor = MagicMock()
    start_pos = MagicMock()
    text_obj = MagicMock()
    text_cursor = MagicMock()
    text_cursor.getPropertyValue.side_effect = lambda prop: object() if prop == "TextTable" else None
    doc2.getCurrentController.return_value = controller
    controller.getViewCursor.return_value = view_cursor
    view_cursor.getStart.return_value = start_pos
    view_cursor.getText.return_value = text_obj
    text_obj.createTextCursorByRange.return_value = text_cursor

    ctx = SimpleNamespace(doc=doc2)
    res2 = PageInsertBreak().execute(ctx)
    assert res2["status"] == "error"
    assert "table cell" in res2["message"]
    text_cursor.setPropertyValue.assert_not_called()


def test_page_break_page_style_in_table_cell_still_errors():
    from plugin.writer.page import PageInsertBreak

    # Anchor path: para is in text table
    doc, para = _pagebreak_doc()
    _attach_page_styles(doc, {"Annex"})
    para.getPropertyValue.side_effect = lambda prop: object() if prop == "TextTable" else None
    res = PageInsertBreak().execute(
        SimpleNamespace(doc=doc),
        before_text="Anchor",
        page_style="Annex",
        restart_page_number=1,
    )
    assert res["status"] == "error"
    assert "table cell" in res["message"]
    para.setPropertyValue.assert_not_called()

    # View-cursor path: model cursor is in text table
    import sys
    setattr(sys.modules["com.sun.star.style.BreakType"], "PAGE_BEFORE", 4)
    doc2 = MagicMock()
    _attach_page_styles(doc2, {"Annex"})
    controller = MagicMock()
    view_cursor = MagicMock()
    start_pos = MagicMock()
    text_obj = MagicMock()
    text_cursor = MagicMock()
    text_cursor.getPropertyValue.side_effect = lambda prop: object() if prop == "TextTable" else None
    doc2.getCurrentController.return_value = controller
    controller.getViewCursor.return_value = view_cursor
    view_cursor.getStart.return_value = start_pos
    view_cursor.getText.return_value = text_obj
    text_obj.createTextCursorByRange.return_value = text_cursor

    ctx = SimpleNamespace(doc=doc2)
    res2 = PageInsertBreak().execute(ctx, page_style="Annex")
    assert res2["status"] == "error"
    assert "table cell" in res2["message"]
    text_cursor.setPropertyValue.assert_not_called()


