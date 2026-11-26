import sys
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from plugin.tests.testing_utils import TestingFactory
from plugin.writer import html_import_styles

from plugin.writer.page import (
    PageGetHeaderFooterText,
    PageGetStyleProperties,
    PageReplaceHeaderFooterText,
    PageSetStyleProperties,
    PageSetHeaderFooterText,
    PageSetColumns,
    PageInsertBreak,
    _disable_blocked_by_content,
    _region_graphics_report,
    _region_holds_content,
    _region_mirrors_shared,
    _region_shapes,
    _scan_region_content,
    copy_page_style,
)


@pytest.fixture(autouse=True)
def _stub_break_type(monkeypatch: pytest.MonkeyPatch) -> None:
    # page.py imports PAGE_BEFORE at call time; the shared shell has none. 4 is the real UNO value.
    monkeypatch.setitem(
        sys.modules,
        "com.sun.star.style.BreakType",
        SimpleNamespace(PAGE_BEFORE=4),
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
    with patch("plugin.writer.page.replace_xtext_with_html", return_value=html_import_styles.WriteReport()) as replace_html:
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
    controller.getSelection.return_value = None
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
    from unittest.mock import call
    doc = MagicMock()
    controller = MagicMock()
    view_cursor = MagicMock()
    start_pos = MagicMock()
    text_obj = MagicMock()
    text_cursor = MagicMock()

    doc.getCurrentController.return_value = controller
    controller.getSelection.return_value = None
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
    text_cursor.gotoNextParagraph.assert_not_called()
    assert [c.args[0] for c in text_cursor.getPropertyValue.call_args_list] == ["TextTable"]


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

    with patch("plugin.writer.page.xtext_to_content",
               return_value='<p>ESCRITORIO ZOLET <span title="page-number"/></p>') as export:
        res = PageGetHeaderFooterText().execute(
            TestingFactory.create_context(doc=doc, doc_type="writer"),
            style="Standard", region="header", include_images=True)

    export.assert_called_once()
    assert res["status"] == "ok"
    assert res["format"] == "html"
    assert "page-number" in res["content"]
    assert res["images"] == ["TIMBRE"]
    assert res["fields"] == [{"presentation": "1", "content": "Page Number"}]
    assert res["paragraph_count"] == 2
    assert "warning" not in res
    assert "apply_content" not in str(res)


def test_get_header_propagates_export_error_and_disposal():
    from plugin.framework.errors import DocumentDisposedError, ToolExecutionError

    text_obj = MagicMock()
    doc, _style = _page_style_doc(text_obj)
    ctx = TestingFactory.create_context(doc=doc, doc_type="writer")
    with (
        patch("plugin.writer.page.xtext_to_content", side_effect=ToolExecutionError("Could not read the document: boom")),
        pytest.raises(ToolExecutionError, match="boom"),
    ):
        PageGetHeaderFooterText().execute(ctx, style="Standard", region="header", include_images=True)
    with (
        patch(
            "plugin.writer.page.xtext_to_content",
            side_effect=DocumentDisposedError("closed", object_type="document"),
        ),
        pytest.raises(DocumentDisposedError),
    ):
        PageGetHeaderFooterText().execute(ctx, style="Standard", region="header", include_images=True)


def test_set_header_footer_imports_html_even_when_a_logo_is_present():
    """HTML set with a logo must succeed — no force, no refuse-on-held."""
    from unittest.mock import patch

    text_obj = MagicMock()
    text_obj.createEnumeration.side_effect = lambda: _enum_of([_paragraph([_portion("Frame")])])
    doc, style = _page_style_doc(text_obj, shapes=[_logo_anchored_in(text_obj)])
    html = '<p>ESCRITORIO ZOLET <img src="data:image/png;base64,xx" alt="TIMBRE"/></p>'

    with patch("plugin.writer.page.replace_xtext_with_html", return_value=html_import_styles.WriteReport()) as replace_html:
        res = PageSetHeaderFooterText().execute(
            TestingFactory.create_context(doc=doc, doc_type="writer"),
            style="Standard", region="header", content=html)

    assert res["status"] == "ok"
    replace_html.assert_called_once()
    text_obj.setString.assert_not_called()
    style.setPropertyValue.assert_any_call("HeaderIsOn", True)


def test_set_header_footer_reports_unapplied_data_lo_para():
    """Names the import could not apply come back as warnings on the ok result."""
    from unittest.mock import patch

    text_obj = MagicMock()
    text_obj.createEnumeration.side_effect = lambda: _enum_of([_paragraph([_portion("Text")])])
    doc, _style = _page_style_doc(text_obj)
    html = '<p data-lo-para="margin-left:1cm; font-weight:bold">Header</p>'

    with patch("plugin.writer.page.replace_xtext_with_html", return_value=html_import_styles.WriteReport(unapplied_para=["font-weight"])):
        res = PageSetHeaderFooterText().execute(
            TestingFactory.create_context(doc=doc, doc_type="writer"),
            style="Standard", region="header", content=html)

    assert res["status"] == "ok"
    assert res["warnings"] == [
        {"code": "PARA_PROPERTY_UNSUPPORTED", "message": "data-lo-para property 'font-weight' was not applied"}
    ]


def test_set_header_footer_imports_field_html_without_force():
    """A footer with a page-number field is a normal HTML set, not a refuse."""
    from unittest.mock import patch

    text_obj = MagicMock()
    text_obj.createEnumeration.side_effect = lambda: _enum_of([
        _paragraph([_portion("Text"), _portion("TextField", _page_number_field())]),
    ])
    doc, _style = _page_style_doc(text_obj)
    html = '<p>Confidential | <span title="page-number"/></p>'

    with patch("plugin.writer.page.replace_xtext_with_html", return_value=html_import_styles.WriteReport()) as replace_html:
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

    with patch("plugin.writer.page.replace_xtext_with_html", return_value=html_import_styles.WriteReport()) as replace_html:
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

    with patch("plugin.writer.page.replace_xtext_with_html", return_value=html_import_styles.WriteReport()) as replace_html:
        res = PageSetHeaderFooterText().execute(
            TestingFactory.create_context(doc=doc, doc_type="writer"),
            style="Standard", region="footer", content="Rua Exemplo, 123")

    assert res["status"] == "ok"
    style.setPropertyValue.assert_any_call("FooterIsOn", True)
    replace_html.assert_called_once()
    text_obj.setString.assert_not_called()


def test_scan_region_warns_when_portion_cap_is_hit(monkeypatch):
    """A header walk that hits the cap warns, so the fields past the cap are not dropped silently."""
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
    with patch("plugin.writer.page.xtext_to_content", return_value="<p>plain</p>"):
        res = PageGetHeaderFooterText().execute(
            TestingFactory.create_context(doc=doc, doc_type="writer"),
            style="Standard", region="header", include_images=True)

    assert res["status"] == "ok"
    assert "cap 1" in res["warning"]


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

    with patch.object(sys.modules["uno"], "isSame", side_effect=lambda a, b: {a, b} == {header, other}):
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
        patch.object(sys.modules["uno"], "isSame", side_effect=lambda a, b: {a, b} == {text_obj, other}),
        patch("plugin.writer.page.xtext_to_content", return_value=""),
    ):
        res = PageGetHeaderFooterText().execute(
            TestingFactory.create_context(doc=doc, doc_type="writer"), style="Standard", region="header", include_images=True)

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


def test_scan_region_skips_table_element_and_marks_has_table():
    doc = MagicMock()
    table = MagicMock()
    table.supportsService.side_effect = lambda s: s == "com.sun.star.text.TextTable"
    table.createEnumeration.side_effect = AttributeError("no enumeration")
    text_obj = MagicMock()
    text_obj.getString.return_value = ""
    text_obj.createEnumeration.side_effect = lambda: _enum_of([table])
    scan = _scan_region_content(doc, text_obj)
    assert scan["has_table"] is True
    assert scan["paragraph_count"] == 1
    assert _region_holds_content(doc, text_obj) is True


def test_set_style_properties_refuses_table_only_header_with_content_message():
    table = MagicMock()
    table.supportsService.side_effect = lambda s: s == "com.sun.star.text.TextTable"
    table.createEnumeration.side_effect = AttributeError("no enumeration")
    text_obj = MagicMock()
    text_obj.getString.return_value = ""
    text_obj.createEnumeration.side_effect = lambda: _enum_of([table])
    doc, _style = _page_style_doc(text_obj)
    res = PageSetStyleProperties().execute(
        TestingFactory.create_context(doc=doc, doc_type="writer"),
        style="Standard",
        header_is_on=False,
    )
    assert res["status"] == "error"
    assert "still has content" in res["message"]
    assert "reading" not in res["message"]


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


# ---- D5: page break happy paths + last-paragraph edge ------------------------

def _pagebreak_doc(found_next=True):
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
    res = PageInsertBreak().execute(SimpleNamespace(doc=doc), before_text="Assinaturas", occurrence=0)
    assert res["status"] == "ok" and "before" in res["message"]
    assert doc.createSearchDescriptor.return_value.SearchCaseSensitive is True
    assert res["matched_case_sensitive"] is True
    para.setPropertyValue.assert_called_once_with("BreakType", 4)
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
        occurrence=0,
    )
    assert res["status"] == "ok"
    assert "before" in res["message"]
    assert set(res) == {"status", "message", "page_style", "matched_case_sensitive", "matched_folded"}
    assert res["page_style"] == "Annex"
    assert res["matched_case_sensitive"] is True
    para.setPropertyValue.assert_has_calls([call("BreakType", 4), call("PageDescName", "Annex")])
    assert para.setPropertyValue.call_count == 2
    para.gotoNextParagraph.assert_not_called()
    assert [c.args[0] for c in para.getPropertyValue.call_args_list] == ["TextTable"]


def test_page_break_after_text_breaks_on_following_paragraph():
    from plugin.writer.page import PageInsertBreak

    doc, para = _pagebreak_doc(found_next=True)
    res = PageInsertBreak().execute(SimpleNamespace(doc=doc), after_text="clausula", occurrence=0)
    assert res["status"] == "ok" and "after" in res["message"]
    para.gotoNextParagraph.assert_called_once()
    para.setPropertyValue.assert_called_once_with("BreakType", 4)


@pytest.mark.parametrize("restart_num", [1, 32767])
def test_page_break_restart_sets_page_number_offset(restart_num):
    from unittest.mock import call
    from plugin.writer.page import PageInsertBreak

    doc, para = _pagebreak_doc(found_next=True)
    _attach_page_styles(doc, {"Annex"})
    res = PageInsertBreak().execute(
        SimpleNamespace(doc=doc),
        after_text="clausula",
        page_style="Annex",
        restart_page_number=restart_num,
        occurrence=0,
    )
    assert res["status"] == "ok"
    assert "after" in res["message"]
    assert set(res) == {"status", "message", "page_style", "restart_page_number", "matched_case_sensitive", "matched_folded"}
    assert res["page_style"] == "Annex"
    assert res["restart_page_number"] == restart_num
    assert res["matched_case_sensitive"] is True
    para.gotoNextParagraph.assert_called_once()
    para.setPropertyValue.assert_has_calls([
        call("BreakType", 4),
        call("PageDescName", "Annex"),
        call("PageNumberOffset", restart_num),
    ])
    assert para.setPropertyValue.call_count == 3


def test_page_break_after_text_last_paragraph_errors_instead_of_wrong_direction():
    from plugin.writer.page import PageInsertBreak

    doc, para = _pagebreak_doc(found_next=False)
    res = PageInsertBreak().execute(SimpleNamespace(doc=doc), after_text="assinatura final", occurrence=0)
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
        occurrence=0,
    )
    assert res["status"] == "error"
    assert "last paragraph" in res["message"]
    para.setPropertyValue.assert_not_called()

# ---- D5: insert_page_break anchored -----------------------------------------

@pytest.mark.parametrize(
    "extra",
    [
        pytest.param({}, id="plain"),
        pytest.param({"page_style": "Nope", "restart_page_number": 1}, id="with_page_style"),
    ],
)
def test_page_break_both_anchors_rejected(extra):
    from plugin.writer.page import PageInsertBreak

    doc = MagicMock()
    res = PageInsertBreak().execute(SimpleNamespace(doc=doc), before_text="a", after_text="b", **extra)
    assert res["status"] == "error" and "only one" in res["message"]
    doc.getStyleFamilies.assert_not_called()


def test_page_break_anchor_not_found():
    from plugin.writer.page import PageInsertBreak
    doc = MagicMock()
    doc.getDrawPage.return_value = None
    doc.findFirst.return_value = None
    # com.sun.star.style.BreakType is a mocked module; the import inside execute resolves via the uno mock.
    res = PageInsertBreak().execute(SimpleNamespace(doc=doc), before_text="Signature", occurrence=0)
    assert res["status"] == "error"
    assert res["code"] == "OLD_CONTENT_NOT_FOUND"
    assert "before_text not found in document" in res["message"]


def test_page_break_anchor_occurrence_out_of_range():
    from plugin.writer.page import PageInsertBreak
    doc = MagicMock()
    m1 = MagicMock()
    doc.findFirst.return_value = m1
    doc.findNext.return_value = None
    res = PageInsertBreak().execute(SimpleNamespace(doc=doc), before_text="Signature", occurrence=1)
    assert res["status"] == "error"
    assert res["code"] == "OCCURRENCE_OUT_OF_RANGE"
    assert "use 0..0" in res["message"]


def test_page_break_anchor_strips_markup_before_find():
    from plugin.writer.page import PageInsertBreak
    from plugin.writer.search import escape_for_lo_regex

    doc, para = _pagebreak_doc()
    res = PageInsertBreak().execute(SimpleNamespace(doc=doc), before_text="<b>Assinaturas</b>", occurrence=0)
    assert res["status"] == "ok"
    assert doc.createSearchDescriptor.return_value.SearchString == escape_for_lo_regex("Assinaturas", fold=False)


def test_page_break_unknown_page_style_changes_nothing():
    from plugin.writer.page import PageInsertBreak

    doc, para = _pagebreak_doc()
    _attach_page_styles(doc, {"Standard"})
    res = PageInsertBreak().execute(
        SimpleNamespace(doc=doc),
        before_text="Assinaturas",
        page_style="NoSuch",
        occurrence=0,
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
        occurrence=0,
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
            occurrence=0,
        )
        assert res["status"] == "error"
        assert res["message"] == "restart_page_number must be an integer from 1 to 32767."
        para.setPropertyValue.assert_not_called()


def test_page_break_page_style_must_be_a_string():
    from plugin.writer.page import PageInsertBreak

    doc, para = _pagebreak_doc()
    res = PageInsertBreak().execute(
        SimpleNamespace(doc=doc),
        before_text="Assinaturas",
        page_style=123,
        occurrence=0,
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
        occurrence=0,
    )
    assert res["status"] == "error"
    assert res["message"] == "restart_page_number requires page_style."
    doc.getStyleFamilies.assert_not_called()


def test_region_graphics_report_counts_and_skips():
    """AT_PARAGRAPH frames are not TextGraphicObject; they and shortfalls are reported."""
    src = MagicMock()
    dest = MagicMock()
    g1 = MagicMock()
    g1.supportsService.return_value = True
    g1.getName.return_value = "logo1"
    g2 = MagicMock()
    g2.supportsService.return_value = True
    g2.getName.return_value = "logo2"
    frame = MagicMock()
    frame.supportsService.return_value = False
    frame.getName.return_value = "frame"
    dest_g = MagicMock()
    dest_g.supportsService.return_value = True
    doc = MagicMock()

    with patch(
        "plugin.writer.page._region_shapes",
        side_effect=[([(g1, MagicMock()), (g2, MagicMock()), (frame, MagicMock())], False), ([(dest_g, MagicMock())], False)],
    ):
        copied, skipped = _region_graphics_report(doc, src, dest)

    assert copied == 1
    assert skipped == ["frame", "1 image(s)"]


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
        "plugin.writer.styles.copy_direct_style_properties",
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

    with patch("plugin.writer.page.copy_flow_into_xtext", return_value=1) as mock_flow, \
         patch("plugin.writer.page.xtext_to_content") as mock_xtext_to_content, \
         patch("plugin.writer.page.replace_xtext_with_html") as mock_replace_html:
        _copy_page_regions(doc, source, clone)

    mock_flow.assert_called_once_with(doc, src_text, dest_text)
    mock_xtext_to_content.assert_not_called()
    mock_replace_html.assert_not_called()


# --- header/footer: replace one substring, leave fields and graphics -----------


class _ReplaceCursor:
    """goRight cursor over a portion-index string. ``\\x01`` is one field step."""

    def __init__(self, hay, pos=0):
        self.hay = hay
        self.pos = pos
        self.anchor = pos
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


def _text_portion(text, start=0):
    portion = _portion("Text")
    portion.getString.return_value = text
    portion.getStart.return_value = start
    return portion


def _replace_doc(portions, shapes=(), hay=""):
    """Page style whose footer enumerates *portions*. Cursors walk *hay*."""
    text_obj = MagicMock()
    text_obj.createEnumeration.side_effect = lambda: _enum_of([_paragraph(portions)])
    text_obj.createTextCursorByRange.side_effect = lambda rng: _ReplaceCursor(hay, rng if isinstance(rng, int) else 0)
    doc, _style = _page_style_doc(text_obj, shapes=shapes)
    return doc, text_obj


def _cursors_for(text_obj, hay):
    """Record every cursor the replace tool builds so tests can see setString."""
    seen = []

    def make(rng):
        cursor = _ReplaceCursor(hay, rng if isinstance(rng, int) else 0)
        seen.append(cursor)
        return cursor

    text_obj.createTextCursorByRange.side_effect = make
    return seen


def _replace(doc, **kwargs):
    args = {"style": "Standard", "region": "footer", "old_content": "Annex", "content": "Appendix", "all_matches": False}
    args.update(kwargs)
    return PageReplaceHeaderFooterText().execute(SimpleNamespace(doc=doc), **args)


def test_replace_header_footer_rejects_bad_args():
    doc = MagicMock()
    tool = PageReplaceHeaderFooterText()
    both = tool.execute(
        SimpleNamespace(doc=doc), style="Standard", region="footer", old_content="Annex", content="A",
        occurrence=1, all_matches=True,
    )
    assert both["status"] == "error" and "all_matches" in both["message"]
    neg = tool.execute(
        SimpleNamespace(doc=doc), style="Standard", region="footer", old_content="Annex", content="A",
        occurrence=-1, all_matches=False,
    )
    assert neg["status"] == "error" and "occurrence must be a non-negative integer." in neg["message"]
    empty = tool.execute(SimpleNamespace(doc=doc), style="Standard", region="footer", old_content="", content="A", all_matches=False)
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


def test_fold_case_sensitivity():
    from plugin.writer.page import _fold

    # Default / insensitive: lowercases matching length
    assert _fold("Hello World", "hello", case_sensitive=False) == ("hello world", "hello")
    # Case-sensitive: keeps exact casing
    assert _fold("Hello World", "hello", case_sensitive=True) == ("Hello World", "hello")
    # Length-changing lower (Turkish İ lowers to i + combining dot, changing length)
    # stays exact compare to keep character offset alignments accurate
    turkish_i = "\u0130"
    assert _fold(turkish_i, turkish_i, case_sensitive=False) == (turkish_i, turkish_i)


def test_replace_header_footer_default_case_insensitive():
    portions = [_text_portion("Hello World")]
    hay = "Hello World"
    doc, text_obj = _replace_doc(portions, hay=hay)
    seen = _cursors_for(text_obj, hay)
    res = _replace(doc, old_content="hello", content="Hi")
    assert res["status"] == "ok"
    assert res["replaced_count"] == 1
    assert res["matched_case_sensitive"] is False
    assert len(seen) == 1 and seen[0].written == "Hi"


def test_replace_header_footer_after_a_field_starts_at_its_portion():
    """A match after a field starts at that portion rather than counting earlier steps."""
    hay = "Annex \x01 of 274"
    portions = [_text_portion("Annex ", start=0), _portion("TextField", _page_number_field()), _text_portion(" of 274", start=7)]
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
    assert seen[0].anchor == 11


def test_replace_header_footer_emoji_all_matches_uses_cursor_indexes():
    hay = "😀 x 😀 y 😀"
    portions = [_text_portion(hay)]
    doc, text_obj = _replace_doc(portions, hay=hay)
    seen = _cursors_for(text_obj, hay)
    res = _replace(doc, old_content="😀", content="🎉", all_matches=True)
    assert res["status"] == "ok", res
    assert [c.anchor for c in seen] == [0, 4, 8]
    assert res["replaced_count"] == 3


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


def test_replace_header_footer_undo_leak_says_text_was_written():
    doc, text_obj = _replace_doc([_text_portion("Annex")], hay="Annex")
    seen = _cursors_for(text_obj, "Annex")
    um = doc.getUndoManager.return_value
    um.isLocked.return_value = False
    um.leaveUndoContext.side_effect = RuntimeError("leave failed")
    res = _replace(doc)
    assert res["status"] == "error"
    assert res["code"] == "UNDO_CONTEXT_LEAK"
    assert "the replacement was written" in res["message"]
    assert "leave failed" in res["message"]
    assert [c.written for c in seen] == ["Appendix"]


def test_replace_header_footer_refused_write_is_not_reported_as_written():
    doc, text_obj = _replace_doc([_text_portion("Annex")], hay="Annex")
    failing_cursor = MagicMock()
    failing_cursor.getString.return_value = "Annex"
    failing_cursor.setString.side_effect = RuntimeError("write boom")
    text_obj.createTextCursorByRange.side_effect = lambda _rng: failing_cursor
    um = doc.getUndoManager.return_value
    um.isLocked.return_value = False
    um.getAllUndoActionTitles.side_effect = [(), ()]
    res = _replace(doc)
    assert res["status"] == "error"
    assert "write boom" in res["message"]
    assert "could not be rolled back" not in res["message"]
    assert res.get("code") != "ROLLBACK_FAILED"


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

    res = PageSetStyleProperties().execute(ctx, style="Standard", width_mm=2.3)
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

    res = PageSetStyleProperties().execute(ctx, style="Standard", width_mm=10, height_mm=20)
    assert res["status"] == "error"
    assert res["code"] == "TOOL_EXECUTION_ERROR"
    assert res["updated"] == ["width"]
    assert "height" in res["failed"]


def test_set_page_style_bad_mm_writes_nothing():
    from plugin.writer.page import PageSetStyleProperties
    doc = MagicMock()
    style = MagicMock()
    doc.getStyleFamilies().getByName().getByName.return_value = style
    ctx = SimpleNamespace(doc=doc)

    with pytest.raises(ValueError, match="non-negative"):
        PageSetStyleProperties().execute(ctx, style="Standard", width_mm=-1)
    style.setPropertyValue.assert_not_called()


def test_is_landscape_flag_only_restores_size():
    from plugin.writer.page import PageSetStyleProperties
    doc = MagicMock()
    style = MagicMock()
    style.getPropertyValue.side_effect = lambda prop: 21000 if prop == "Width" else (29700 if prop == "Height" else None)
    doc.getStyleFamilies().getByName().getByName.return_value = style
    ctx = SimpleNamespace(doc=doc)

    res = PageSetStyleProperties().execute(ctx, style="Standard", is_landscape=True)
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

    res = PageSetStyleProperties().execute(ctx, style="Standard", is_landscape=True, width_mm=297, height_mm=210)
    assert res["status"] == "ok"
    calls = [c.args for c in style.setPropertyValue.call_args_list]
    props = [c[0] for c in calls]
    assert "IsLandscape" in props
    assert props.index("IsLandscape") < props.index("Width")
    assert props.index("IsLandscape") < props.index("Height")
    style.setPropertyValue.assert_any_call("Width", 29700)
    style.setPropertyValue.assert_any_call("Height", 21000)


def test_page_style_layout_read_and_write(monkeypatch: pytest.MonkeyPatch):
    from plugin.writer.page import get_page_style_properties, PageSetStyleProperties

    psl = SimpleNamespace(ALL=0, LEFT=1, RIGHT=2, MIRRORED=3)
    monkeypatch.setitem(sys.modules, "com.sun.star.style.PageStyleLayout", psl)

    doc = MagicMock()
    style = MagicMock()
    layout_enum = SimpleNamespace(value="ALL")
    style.getPropertyValue.side_effect = lambda prop: layout_enum if prop == "PageStyleLayout" else 21000
    doc.getStyleFamilies().getByName().getByName.return_value = style
    res = get_page_style_properties(doc, "Standard")
    assert res["properties"]["page_style_layout"] == 0

    ctx = SimpleNamespace(doc=doc)
    with pytest.raises(ValueError, match="page_style_layout"):
        PageSetStyleProperties().execute(ctx, style="Standard", page_style_layout=4)
    style.setPropertyValue.assert_not_called()

    res_ok = PageSetStyleProperties().execute(ctx, style="Standard", page_style_layout=1)
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

    res = PageSetHeaderFooterText().execute(ctx, style="Standard", region="header_first", content="<p>Test</p>")
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

    with patch("plugin.writer.page.replace_xtext_with_html", side_effect=RuntimeError("import failed")):
        res = PageSetHeaderFooterText().execute(ctx, style="Standard", region="header", content="<p>Test</p>")
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

    res = PageSetColumns().execute(ctx, style="Standard", column_count=2, spacing_mm=0)
    assert res["status"] == "ok"
    assert col1.RightMargin == 0
    assert col2.LeftMargin == 0
    text_columns.setColumns.assert_called_once()


@pytest.mark.parametrize(
    ("kwargs", "expected_param"),
    [
        ({"column_count": 0}, "column_count"),
        ({"column_count": 100}, "column_count"),
        ({"column_count": 2, "spacing_mm": -1}, "spacing_mm"),
        ({"column_count": 2, "spacing_mm": float("nan")}, "spacing_mm"),
        ({"column_count": 2, "spacing_mm": float("inf")}, "spacing_mm"),
    ],
)
def test_page_set_columns_rejects_bad_values_before_touching_doc(
    kwargs: dict[str, object], expected_param: str
) -> None:
    doc = MagicMock()
    ctx = SimpleNamespace(doc=doc)

    res = PageSetColumns().execute(ctx, **{"style": "Standard", "spacing_mm": 0, **kwargs})
    assert res["status"] == "error"
    assert res["code"] == "INVALID_PARAM"
    assert res["details"]["parameter"] == expected_param
    doc.getStyleFamilies.assert_not_called()


def test_page_set_columns_accepts_cap() -> None:
    doc = MagicMock()
    style = MagicMock()
    text_columns = MagicMock()
    text_columns.getColumns.return_value = [MagicMock(RightMargin=0, LeftMargin=0) for _ in range(99)]
    style.getPropertyValue.return_value = text_columns
    doc.getStyleFamilies().getByName().getByName.return_value = style
    ctx = SimpleNamespace(doc=doc)

    res = PageSetColumns().execute(ctx, style="Standard", column_count=99, spacing_mm=0)
    assert res["status"] == "ok"
    text_columns.setColumnCount.assert_called_once_with(99)


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

    res = PageGetColumns().execute(ctx, style="Standard")
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


def test_page_break_in_table_cell_errors():
    from plugin.writer.page import PageInsertBreak

    # Anchor path: para is in text table
    doc, para = _pagebreak_doc()
    para.getPropertyValue.side_effect = lambda prop: object() if prop == "TextTable" else None
    res = PageInsertBreak().execute(SimpleNamespace(doc=doc), before_text="Anchor", occurrence=0)
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
    controller.getSelection.return_value = None
    controller.getViewCursor.return_value = view_cursor
    view_cursor.getStart.return_value = start_pos
    view_cursor.getText.return_value = text_obj
    text_obj.createTextCursorByRange.return_value = text_cursor

    ctx = SimpleNamespace(doc=doc2, selection_pin=None)
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
        occurrence=0,
    )
    assert res["status"] == "error"
    assert "table cell" in res["message"]
    para.setPropertyValue.assert_not_called()

    # View-cursor path: model cursor is in text table
    doc2 = MagicMock()
    _attach_page_styles(doc2, {"Annex"})
    controller = MagicMock()
    view_cursor = MagicMock()
    start_pos = MagicMock()
    text_obj = MagicMock()
    text_cursor = MagicMock()
    text_cursor.getPropertyValue.side_effect = lambda prop: object() if prop == "TextTable" else None
    doc2.getCurrentController.return_value = controller
    controller.getSelection.return_value = None
    controller.getViewCursor.return_value = view_cursor
    view_cursor.getStart.return_value = start_pos
    view_cursor.getText.return_value = text_obj
    text_obj.createTextCursorByRange.return_value = text_cursor

    ctx = SimpleNamespace(doc=doc2, selection_pin=None)
    res2 = PageInsertBreak().execute(ctx, page_style="Annex")
    assert res2["status"] == "error"
    assert "table cell" in res2["message"]
    text_cursor.setPropertyValue.assert_not_called()


def test_scan_region_content_raises_on_unreadable_portion():
    """Unreadable portions in a header/footer region enumeration must raise.

    The region scanner does not silently ignore portion read failures. Callers
    that tolerate partial reads decide when to wrap the walk in uno_read.
    """
    portion = MagicMock()
    portion.getPropertyValue.side_effect = RuntimeError("bridge hiccup")
    text_obj = MagicMock()
    text_obj.createEnumeration.side_effect = lambda: _enum_of([_paragraph([portion])])
    doc = MagicMock()
    with pytest.raises(RuntimeError, match="Unreadable text portion"):
        _scan_region_content(doc, text_obj)


def test_get_header_footer_omits_scan_extras_when_draw_page_unreadable():
    """An unreadable draw page drops extra scan metadata without failing the get tool.

    When reading shapes from the draw page raises an error, the scan falls back
    to an empty scan rather than returning partially populated extras.
    """
    portion = _portion("TextField", field=_page_number_field())
    text_obj = MagicMock()
    text_obj.createEnumeration.side_effect = lambda: _enum_of([_paragraph([portion])])
    doc, _style = _page_style_doc(text_obj)
    doc.getDrawPage.side_effect = RuntimeError("bridge hiccup")
    ctx = TestingFactory.create_context(doc=doc, doc_type="writer")
    with patch("plugin.writer.page.xtext_to_content", return_value="<p>x</p>"):
        res = PageGetHeaderFooterText().execute(ctx, style="Standard", region="header", include_images=True)
    assert res["status"] == "ok"
    assert res["content"] == "<p>x</p>"
    assert "fields" not in res
    assert "images" not in res
    assert res["paragraph_count"] == 0


def test_get_header_footer_reports_disposed_region_text():
    """Accessing region text on a disposed document returns DOCUMENT_DISPOSED."""
    class DisposedException(Exception):
        pass

    text_obj = MagicMock()
    doc, style = _page_style_doc(text_obj)

    def props(name):
        if name == "HeaderText":
            raise DisposedException("closed")
        if name.endswith("IsOn"):
            return True
        return text_obj

    style.getPropertyValue.side_effect = props
    ctx = TestingFactory.create_context(doc=doc, doc_type="writer")
    res = PageGetHeaderFooterText().execute_safe(ctx, style="Standard", region="header", include_images=True)
    assert res["status"] == "error"
    assert res["code"] == "DOCUMENT_DISPOSED"


def test_replace_reports_disposed_instead_of_region_off():
    """A disposed document returns DOCUMENT_DISPOSED rather than reporting the region is off.

    When reading the region's IsOn flag fails due to document disposal, uno_read raises
    so the tool reports document disposal instead of claiming the region is turned off.
    """
    class DisposedException(Exception):
        pass

    text_obj = MagicMock()
    doc, style = _page_style_doc(text_obj)

    def props(name):
        if name == "FooterIsOn":
            raise DisposedException("closed")
        return text_obj

    style.getPropertyValue.side_effect = props
    ctx = TestingFactory.create_context(doc=doc, doc_type="writer")
    res = PageReplaceHeaderFooterText().execute_safe(
        ctx, style="Standard", region="footer", old_content="a", content="b", all_matches=False
    )
    assert res["status"] == "error"
    assert res["code"] == "DOCUMENT_DISPOSED"
    assert "is off" not in res["message"]


def test_replace_refuses_when_draw_page_unreadable():
    """An unreadable draw page fails the replace tool before modifying any content.

    If the draw page cannot be read, the anchor check cannot verify whether an image
    would be affected. The operation fails closed and no text is written.
    """
    doc, text_obj = _replace_doc([_text_portion("Annex")], hay="Annex")
    seen = _cursors_for(text_obj, "Annex")
    doc.getDrawPage.side_effect = RuntimeError("bridge hiccup")
    ctx = TestingFactory.create_context(doc=doc, doc_type="writer")
    res = PageReplaceHeaderFooterText().execute_safe(
        ctx, style="Standard", region="footer", old_content="Annex", content="B", all_matches=False
    )
    assert res["status"] == "error"
    assert res["code"] == "TOOL_EXECUTION_ERROR"
    assert seen and all(c.written is None for c in seen)


def test_region_shapes_snapshot_skips_page_anchored_shape():
    """Page-anchored shapes lack an anchor text object and are skipped.

    Only shapes anchored in the given region XText are returned in the snapshot list.
    """
    text_obj = MagicMock()
    page_shape = MagicMock()
    page_shape.getAnchor.return_value = None
    anchored_shape = _logo_anchored_in(text_obj)
    doc = MagicMock()
    draw_page = MagicMock()
    draw_page.getCount.return_value = 2
    draw_page.getByIndex.side_effect = lambda i: [page_shape, anchored_shape][i]
    doc.getDrawPage.return_value = draw_page
    shapes, struck = _region_shapes(doc, text_obj)
    assert isinstance(shapes, list)
    assert struck is False
    assert len(shapes) == 1
    assert shapes[0][0] is anchored_shape


def test_copy_page_style_unreadable_region_copies_as_empty_and_disposal_raises():
    """An unreadable region walk copies as empty; document disposal raises.

    When cloning a page style whose region cannot be walked, the region is copied
    as empty rather than failing the clone. If the failure is due to document
    disposal, uno_read raises DocumentDisposedError immediately.
    """
    from plugin.framework.errors import DocumentDisposedError

    class DisposedException(Exception):
        pass

    src_text = MagicMock()
    src_text.getString.return_value = ""
    src_text.createEnumeration.side_effect = lambda: _enum_of(
        [_paragraph([_portion("TextField", field=_page_number_field())])]
    )

    dest_text = MagicMock()
    dest_text.getString.return_value = ""

    source = MagicMock()
    source.getParentStyle.return_value = ""

    def _src_get_prop(name: str):
        if name == "HeaderIsOn":
            return True
        if name.endswith("IsOn"):
            return False
        if name == "HeaderText":
            return src_text
        return 0

    source.getPropertyValue.side_effect = _src_get_prop

    clone = MagicMock()

    def _clone_get_prop(name: str):
        if name == "HeaderText":
            return dest_text
        return 0

    clone.getPropertyValue.side_effect = _clone_get_prop

    family = MagicMock()
    family.hasByName.side_effect = lambda name: name == "Src"
    family.getByName.return_value = source
    family.getElementNames.return_value = ("Src",)

    doc = MagicMock()
    doc.getStyleFamilies.return_value.getByName.return_value = family
    doc.createInstance.return_value = clone

    with patch("plugin.writer.styles.copy_direct_style_properties", return_value=([], [])), \
         patch("plugin.writer.page.copy_flow_into_xtext") as mock_flow:
        doc.getDrawPage.side_effect = RuntimeError("bridge hiccup")
        res = copy_page_style(doc, "Src", "Dst")
        assert res["shapes_copied"] == 0
        mock_flow.assert_not_called()

        doc.getDrawPage.side_effect = DisposedException("closed")
        with pytest.raises(DocumentDisposedError):
            copy_page_style(doc, "Src", "Dst2")


def test_page_set_header_footer_text_flags_layout_change_only():
    style = MagicMock()
    props = {"HeaderIsOn": False, "HeaderIsDynamicHeight": None, "HeaderText": MagicMock()}

    def get_prop(name):
        return props[name]

    def set_prop(name, val):
        props[name] = val
        if name == "HeaderIsOn" and val and props["HeaderIsDynamicHeight"] is None:
            props["HeaderIsDynamicHeight"] = True

    style.getPropertyValue.side_effect = get_prop
    style.setPropertyValue.side_effect = set_prop

    doc = MagicMock()
    doc.getPropertyValue.side_effect = lambda name: True if name == "RecordChanges" else None
    family = MagicMock()
    family.hasByName.return_value = True
    family.getByName.return_value = style
    doc.getStyleFamilies.return_value.getByName.return_value = family

    ctx = TestingFactory.create_context(doc=doc, doc_type="writer")
    with (
        patch("plugin.writer.page.replace_xtext_with_html", return_value=MagicMock(style_skips=[])),
        patch("plugin.writer.page.attach_write_report", side_effect=lambda r, _rep: r),
        patch("plugin.writer.edit_review.get_agent_edit_review_mode", return_value="off"),
    ):
        first = PageSetHeaderFooterText().execute(ctx, style="Standard", region="header", content="<p>H1</p>")
        second = PageSetHeaderFooterText().execute(ctx, style="Standard", region="header", content="<p>H2</p>")

    assert first.get("style_unreviewed") is True
    assert "style_unreviewed" not in second


def _redline_portion(redline_type: str, is_start: bool) -> MagicMock:
    p = MagicMock()

    def _get_prop(name: str):
        if name == "TextPortionType":
            return "Redline"
        if name == "RedlineType":
            return redline_type
        if name == "IsStart":
            return is_start
        return None

    p.getPropertyValue.side_effect = _get_prop
    return p


def _field_portion(presentation: str, content: str) -> MagicMock:
    field = MagicMock()
    field.getPresentation.side_effect = lambda show_cmd: content if show_cmd else presentation
    return _portion("TextField", field=field)


def _table_mock(cells: dict[str, MagicMock], *, row_flags: tuple[bool, ...] = (True,)) -> MagicMock:
    table = MagicMock()
    table.supportsService.side_effect = lambda s: s == "com.sun.star.text.TextTable"
    table.getCellNames.return_value = tuple(cells.keys())
    table.getCellByName.side_effect = lambda name: cells[name]
    rows = MagicMock()
    rows.getCount.return_value = len(row_flags)
    row_objs = []
    for flag in row_flags:
        r = MagicMock()
        r.getPropertyValue.side_effect = lambda prop, f=flag: f if prop == "HasTextChangesOnly" else None
        row_objs.append(r)
    rows.getByIndex.side_effect = lambda i: row_objs[i]
    table.getRows.return_value = rows
    return table


def _para_mock(portions) -> MagicMock:
    para = _paragraph(portions)
    para.supportsService.return_value = False
    return para


def test_scan_region_lists_fields_in_table_cells():
    inner_cell = MagicMock()
    inner_cell.createEnumeration.side_effect = lambda: _enum_of([_para_mock([_field_portion("42", "Word count")])])
    inner_table = _table_mock({"A1": inner_cell})

    cell_a1 = MagicMock()
    cell_a1.createEnumeration.side_effect = lambda: _enum_of([_para_mock([_field_portion("1", "Page number")])])
    cell_b1 = MagicMock()
    cell_b1.createEnumeration.side_effect = lambda: _enum_of([inner_table])
    outer_table = _table_mock({"A1": cell_a1, "B1": cell_b1})

    text_obj = MagicMock()
    text_obj.createEnumeration.side_effect = lambda: _enum_of([
        _para_mock([_field_portion("Title", "Doc title")]),
        outer_table,
        _para_mock([_field_portion("5", "Page count")]),
    ])
    doc, _style = _page_style_doc(text_obj)
    scan = _scan_region_content(doc, text_obj)
    assert [f["content"] for f in scan["fields"]] == ["Doc title", "Page number", "Word count", "Page count"]
    assert scan["paragraph_count"] == 3
    assert scan["has_table"] is True


def test_scan_region_leaves_out_struck_table_and_fields():
    cell = MagicMock()
    cell.createEnumeration.side_effect = lambda: _enum_of([_para_mock([_field_portion("1", "Page number")])])
    table = _table_mock({"A1": cell})

    p1 = _para_mock([_redline_portion("Delete", True)])
    p2 = _para_mock([_field_portion("5", "Page count"), _redline_portion("Delete", False)])
    text_obj = MagicMock()
    text_obj.getString.return_value = ""
    text_obj.createEnumeration.side_effect = lambda: _enum_of([p1, table, p2])

    logo = _logo_anchored_in(cell, name="CellLogo")
    doc, _style = _page_style_doc(text_obj, shapes=(logo,))
    scan = _scan_region_content(doc, text_obj)
    assert scan["fields"] == []
    assert scan["images"] == []
    assert scan["has_table"] is False
    assert scan["has_struck"] is True
    assert scan["paragraph_count"] == 1
    assert _region_holds_content(doc, text_obj) is True


def test_scan_region_counts_first_paragraph_that_starts_struck():
    from plugin.doc.text_helpers import DeletionState

    p1 = _para_mock([_redline_portion("Delete", True), _redline_portion("Delete", False)])
    p2 = _para_mock([_field_portion("1", "Page number")])
    one_para = MagicMock()
    one_para.createEnumeration.side_effect = lambda: _enum_of([p1])
    two_paras = MagicMock()
    two_paras.createEnumeration.side_effect = lambda: _enum_of([p1, p2])
    doc, _style = _page_style_doc(one_para)

    with patch("plugin.writer.page.TrackedDeletions") as td_cls:
        td_cls.return_value.state_at.return_value = DeletionState(True)
        assert _scan_region_content(doc, one_para)["paragraph_count"] == 1
        assert _scan_region_content(doc, two_paras)["paragraph_count"] == 2


def test_scan_region_portion_cap_closes_open_deletion(monkeypatch):
    import plugin.writer.page as pg

    monkeypatch.setattr(pg, "_SCAN_PORTION_LIMIT", 1)
    p1 = _para_mock([_redline_portion("Delete", True), _redline_portion("Delete", False)])
    p2 = _para_mock([_field_portion("1", "Page number")])
    text_obj = MagicMock()
    text_obj.createEnumeration.side_effect = lambda: _enum_of([p1, p2])
    doc, _style = _page_style_doc(text_obj)
    scan = _scan_region_content(doc, text_obj)
    assert [f["content"] for f in scan["fields"]] == ["Page number"]
    assert "warning" in scan


def test_set_header_footer_keeps_error_code():
    from plugin.framework.errors import ToolExecutionError

    doc, _style = _page_style_doc(MagicMock())
    ctx = TestingFactory.create_context(doc=doc, doc_type="writer")
    with patch(
        "plugin.writer.page.replace_xtext_with_html",
        side_effect=ToolExecutionError("bad table", code="TABLE_INSERT_FAILED"),
    ):
        res = PageSetHeaderFooterText().execute(ctx, style="Standard", region="header", content="<p>x</p>")
    assert res["status"] == "error"
    assert res["code"] == "TABLE_INSERT_FAILED"


def test_replace_header_footer_codes():
    # Locked undo with two matches and all_matches -> UNDO_UNAVAILABLE, nothing written
    doc, text_obj = _replace_doc([_text_portion("Annex and Annex")], hay="Annex and Annex")
    seen = _cursors_for(text_obj, "Annex and Annex")
    doc.getUndoManager.return_value.isLocked.return_value = True
    res = _replace(doc, all_matches=True)
    assert res["status"] == "error"
    assert res["code"] == "UNDO_UNAVAILABLE"
    assert seen and all(c.written is None for c in seen)

    # Locked undo with one match and failing setString -> ROLLBACK_FAILED
    doc2, text_obj2 = _replace_doc([_text_portion("Annex")], hay="Annex")
    failing_cursor = MagicMock()
    failing_cursor.getString.return_value = "Annex"
    failing_cursor.setString.side_effect = RuntimeError("write boom")
    text_obj2.createTextCursorByRange.side_effect = lambda _rng: failing_cursor
    doc2.getUndoManager.return_value.isLocked.return_value = True
    res2 = _replace(doc2)
    assert res2["status"] == "error"
    assert res2["code"] == "ROLLBACK_FAILED"
    assert "was written" not in res2["message"]

    # setString fails and undo raises -> ROLLBACK_FAILED
    doc3, text_obj3 = _replace_doc([_text_portion("Annex")], hay="Annex")
    text_obj3.createTextCursorByRange.side_effect = lambda _rng: failing_cursor
    um3 = doc3.getUndoManager.return_value
    um3.isLocked.return_value = False
    undo_title = None

    def _enter(t):
        nonlocal undo_title
        undo_title = t

    um3.enterUndoContext.side_effect = _enter
    um3.getAllUndoActionTitles.side_effect = lambda: [undo_title]
    um3.undo.side_effect = RuntimeError("undo boom")
    res3 = _replace(doc3)
    assert res3["status"] == "error"
    assert res3["code"] == "ROLLBACK_FAILED"


@pytest.mark.parametrize(
    ("raw_color", "expected_uno"),
    [
        ("#FF0000", 0xFF0000),
        ("red", 0xFF0000),
        ("transparent", -1),
        (-1, -1),
        (2147418112, 2147418112),
    ],
)
def test_page_set_style_properties_back_color_variants(raw_color, expected_uno):
    from plugin.framework.tool import prepare_tool_call

    style = MagicMock()
    doc = MagicMock()
    family = MagicMock()
    family.hasByName.return_value = True
    family.getByName.return_value = style
    doc.getStyleFamilies.return_value.getByName.return_value = family

    ctx = TestingFactory.create_context(doc=doc, doc_type="writer")
    tool = PageSetStyleProperties()
    prepared = prepare_tool_call(tool, ctx, "page_set_style_properties", {"style": "Standard", "back_color": raw_color})
    assert prepared.error is None, prepared.error
    with patch("plugin.writer.page.flag_style_unreviewed"):
        res = tool.execute(ctx, **prepared.kwargs)

    assert res["status"] == "ok", res
    style.setPropertyValue.assert_called_once_with("BackColor", expected_uno)


def test_page_set_style_properties_invalid_back_color_writes_nothing():
    from plugin.framework.errors import ToolExecutionError
    from plugin.framework.tool import prepare_tool_call

    style = MagicMock()
    doc = MagicMock()
    family = MagicMock()
    family.hasByName.return_value = True
    family.getByName.return_value = style
    doc.getStyleFamilies.return_value.getByName.return_value = family

    ctx = TestingFactory.create_context(doc=doc, doc_type="writer")
    tool = PageSetStyleProperties()
    prepared = prepare_tool_call(
        tool,
        ctx,
        "page_set_style_properties",
        {"style": "Standard", "width_mm": 210, "back_color": "notacolor"},
    )
    assert prepared.error is None, prepared.error
    with pytest.raises(ToolExecutionError) as exc_info:
        tool.execute(ctx, **prepared.kwargs)

    assert exc_info.value.code == "INVALID_PARAM"
    style.setPropertyValue.assert_not_called()


def _cursor_portion(kind: str, text: str = "", *, is_start: bool = False, start: int = 0):
    p = MagicMock()
    p.getPropertyValue.side_effect = lambda name: (
        kind if name == "TextPortionType"
        else ("Delete" if name == "RedlineType" else is_start)
    )
    p.getString.return_value = text
    p.getStart.return_value = start
    return p


def _cursor_para(portions, *, is_table: bool = False):
    para = MagicMock()
    para.supportsService.side_effect = lambda svc: is_table and svc == "com.sun.star.text.TextTable"
    if is_table:
        para.createEnumeration.side_effect = AssertionError("TextTable has no createEnumeration")
    else:
        para.createEnumeration.return_value = _enum_of(portions)
    return para


def test_replace_header_footer_skips_struck_portions():
    from plugin.writer.page import _find_region_matches

    # Case 1: live "Annex ", struck "Annex", live " Annex" in one paragraph, driven through the tool.
    portions = [
        _cursor_portion("Text", "Annex ", start=0),
        _cursor_portion("Redline", is_start=True),
        _cursor_portion("Text", "Annex", start=6),
        _cursor_portion("Redline", is_start=False),
        _cursor_portion("Text", " Annex", start=11),
    ]
    hay = "Annex Annex Annex"
    doc, text_obj = _replace_doc(portions, hay=hay)
    seen = _cursors_for(text_obj, hay)
    res = _replace(doc, old_content="Annex", content="Appendix", all_matches=True)
    assert res["status"] == "ok" and res["replaced_count"] == 2
    assert [(c.anchor, c.pos, c.written) for c in seen] == [
        (0, 5, "Appendix"),
        (12, 17, "Appendix"),
    ]

    # Case 2: deletion opens in paragraph 1 and closes in paragraph 2.
    doc2 = MagicMock()
    doc2.getRedlines.return_value.hasElements.return_value = False
    p2a_first = _cursor_portion("Text", "Annex ", start=0)
    p2a = _cursor_para([
        p2a_first,
        _cursor_portion("Redline", is_start=True),
        _cursor_portion("Text", "Annex", start=6),
    ])
    p2b_last = _cursor_portion("Text", " Annex", start=6)
    p2b = _cursor_para([
        _cursor_portion("Text", "Annex", start=0),
        _cursor_portion("Redline", is_start=False),
        p2b_last,
    ])
    t2 = MagicMock()
    t2.createEnumeration.return_value = _enum_of([p2a, p2b])
    hits2, _ = _find_region_matches(doc2, t2, "Annex", True)
    assert [(h[0], h[1]) for h in hits2] == [(p2a_first, 0), (p2b_last, 1)]

    # Case 3: a TextTable before the paragraph is skipped without createEnumeration().
    tbl = _cursor_para([], is_table=True)
    p3_text = _cursor_portion("Text", "Annex")
    p3 = _cursor_para([p3_text])
    t3 = MagicMock()
    t3.createEnumeration.return_value = _enum_of([tbl, p3])
    hits3, _ = _find_region_matches(doc2, t3, "Annex", True)
    assert [(h[0], h[1]) for h in hits3] == [(p3_text, 0)]


def test_scan_region_content_excludes_struck_logo_but_keeps_has_struck():
    from types import SimpleNamespace
    from plugin.writer.page import _region_holds_content, _scan_region_content

    text_obj = MagicMock()
    text_obj.getString.return_value = ""
    text_obj.createEnumeration.side_effect = lambda: _enum_of([])

    shape = MagicMock()
    shape.getName.return_value = "StruckLogo"
    anchor = MagicMock()
    anchor.getText.return_value = text_obj
    shape.getAnchor.return_value = anchor
    shape.getPropertyValue.return_value = SimpleNamespace(value="AT_PARAGRAPH")

    draw_page = MagicMock()
    draw_page.getCount.return_value = 1
    draw_page.getByIndex.return_value = shape

    doc = MagicMock()
    doc.getDrawPage.return_value = draw_page
    rl = MagicMock()
    rl.getPropertyValue.side_effect = lambda name: (
        "Delete" if name == "RedlineType" else SimpleNamespace(getText=lambda: text_obj, getStart=lambda: MagicMock())
    )
    doc.getRedlines.return_value.hasElements.return_value = True
    doc.getRedlines.return_value.createEnumeration.side_effect = lambda: _enum_of([rl])
    # _deletion_removes_anchor: AT_PARAGRAPH covering the whole text_obj.
    text_obj.compareRegionStarts.return_value = 0
    text_obj.compareRegionEnds.return_value = 0

    scan = _scan_region_content(doc, text_obj)
    assert scan["images"] == []
    assert scan["has_struck"] is True
    assert _region_holds_content(doc, text_obj) is True


def test_scan_region_content_sees_nested_fields_and_redlines_in_content_control():
    from plugin.writer.page import _scan_region_content
    from tests.testing_utils import FakeUnoEnum, FakeUnoPara, FakeUnoPortion

    field = MagicMock()
    field.getPresentation.side_effect = lambda show_cmd: "Page" if show_cmd else "1"
    nested_field = MagicMock()
    nested_field.getPropertyValue.side_effect = lambda name: field if name == "TextField" else ("TextField" if name == "TextPortionType" else None)
    cc_field = FakeUnoPortion("ContentControl", inner=[nested_field])

    struck_field = MagicMock()
    struck_field.getPropertyValue.side_effect = lambda name: field if name == "TextField" else ("TextField" if name == "TextPortionType" else None)
    cc_struck = FakeUnoPortion(
        "ContentControl",
        inner=[
            FakeUnoPortion("Redline", redline_type="Delete", is_start=True),
            struck_field,
            FakeUnoPortion("Redline", redline_type="Delete", is_start=False),
        ],
    )

    para = FakeUnoPara([cc_field, cc_struck])
    text_obj = MagicMock()
    text_obj.createEnumeration.side_effect = lambda: FakeUnoEnum([para])
    doc = MagicMock()
    doc.getDrawPage.return_value.getCount.return_value = 0

    scan = _scan_region_content(doc, text_obj)
    assert scan["fields"] == [{"presentation": "1", "content": "Page"}]
    assert scan["has_struck"] is True
