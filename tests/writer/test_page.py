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
    _disable_blocked_by_content,
    _region_holds_content,
    _scan_region_content,
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

    with patch.object(sys.modules["uno"], "isSame", side_effect=lambda a, b: {a, b} == {text_obj, other}, create=True):
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


def test_page_uno_skips_windows_leftover_hidden_xtext() -> None:
    """GHA 34689136372: leftover Hidden _blank hung in xtext_to_content."""
    from pathlib import Path

    writer = Path(__file__).parent
    for name in ("test_page_header_html_uno.py", "test_page_uno.py"):
        src = (writer / name).read_text(encoding="utf-8")
        assert "skip_windows_leftover_hidden_load" in src, name
        assert "page_header Hidden _blank xtext_to_content" in src, name
        assert "34689136372" in src, name
