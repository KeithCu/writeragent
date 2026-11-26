import base64
import re
from unittest.mock import MagicMock, patch

import pytest

from plugin.framework.errors import DocumentDisposedError, ToolExecutionError
from plugin.writer.html_export import (
    _apply_image_export_options,
    _inject_page_field_attributes,
    _range_to_content_via_temp_doc,
    strip_embedded_image_data,
    xtext_to_content,
)
from plugin.tests.testing_utils import DisposedException, FakeUnoEnum


class _Portion:
    def getPropertyValue(self, name: str) -> str:
        return ""

    def getString(self) -> str:
        return "Hello"


def _para(style: str):
    element = MagicMock()
    element.supportsService.return_value = False
    element.getString.return_value = "Hello"

    def _prop(name: str) -> str:
        if name == "ParaStyleName":
            return style
        return ""

    element.getPropertyValue.side_effect = _prop
    return element


def _run_range_export(
    elements=None,
    *,
    temp_doc=None,
    has_style: bool = True,
    set_style_raises: bool = False,
    portions_fn=None,
    xhtml: str = "<p>Hello</p><p>Hello</p>",
    export_side_effect=None,
    autostyle_return=({}, {}, False),
    passthrough_semantic: bool = True,
):
    """Run _range_to_content_via_temp_doc with standard mocked collaborators."""
    from contextlib import ExitStack

    temp_doc = temp_doc if temp_doc is not None else MagicMock()
    temp_text = MagicMock()
    temp_cursor = MagicMock()
    temp_doc.getText.return_value = temp_text
    temp_text.createTextCursor.return_value = temp_cursor
    styles = MagicMock()
    styles.hasByName.return_value = has_style
    temp_doc.getStyleFamilies.return_value.getByName.return_value = styles
    if set_style_raises:
        def _set(name: str, value: object) -> None:
            if name == "ParaStyleName":
                raise RuntimeError("style missing from scratch document")

        temp_cursor.setPropertyValue.side_effect = _set

    elems = list(elements) if elements is not None else [_para("Standard"), _para("Standard")]
    text = MagicMock()
    text.createEnumeration.side_effect = lambda: FakeUnoEnum(elems)
    source = MagicMock()
    source.getText.return_value = text

    if portions_fn is None:
        def portions_fn(element, truncated_out=None, state=None):
            yield (_Portion(), "Hello")

    export_kwargs = {"side_effect": export_side_effect} if export_side_effect is not None else {"return_value": xhtml}

    with ExitStack() as stack:
        stack.enter_context(patch("plugin.writer.html_export.new_blank_writer", return_value=temp_doc))
        stack.enter_context(patch("plugin.writer.html_export._visible_portions", side_effect=portions_fn))
        stack.enter_context(patch("plugin.writer.html_export._element_overlaps", return_value=True))
        stack.enter_context(
            patch("plugin.writer.html_export._trim_to_source", side_effect=lambda t, el, para_text, src, opened: (0, len(para_text)))
        )
        stack.enter_context(patch("plugin.writer.html_export.starts_at_or_after", return_value=False))
        stack.enter_context(patch("plugin.writer.range_copy.paint_direct_formatting"))
        stack.enter_context(patch("plugin.writer.html_export._hyperlink_urls", return_value=[]))
        stack.enter_context(patch("plugin.writer.html_ruby.ruby_spans_in_window", return_value=[]))
        stack.enter_context(patch("plugin.writer.html_export._export_xhtml", **export_kwargs))
        autostyle_mock = stack.enter_context(
            patch("plugin.writer.html_export._autostyle_maps", return_value=autostyle_return)
        )
        stack.enter_context(
            patch("plugin.writer.html_export.inject_exported_math_tex", side_effect=lambda model, ctx, content: content)
        )
        if passthrough_semantic:
            stack.enter_context(
                patch(
                    "plugin.writer.html_export.xhtml_post.xhtml_to_semantic_html",
                    side_effect=lambda xhtml_arg, parents, overrides: xhtml_arg,
                )
            )
        html = _range_to_content_via_temp_doc(
            MagicMock(), MagicMock(), 0, 0, None, source_range=source,
        )
    return html, temp_cursor, temp_doc, autostyle_mock


def _range_export(style: str, *, has_style: bool, set_style_raises: bool = False) -> tuple[str, MagicMock]:
    """Run a two-paragraph selection export against a mocked scratch document."""
    html, temp_cursor, _temp_doc, _autostyle_mock = _run_range_export(
        [_para(style), _para(style)],
        has_style=has_style,
        set_style_raises=set_style_raises,
    )
    return html, temp_cursor


def test_range_export_keeps_text_when_paragraph_style_is_missing():
    """A style absent from the scratch doc must not turn the selection into \"\"."""
    with patch("plugin.writer.range_copy._clone_missing_paragraph_style", return_value=False):
        html, cursor = _range_export("Petition Heading", has_style=False)
    assert "Hello" in html
    assert html != ""
    styled = [call for call in cursor.setPropertyValue.call_args_list if call.args and call.args[0] == "ParaStyleName"]
    assert styled == []
    assert [call.args[0] for call in cursor.setString.call_args_list] == ["Hello", "Hello"]


def test_range_export_sets_para_style_when_clone_succeeds():
    with patch("plugin.writer.range_copy._clone_missing_paragraph_style", return_value=True):
        html, cursor = _range_export("CustomStyle", has_style=False)
    assert "Hello" in html
    styled = [call.args for call in cursor.setPropertyValue.call_args_list if call.args and call.args[0] == "ParaStyleName"]
    assert styled == [("ParaStyleName", "CustomStyle"), ("ParaStyleName", "CustomStyle")]
    assert [call.args[0] for call in cursor.setString.call_args_list] == ["Hello", "Hello"]


def test_range_export_applies_paragraph_style_when_scratch_has_it():
    html, cursor = _range_export("Standard", has_style=True)
    assert "Hello" in html
    styled = [call.args for call in cursor.setPropertyValue.call_args_list if call.args and call.args[0] == "ParaStyleName"]
    assert styled == [("ParaStyleName", "Standard"), ("ParaStyleName", "Standard")]


def test_range_export_keeps_text_when_setting_paragraph_style_raises():
    html, cursor = _range_export("Standard", has_style=True, set_style_raises=True)
    assert "Hello" in html
    assert html != ""
    assert cursor.setString.call_count == 2


def test_xtext_to_content_none_is_empty():
    assert xtext_to_content(None, object(), object()) == ""


def _content_doc(text="Texto do documento"):
    model = MagicMock()
    model.getText.return_value.getString.return_value = text
    return model


def test_document_to_content_fails_loudly_when_both_exports_fail():
    """#63: both exports failing returned "" and get_content said ok with empty content."""

    from plugin.writer import html_export

    model = _content_doc()
    with patch.object(html_export, "_read_filter_text", side_effect=PermissionError("Permission denied")), \
         patch.object(html_export, "_export_starwriter_html", side_effect=OSError("No space left on device")), \
         pytest.raises(ToolExecutionError) as err:
        html_export.document_to_content(model, None)
    message = str(err.value)
    assert "Permission denied" in message and "No space left on device" in message


def test_empty_xhtml_export_of_a_document_with_text_falls_back():
    from plugin.writer import html_export

    with patch.object(html_export, "_export_xhtml", return_value=""), \
         patch.object(html_export, "_autostyle_maps", return_value=({}, {}, None)), \
         patch("plugin.writer.html_export.with_temp_buffer") as buf, \
         patch("builtins.open") as opener:
        buf.return_value.__enter__.return_value = ("/tmp/x.html", "file:///tmp/x.html")
        opener.return_value.__enter__.return_value.read.return_value = "<p>Texto do documento</p>"
        out = html_export.document_to_content(_content_doc(), None)
    assert "Texto do documento" in out


def test_full_export_fallback_reports_missing_style_tokens():
    from plugin.writer import html_export

    warnings: list[str] = []
    with (
        patch.object(html_export, "_export_xhtml", side_effect=PermissionError("Permission denied")),
        patch("plugin.writer.html_export.with_temp_buffer") as buf,
        patch("builtins.open") as opener,
        patch("plugin.writer.html_export.inject_exported_math_tex", side_effect=lambda model, ctx, content: content),
        patch("plugin.writer.html_ruby.inject_ruby_from_model", side_effect=lambda model, content, spans: content),
    ):
        buf.return_value.__enter__.return_value = ("/tmp/x.html", "file:///tmp/x.html")
        opener.return_value.__enter__.return_value.read.return_value = "<p>Texto do documento</p>"
        out = html_export.document_to_content(_content_doc(), None, walk_warnings=warnings)
    assert "Texto do documento" in out
    assert len(warnings) == 1


def test_full_export_happy_path_produces_no_fallback_warning():
    from plugin.writer import html_export

    warnings: list[str] = []
    with (
        patch.object(html_export, "_export_xhtml", return_value="<p>x</p>"),
        patch.object(html_export, "_autostyle_maps", return_value=({}, {}, False)),
        patch("plugin.writer.xhtml_style_postprocess.xhtml_to_semantic_html", side_effect=lambda xhtml, parents, overrides: xhtml),
        patch("plugin.writer.html_export.inject_exported_math_tex", side_effect=lambda model, ctx, content: content),
        patch("plugin.writer.html_ruby.inject_ruby_from_model", side_effect=lambda model, content, spans: content),
    ):
        out = html_export.document_to_content(_content_doc(), None, walk_warnings=warnings)
    assert "<p>x</p>" in out
    assert warnings == []


def test_an_empty_document_still_reads_as_empty():
    from plugin.writer import html_export

    with patch.object(html_export, "_export_xhtml", return_value=""), \
         patch.object(html_export, "_autostyle_maps", return_value=({}, {}, None)):
        assert html_export.document_to_content(_content_doc(""), None) == ""
def test_shorten_outline_anchor_ids_replaces_picture_data_in_heading_anchor():
    from plugin.writer.html_export import shorten_outline_anchor_ids

    # Shape of LibreOffice's XHTML export for a heading that holds an embedded picture.
    long_id = "a__iVBORw0KGgoAAAANSUhEUgAAAxs" + "A" * 40000 + "ErkJgggAODOUTOJUIZODETESTE"
    html = '<h1><a id="%s"><span/></a>AO DOUTO</h1><p><a href="#%s">ver</a></p>' % (long_id, long_id)
    out = shorten_outline_anchor_ids(html)
    ids = re.findall(r'id="([^"]*)"', out)
    assert len(out) < 200 and ids and re.fullmatch(r"a__[0-9a-f]{8}", ids[0])
    assert 'href="#%s"' % ids[0] in out  # links to the anchor follow it
    assert shorten_outline_anchor_ids(html) == out  # stable across reads


def test_shorten_outline_anchor_ids_keeps_short_and_other_ids():
    from plugin.writer.html_export import shorten_outline_anchor_ids

    long_bookmark = "x" * 500  # a user bookmark is not an outline anchor: agents may target it
    html = '<h2><a id="a__Introducao"><span/></a>Introdução</h2><a id="_mcp_1"></a><a id="%s"></a>' % long_bookmark
    assert shorten_outline_anchor_ids(html) == html


def test_apply_image_export_options_shortens_anchor_with_or_without_images():
    data = "data:image/png;base64," + "QUJD" * 100
    html = '<h1><a id="a__%s"><span/></a><img src="%s"/>T</h1>' % ("QUJD" * 100, data)
    with_images = _apply_image_export_options(html, include_images=True)
    assert data in with_images and "a__QUJD" not in with_images
    without = _apply_image_export_options(html, include_images=False)
    assert "QUJD" not in without and 'src=""' in without


def test_xtext_to_content_propagates_export_error_and_closes():
    """An export failure propagates; returning "" would let the header tool report ok on an empty read."""
    from plugin.writer import html_export

    temp = MagicMock()
    temp.getText.return_value = MagicMock()
    with (
        patch.object(html_export, "new_blank_writer", return_value=temp),
        patch.object(html_export.range_copy, "copy_xtext_into_doc"),
        patch.object(
            html_export,
            "document_to_content",
            side_effect=ToolExecutionError("Could not read the document: boom"),
        ),
        pytest.raises(ToolExecutionError, match="boom"),
    ):
        html_export.xtext_to_content(MagicMock(), MagicMock(), MagicMock())
    temp.close.assert_called_once_with(True)


def test_xtext_to_content_raises_when_scratch_missing():
    from plugin.writer import html_export

    with (
        patch.object(html_export, "new_blank_writer", return_value=None),
        pytest.raises(ToolExecutionError, match="no scratch document"),
    ):
        html_export.xtext_to_content(MagicMock(), MagicMock(), MagicMock())


def test_range_export_raises_when_scratch_missing():
    with (
        patch("plugin.writer.html_export.new_blank_writer", return_value=None),
        pytest.raises(ToolExecutionError, match="no scratch document"),
    ):
        _range_to_content_via_temp_doc(MagicMock(), MagicMock(), 0, 5, None)


def test_full_export_disposal_does_not_fall_back():
    from plugin.writer import html_export

    model = _content_doc()
    with (
        patch.object(html_export, "_read_filter_text", side_effect=DisposedException("disposed")),
        patch.object(html_export, "_export_starwriter_html") as starwriter,
        pytest.raises(DocumentDisposedError),
    ):
        html_export.document_to_content(model, None)
    starwriter.assert_not_called()


def test_starwriter_disposal_is_not_wrapped():
    from plugin.writer import html_export

    model = _content_doc()
    model.storeToURL.side_effect = DisposedException("disposed")
    with (
        patch.object(html_export, "_export_xhtml", side_effect=PermissionError("Permission denied")),
        pytest.raises(DocumentDisposedError),
    ):
        html_export.document_to_content(model, None)


def test_range_xhtml_disposal_does_not_fall_back():
    temp_doc = MagicMock()
    with pytest.raises(DocumentDisposedError):
        _run_range_export(
            [_para("Standard")],
            temp_doc=temp_doc,
            export_side_effect=DisposedException("disposed"),
            passthrough_semantic=False,
        )
    temp_doc.storeToURL.assert_not_called()


def test_range_starwriter_fallback_strips_deleted_mark():
    from plugin.writer import html_export, xhtml_style_postprocess as xhtml_post

    with patch.object(
        html_export, "_export_starwriter_html", return_value=f"<p>{xhtml_post.DELETED_MARK}</p>"
    ):
        out = _run_range_export(
            [_para("Standard")],
            export_side_effect=RuntimeError("xhtml failed"),
            passthrough_semantic=False,
        )
    assert xhtml_post.DELETED_MARK not in out


def test_offset_window_slices_by_running_offset():
    from plugin.writer.html_export import _offset_window

    past_end, window = _offset_window(2, 8)
    e1, e2, e3 = MagicMock(), MagicMock(), MagicMock()
    assert window(e1, "Hello", [], False) == ([], (2, 5))
    assert window(e2, "World", [], False) == ([], (0, 2))
    assert window(e3, "Next", [], False) == ([], None)
    assert past_end(e1) is False


def test_offset_window_leading_and_adjacent_tables():
    from plugin.writer.html_export import _offset_window

    t1, t2 = MagicMock(), MagicMock()
    texts = {id(t1): "T1", id(t2): "T23"}
    with patch("plugin.writer.html_export.table_visible_text", side_effect=lambda t, _opened: texts[id(t)]):
        # Leading t1 (0..2), t2 (3..6), para "Beta" (7..11)
        _past_end, window = _offset_window(0, 2)
        assert window(MagicMock(), "Beta", [t1, t2], False) == ([t1], None)

        _past_end, window = _offset_window(3, 6)
        assert window(MagicMock(), "Beta", [t1, t2], False) == ([t2], None)

        _past_end, window = _offset_window(7, 11)
        assert window(MagicMock(), "Beta", [t1, t2], False) == ([], (0, 4))


def test_offset_window_keeps_empty_paragraph_at_start():
    from plugin.writer.html_export import _offset_window

    _past_end, window = _offset_window(6, 8)
    assert window(MagicMock(), "Alpha", [], False) == ([], None)
    assert window(MagicMock(), "", [], False) == ([], (0, 0))
    assert window(MagicMock(), "Beta", [], False) == ([], (0, 1))


def test_offset_window_empty_and_struck_tables():
    from plugin.writer.html_export import _offset_window

    empty_t, struck_t = MagicMock(), MagicMock()
    with patch("plugin.writer.html_export.table_visible_text", side_effect=lambda _t, opened: "" if opened else "Cell"):
        # Para "A" (0..1), empty_t (2..6), para "B" (7..8)
        _past_end, window = _offset_window(2, 6)
        assert window(MagicMock(), "A", [], False) == ([], None)
        assert window(MagicMock(), "B", [empty_t], False) == ([empty_t], None)

        # Para "A" (0..1), struck_t (2..2, one break), para "B" (3..4)
        _past_end, window = _offset_window(3, 4)
        assert window(MagicMock(), "A", [], False) == ([], None)
        assert window(MagicMock(), "B", [struck_t], True) == ([], (0, 1))


@pytest.mark.parametrize(
    "exc, expected, match",
    [
        (RuntimeError("IllegalArgumentException"), ToolExecutionError, "Could not read that part"),
        (DisposedException("disposed"), DocumentDisposedError, None),
    ],
)
def test_selection_compare_failure_is_not_a_silent_skip(exc, expected, match):
    temp_doc = MagicMock()
    temp_text = MagicMock()
    temp_doc.getText.return_value = temp_text
    temp_text.createTextCursor.return_value = MagicMock()
    text = MagicMock()
    text.createEnumeration.return_value = FakeUnoEnum([_para("Standard")])
    text.compareRegionStarts.side_effect = exc
    source = MagicMock()
    source.getText.return_value = text

    def _portions(element, truncated_out=None, state=None):
        yield (_Portion(), "Hello")

    with (
        patch("plugin.writer.html_export.new_blank_writer", return_value=temp_doc),
        patch("plugin.writer.html_export._visible_portions", side_effect=_portions),
        patch("plugin.writer.html_export._trim_to_source", side_effect=lambda text, element, para_text, source, opened: (0, len(para_text))),
        patch("plugin.writer.html_export.starts_at_or_after", return_value=False),
        patch("plugin.writer.range_copy.paint_direct_formatting"),
        patch("plugin.writer.html_export._hyperlink_urls", return_value=[]),
        patch("plugin.writer.html_ruby.ruby_spans_in_window", return_value=[]),
        pytest.raises(expected, match=match),
    ):
        _range_to_content_via_temp_doc(
            MagicMock(), MagicMock(), 0, 0, None, source_range=source,
        )


def test_trim_to_source_does_not_swallow_get_start():
    from plugin.writer.html_export import _trim_to_source

    element = MagicMock()
    element.getStart.side_effect = DisposedException("disposed")
    with pytest.raises(DisposedException):
        _trim_to_source(MagicMock(), element, "Hello", MagicMock(), False)


def test_range_export_does_not_read_para_style_on_a_table():
    """SwXTextTable has no ParaStyleName. The walk must not abort on it."""
    table = MagicMock()
    table.supportsService.side_effect = lambda name: name == "com.sun.star.text.TextTable"

    def _portions(element, truncated_out=None, state=None):
        if element is not table:
            yield (_Portion(), "Hello")

    html, _cursor, _temp_doc, _autostyle = _run_range_export(
        [table, _para("Standard")],
        portions_fn=_portions,
        xhtml="<p>Hello</p>",
    )
    assert "Hello" in html
    table.getPropertyValue.assert_not_called()


def test_truncate_html_keeps_tag_and_entity_closed():
    from plugin.writer.html_export import _truncate_html

    html = '<p><a href="https://example.com/very-long-path">x</a></p>'
    out = _truncate_html(html, 12)
    assert out.endswith("[... truncated ...]")
    body = out.split("[... truncated ...]", 1)[0]
    assert "<" not in body or body.rfind("<") < body.rfind(">")
    entity = _truncate_html("hello&nbsp;world", 8)
    assert "&n" not in entity.split("[... truncated ...]", 1)[0]
    assert entity.startswith("hello")


def test_document_to_content_max_chars_does_not_split_a_tag():
    from plugin.writer import html_export, html_ruby

    long_html = '<p><a href="https://example.com/very-long-path">visible</a></p>'
    with (
        patch.object(html_export, "_export_xhtml", return_value=long_html),
        patch.object(html_export, "_autostyle_maps", return_value=({}, {}, False)),
        patch.object(html_export, "inject_exported_math_tex", side_effect=lambda model, ctx, content: content),
        patch.object(html_ruby, "inject_ruby_from_model", side_effect=lambda model, content, fodt_has_ruby=None: content),
        patch(
            "plugin.writer.html_export.xhtml_post.xhtml_to_semantic_html",
            side_effect=lambda xhtml, parents, overrides: xhtml,
        ),
    ):
        out = html_export.document_to_content(_content_doc(), None, max_chars=12)
    body = out.split("[... truncated ...]", 1)[0]
    assert "<" not in body or body.rfind("<") < body.rfind(">")


def test_extract_page_fields_reads_properties():
    from plugin.writer.html_export import extract_page_fields

    field = MagicMock()
    field.supportsService.side_effect = lambda svc: svc == "com.sun.star.text.textfield.PageNumber"
    field.getPropertyValue.side_effect = lambda name: {"SubType": 2, "Offset": 1, "NumberingType": 3}[name]
    portion = MagicMock()
    portion.getPropertyValue.side_effect = lambda name: "TextField" if name == "TextPortionType" else field
    para = MagicMock()
    para.supportsService.return_value = False
    para.createEnumeration.return_value = FakeUnoEnum([portion])
    text = MagicMock()
    text.createEnumeration.return_value = FakeUnoEnum([para])
    assert extract_page_fields(MagicMock(), text) == [
        {"title": "page-number", "SubType": 2, "Offset": 1, "NumberingType": 3}
    ]


def test_extract_page_fields_reads_a_table_cell():
    from plugin.writer.html_export import extract_page_fields

    field = MagicMock()
    field.supportsService.side_effect = lambda svc: svc == "com.sun.star.text.textfield.PageNumber"
    field.getPropertyValue.side_effect = lambda name: {"SubType": 1, "Offset": 2, "NumberingType": 4}[name]
    portion = MagicMock()
    portion.getPropertyValue.side_effect = lambda name: "TextField" if name == "TextPortionType" else field
    para = MagicMock()
    para.supportsService.return_value = False
    para.createEnumeration.return_value = FakeUnoEnum([portion])

    cell = MagicMock()
    cell.createEnumeration.return_value = FakeUnoEnum([para])

    table = MagicMock()
    table.supportsService.side_effect = lambda s: s == "com.sun.star.text.TextTable"
    table.getCellNames.return_value = ("A1",)
    table.getCellByName.return_value = cell

    text = MagicMock()
    text.createEnumeration.return_value = FakeUnoEnum([table])

    assert extract_page_fields(MagicMock(), text) == [
        {"title": "page-number", "SubType": 1, "Offset": 2, "NumberingType": 4}
    ]


def test_extract_page_fields_no_field_returns_empty():
    from plugin.writer.html_export import extract_page_fields

    portion = MagicMock()
    portion.getPropertyValue.side_effect = lambda name: "Text" if name == "TextPortionType" else None
    para = MagicMock()
    para.supportsService.return_value = False
    para.createEnumeration.return_value = FakeUnoEnum([portion])
    text = MagicMock()
    text.createEnumeration.return_value = FakeUnoEnum([para])

    assert extract_page_fields(MagicMock(), text) == []


def test_xtext_injects_nonzero_page_offset():
    from plugin.writer import html_export

    html = '<p><span title="page-number"></span></p>'
    temp = MagicMock()
    temp.getText.return_value = MagicMock()
    fields = [{"title": "page-number", "SubType": 2, "Offset": 1, "NumberingType": 4}]
    with (
        patch.object(html_export, "new_blank_writer", return_value=temp),
        patch.object(html_export.range_copy, "copy_xtext_into_doc"),
        patch.object(html_export, "extract_page_fields", return_value=fields),
        patch.object(html_export, "document_to_content", return_value=html),
    ):
        out = html_export.xtext_to_content(MagicMock(), MagicMock(), MagicMock())
    assert 'data-offset="1"' in out
    assert 'data-subtype="NEXT"' in out
    assert "data-numbering-type" not in out


def test_inject_page_field_attributes_defaults_omitted():
    inp = '<p><span title="page-number"></span></p>'
    fields = [{"title": "page-number", "SubType": 1, "Offset": 0, "NumberingType": 4}]
    assert _inject_page_field_attributes(inp, fields) == inp


def test_inject_page_field_attributes_non_defaults_in_order():
    inp = '<p><span title="page-number"></span></p>'
    fields = [{"title": "page-number", "SubType": 2, "Offset": 1, "NumberingType": 3}]
    out = _inject_page_field_attributes(inp, fields)
    assert '<span title="page-number" data-subtype="NEXT" data-offset="1" data-numbering-type="ROMAN_LOWER"></span>' in out


def test_inject_page_field_attributes_self_closing_tag():
    inp = '<p><span title="page-number"/></p>'
    fields = [{"title": "page-number", "SubType": 2, "Offset": 1, "NumberingType": 3}]
    out = _inject_page_field_attributes(inp, fields)
    assert '<span title="page-number" data-subtype="NEXT" data-offset="1" data-numbering-type="ROMAN_LOWER"/>' in out


def test_inject_page_field_attributes_page_count():
    inp = '<p>Page <span title="page-number"/> of <span title="page-count"/></p>'
    fields = [
        {"title": "page-number", "SubType": 1, "Offset": 0, "NumberingType": 4},
        {"title": "page-count", "NumberingType": 2},
    ]
    out = _inject_page_field_attributes(inp, fields)
    assert out == '<p>Page <span title="page-number"/> of <span title="page-count" data-numbering-type="ROMAN_UPPER"/></p>'
    assert _inject_page_field_attributes(inp, list(reversed(fields))) == inp


def test_inject_page_field_attributes_count_mismatch_returns_unchanged():
    two_spans = '<p><span title="page-number"></span><span title="page-number"></span></p>'
    one_field = [{"title": "page-number", "SubType": 2, "Offset": 1, "NumberingType": 3}]
    assert _inject_page_field_attributes(two_spans, one_field) == two_spans

    one_span = '<p><span title="page-number"></span></p>'
    two_fields = [
        {"title": "page-number", "SubType": 2, "Offset": 1, "NumberingType": 3},
        {"title": "page-number", "SubType": 1, "Offset": 0, "NumberingType": 4},
    ]
    assert _inject_page_field_attributes(one_span, two_fields) == one_span


def test_inject_page_field_attributes_empty_fields_returns_unchanged():
    inp = '<p><span title="page-number"></span></p>'
    assert _inject_page_field_attributes(inp, []) == inp


def test_page_field_converters():
    from types import SimpleNamespace

    from plugin.writer.html_export import (
        _numbering_type_to_name,
        _page_number_subtype_to_name,
    )

    assert _page_number_subtype_to_name(2) == "NEXT"
    assert _page_number_subtype_to_name("2") == "NEXT"
    assert _page_number_subtype_to_name("next") == "NEXT"
    assert _page_number_subtype_to_name(SimpleNamespace(value="PREV")) == "PREV"
    assert _page_number_subtype_to_name(99) == "CURRENT"

    assert _numbering_type_to_name(4) == "ARABIC"
    assert _numbering_type_to_name("ARABIC") == "ARABIC"
    assert _numbering_type_to_name("roman_upper") == "ROMAN_UPPER"
    assert _numbering_type_to_name(99) == "ARABIC"


def test_uno_read_probes_default_on_ordinary_errors():
    from plugin.writer.html_export import _raise_if_empty_export
    from plugin.writer.range_copy import _writer_table_copy_layout

    class RuntimeException(Exception):
        pass

    # A failed text read still counts as "has text" and the export error is raised.
    model = MagicMock()
    model.getText.side_effect = RuntimeException("uno")
    with pytest.raises(Exception, match="empty"):
        _raise_if_empty_export(model, "", "empty")

    table = MagicMock()
    table.getRows.side_effect = RuntimeException("uno")
    table.getCellNames.return_value = ()
    rows, cols, names = _writer_table_copy_layout(table)
    assert (rows, cols, names) == (1, 1, [])


def test_substitute_math_placeholders_entities_and_fallback():
    from plugin.writer.math import math_mml_export as exp

    p1 = chr(0xE000)
    p2 = chr(0xE001)
    p3 = chr(0xE002)

    html = f"<p>{p1} and &#xe001; and &#57346;</p>"
    records = [
        (p1, "a + b", False),
        (p2, "x", True),
        (p3, "bad starmath", False),
    ]

    def fake_convert(self: object, starmath: str | None = None) -> exp.MathExportResult:
        expr = self if starmath is None else starmath
        if expr == "a + b":
            return exp.MathExportResult(True, "a + b", None)
        if expr == "x":
            return exp.MathExportResult(True, "x", None)
        return exp.MathExportResult(False, None, "error")

    with patch.object(exp.MathExportBatch, "convert_starmath_to_latex", side_effect=fake_convert):
        out = exp.substitute_math_placeholders(html, records, object())

    assert "$a + b$" in out
    assert "$$x$$" in out
    assert 'data-math-export-error="error">bad starmath' in out
    assert "$bad starmath" not in out

def test_range_export_skips_flat_odf_when_no_autostyle():
    """Flat ODF is skipped when XHTML contains no paragraph-P<digits> autostyle classes."""
    # Case 1: XHTML with paragraph-Text_20_body does not call _autostyle_maps
    out_named, _c1, _td1, autostyle_mock1 = _run_range_export(
        [_para("Text body")],
        xhtml='<html><body><p class="paragraph-Text_20_body">Hello</p></body></html>',
        autostyle_return=({"P1": "Standard"}, {"P1": "margin:0"}, False),
        passthrough_semantic=False,
    )
    autostyle_mock1.assert_not_called()
    assert '<p data-lo-style="Textbody">Hello</p>' in out_named

    # Case 2: XHTML with paragraph-P1 calls _autostyle_maps and applies overrides in semantic HTML
    out_auto, _c2, temp_doc2, autostyle_mock2 = _run_range_export(
        [_para("Text body")],
        xhtml='<html><body><p class="paragraph-P1">Hello</p></body></html>',
        autostyle_return=({"P1": "Standard"}, {"P1": "color:red"}, False),
        passthrough_semantic=False,
    )
    autostyle_mock2.assert_called_once_with(temp_doc2, None)
    assert '<p data-lo-style="Standard" data-lo-para="color:red">Hello</p>' in out_auto


def test_stripping():
    from plugin.writer.html_export import strip_html_boilerplate

    # Full HTML document stripping
    inp = '<html><head><style>body { color: red; }</style></head><body lang="en-US"><h1>Hello</h1><p>World</p></body></html>'
    expected = '<h1>Hello</h1><p>World</p>'
    result = strip_html_boilerplate(inp)
    assert result == expected

    # Upper case tags
    inp = '<BODY>Upper Case</BODY>'
    expected = 'Upper Case'
    result = strip_html_boilerplate(inp)
    assert result == expected


def test_strip_embedded_image_data_removes_base64_keeps_external_url():
    b64 = base64.b64encode(b"png-bytes").decode("ascii")
    html = (
        f'<p><img src="data:image/png;base64,{b64}" alt="chart"/>'
        f'<img src="image001.png" alt="linked"/></p>'
    )
    out = strip_embedded_image_data(html)
    assert "data:image" not in out
    assert b64 not in out
    assert 'src="image001.png"' in out
    assert 'alt="chart"' in out


def test_strip_embedded_image_data_css_background_url():
    b64 = base64.b64encode(b"x").decode("ascii")
    html = f'<p style="background-image: url(data:image/png;base64,{b64})">x</p>'
    out = strip_embedded_image_data(html)
    assert "data:image" not in out
    assert b64 not in out
    assert "background-image: url()" in out


def test_apply_image_export_options_skips_when_include_images_true():
    b64 = base64.b64encode(b"x").decode("ascii")
    html = f'<img src="data:image/png;base64,{b64}"/>'
    assert _apply_image_export_options(html, include_images=True) == html


def _enum(items):
    e = MagicMock()
    rest = list(items)
    e.hasMoreElements.side_effect = lambda: True if rest else False
    e.nextElement.side_effect = lambda: rest.pop(0)
    return e


def _text_portion(text, ptype="Text", **props):
    p = MagicMock()
    values = {"TextPortionType": ptype}
    values.update(props)
    p.getPropertyValue.side_effect = lambda n: values[n]
    p.getString.return_value = text
    return p


def _para_of(portions):
    para = MagicMock()
    para.createEnumeration.side_effect = lambda: _enum(portions)
    return para


def test_visible_portions_skips_tracked_deletions():
    """Shared walk with get_string_without_tracked_deletions — a divergence would paint one
    run's formatting onto another run's characters."""
    from plugin.writer import html_export as hx

    para = _para_of([
        _text_portion("kept "),
        _text_portion("", "Redline", RedlineType="Delete", IsStart=True),
        _text_portion("deleted"),
        _text_portion("", "Redline", RedlineType="Delete", IsStart=False),
        _text_portion(" tail"),
    ])

    assert "".join(chunk for _p, chunk in hx._visible_portions(para)) == "kept  tail"


def test_visible_portions_ignores_non_delete_redlines():
    from plugin.writer import html_export as hx

    para = _para_of([
        _text_portion("", "Redline", RedlineType="Insert"),
        _text_portion("inserted text"),
    ])

    assert "".join(chunk for _p, chunk in hx._visible_portions(para)) == "inserted text"


def test_visible_portions_warns_when_walk_hits_cap():
    """Silent truncate hid later runs' Char*; the agent needs a warning that the paint is partial."""
    from plugin.writer import html_export as hx

    para = _para_of([_text_portion("a"), _text_portion("b"), _text_portion("c")])
    truncated = []
    chunks = [chunk for _p, chunk in hx._visible_portions(para, limit=2, truncated_out=truncated)]

    assert chunks == ["a", "b"]
    assert len(truncated) == 1


def test_visible_portions_does_not_warn_when_enum_is_exhausted():
    from plugin.writer import html_export as hx

    para = _para_of([_text_portion("a"), _text_portion("b")])
    truncated = []
    chunks = [chunk for _p, chunk in hx._visible_portions(para, limit=2, truncated_out=truncated)]

    assert chunks == ["a", "b"]
    assert truncated == []


def test_put_outline_urls_rewrites_only_a_anchors_and_escapes_html():
    from plugin.writer.html_export import _put_outline_urls

    content = (
        '<p><a href="#ftn0" id="body_ftn0">1</a> '
        '<a href="#a_one">one</a> '
        '<a href="https://x/">ext</a> '
        '<a href="#a_two">two</a></p>'
    )
    urls = ["#A & B|outline", "https://x/", "#C%7Coutline"]
    out = _put_outline_urls(content, urls, {})
    assert out == (
        '<p><a href="#ftn0" id="body_ftn0">1</a> '
        '<a href="#A &amp; B|outline">one</a> '
        '<a href="https://x/">ext</a> '
        '<a href="#C%7Coutline">two</a></p>'
    )


def test_put_outline_urls_leaves_content_unchanged_on_count_mismatch():
    from plugin.writer.html_export import _put_outline_urls

    content = '<p><a href="#a_one">one</a> <a href="#a_two">two</a></p>'
    assert _put_outline_urls(content, ["#OnlyOne|outline"], {}) == content


def test_put_outline_urls_skips_empty_split_anchors():
    from plugin.writer.html_export import _put_outline_urls

    content = '<p><a href="#a_" class="text-Internet_20_link"/></p><p><a href="#a_Live">Live</a></p>'
    out = _put_outline_urls(content, ["#Heading|outline"], {})
    assert out == '<p><a href="#a_" class="text-Internet_20_link"/></p><p><a href="#Heading|outline">Live</a></p>'


def test_put_outline_urls_restores_picture_by_name_even_on_text_mismatch():
    from plugin.writer.html_export import _put_outline_urls

    pic_only = '<p><a href="#a_base64"><span id="Pic1"><img src=""/></span></a> <a href="#a_one">one</a></p>'
    assert _put_outline_urls(pic_only, ["#One|outline"], {"Pic1": "#PicTarget|outline"}) == (
        '<p><a href="#PicTarget|outline"><span id="Pic1"><img src=""/></span></a> <a href="#One|outline">one</a></p>'
    )
    mismatch = '<p><a href="#a_base64"><span id="Pic1"><img src=""/></span></a> <a href="#a_one">one</a> <a href="#a_two">two</a></p>'
    assert _put_outline_urls(mismatch, ["#One|outline"], {"Pic1": "#PicTarget|outline"}) == (
        '<p><a href="#PicTarget|outline"><span id="Pic1"><img src=""/></span></a> <a href="#a_one">one</a> <a href="#a_two">two</a></p>'
    )


def test_restore_anchor_hrefs_pairs_unlinked_picture_at_start_of_text_link_as_text():
    from plugin.writer.html_export import _ANCHOR_HREF_RE, _restore_anchor_hrefs

    content = '<p><a href="https://wrong"><span id="Pic1"><img src=""/></span> link</a></p>'
    assert _restore_anchor_hrefs(content, ["https://text/l"], _ANCHOR_HREF_RE, {}) == (
        '<p><a href="https://text/l"><span id="Pic1"><img src=""/></span> link</a></p>'
    )


def test_hyperlink_urls_handles_frame_portion_splitting_a_link():
    from plugin.writer.html_export import _hyperlink_urls

    def _portion(chunk, url, ptype="Text"):
        p = MagicMock()
        p.getPropertyValue.side_effect = lambda name: {"HyperLinkURL": url, "TextPortionType": ptype}[name]
        return (p, chunk)

    # Frame with a different URL splits the surrounding link into two runs.
    split_urls = _hyperlink_urls([
        _portion("before ", "https://text/l"),
        _portion("", "https://pic/l", "Frame"),
        _portion("after", "https://text/l"),
    ])
    assert split_urls == ["https://text/l", "https://text/l"]

    # Frame carrying the same URL as the surrounding link keeps one run.
    same_urls = _hyperlink_urls([
        _portion("before ", "https://text/l"),
        _portion("", "https://text/l", "Frame"),
        _portion("after", "https://text/l"),
    ])
    assert same_urls == ["https://text/l"]


def test_full_export_reads_flat_odf_once_when_no_break_is_split():
    from plugin.writer import html_export as hx

    fodt = (
        '<office:document xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0" '
        'xmlns:style="urn:oasis:names:tc:opendocument:xmlns:style:1.0" '
        'xmlns:fo="urn:oasis:names:tc:opendocument:xmlns:xsl-fo-compatible:1.0">'
        '<office:automatic-styles>'
        '<style:style style:name="P1" style:family="paragraph" style:parent-style-name="Standard">'
        '<style:paragraph-properties fo:margin-left="1in"/>'
        '</style:style>'
        '</office:automatic-styles></office:document>'
    )
    xhtml = '<html><body><p class="paragraph-P1">Hello</p></body></html>'
    flat_reads = 0

    def fake_read(_doc, filter_name, **_kwargs):
        nonlocal flat_reads
        if filter_name == hx.FLAT_ODF_FILTER:
            flat_reads += 1
            return fodt
        return xhtml

    with patch("plugin.writer.html_export.TrackedDeletions", return_value=True), \
         patch("plugin.writer.xhtml_style_postprocess.expand_tracked_deletions", return_value=None), \
         patch("plugin.writer.html_export._read_filter_text", side_effect=fake_read):
        html, _has_ruby = hx._export_full(MagicMock(), MagicMock())

    assert flat_reads == 1
    assert 'data-lo-para="margin-left:1in"' in html


def test_full_export_of_split_copy_reads_its_own_flat_odf():
    from plugin.writer import html_export as hx

    source_fodt = (
        '<office:document xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0" '
        'xmlns:style="urn:oasis:names:tc:opendocument:xmlns:style:1.0" '
        'xmlns:fo="urn:oasis:names:tc:opendocument:xmlns:xsl-fo-compatible:1.0">'
        '<office:automatic-styles>'
        '<style:style style:name="P1" style:family="paragraph" style:parent-style-name="Standard">'
        '<style:paragraph-properties fo:color="#111111"/>'
        '</style:style>'
        '</office:automatic-styles></office:document>'
    )
    copy_fodt = (
        '<office:document xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0" '
        'xmlns:style="urn:oasis:names:tc:opendocument:xmlns:style:1.0" '
        'xmlns:fo="urn:oasis:names:tc:opendocument:xmlns:xsl-fo-compatible:1.0">'
        '<office:automatic-styles>'
        '<style:style style:name="P1" style:family="paragraph" style:parent-style-name="Standard">'
        '<style:paragraph-properties fo:font-weight="bold"/>'
        '</style:style>'
        '</office:automatic-styles></office:document>'
    )
    source_doc = MagicMock()
    copy_doc = MagicMock()
    xhtml = '<html><body><p class="paragraph-P1">Hello</p></body></html>'

    def fake_read(doc, filter_name, **_kwargs):
        if filter_name == hx.FLAT_ODF_FILTER:
            return source_fodt if doc is source_doc else copy_fodt
        return xhtml

    with patch("plugin.writer.html_export.TrackedDeletions", return_value=True), \
         patch("plugin.writer.xhtml_style_postprocess.expand_tracked_deletions", return_value="<split/>"), \
         patch("plugin.writer.html_export.load_scratch_document", return_value=copy_doc), \
         patch("plugin.writer.html_export.close_scratch_document"), \
         patch("plugin.writer.html_export._read_filter_text", side_effect=fake_read):
        html, _has_ruby = hx._export_full(source_doc, MagicMock())

    assert 'data-lo-para="font-weight:bold"' in html
    assert "#111111" not in html


def test_export_full_disposes_struck_tables_even_without_split():
    from plugin.writer import html_export as hx

    fodt = (
        '<office:document xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0" '
        'xmlns:style="urn:oasis:names:tc:opendocument:xmlns:style:1.0">'
        '<office:automatic-styles/></office:document>'
    )
    xhtml = '<html><body><p>After</p></body></html>'

    struck_table = MagicMock()
    live_table = MagicMock()
    src_tables = MagicMock()
    src_tables.getElementNames.return_value = ("Table1", "Table2")
    src_tables.getByName.side_effect = lambda n: struck_table if n == "Table1" else live_table
    src_doc = MagicMock()
    src_doc.getTextTables.return_value = src_tables

    copy_t1 = MagicMock()
    copy_tables = MagicMock()
    copy_tables.hasByName.side_effect = lambda n: n == "Table1"
    copy_tables.getByName.return_value = copy_t1
    copy_doc = MagicMock()
    copy_doc.getTextTables.return_value = copy_tables

    def fake_read(_doc, filter_name, **_kwargs):
        return fodt if filter_name == hx.FLAT_ODF_FILTER else xhtml

    with patch("plugin.writer.html_export.TrackedDeletions", return_value=True), \
         patch("plugin.writer.range_copy.table_is_struck", side_effect=lambda _d, t, _s: t is struck_table), \
         patch("plugin.writer.xhtml_style_postprocess.expand_tracked_deletions", return_value=None), \
         patch("plugin.writer.html_export.load_scratch_document", return_value=copy_doc), \
         patch("plugin.writer.html_export.close_scratch_document"), \
         patch("plugin.writer.html_export._read_filter_text", side_effect=fake_read):
        html, _has_ruby = hx._export_full(src_doc, MagicMock())

    copy_t1.dispose.assert_called_once()
    assert "After" in html


def test_raise_if_empty_export_uses_visible_text_when_deletions_pending():
    from plugin.writer import html_export as hx

    model = MagicMock()
    model.getText.return_value.getString.return_value = "\nSECRET\n"

    with patch("plugin.writer.html_export.TrackedDeletions", return_value=True), \
         patch("plugin.writer.html_export.visible_range_text", return_value="\n\n"):
        hx._raise_if_empty_export(model, "", "empty")

    with patch("plugin.writer.html_export.TrackedDeletions", return_value=True), \
         patch("plugin.writer.html_export.visible_range_text", return_value="Visible"):
        with pytest.raises(ToolExecutionError, match="empty"):
            hx._raise_if_empty_export(model, "", "empty")


def test_full_fallback_without_deletions_exports_the_model():
    from plugin.writer import html_export as hx

    model = MagicMock()
    warnings: list[str] = []
    with patch("plugin.writer.html_export._export_full", side_effect=RuntimeError("boom")), \
         patch("plugin.writer.html_export._export_starwriter_html", return_value="<p>Body</p>") as sw, \
         patch("plugin.writer.html_export.TrackedDeletions", return_value=False):
        out = hx.document_to_content(model, MagicMock(), scope="full", walk_warnings=warnings)

    assert "Body" in out
    assert sw.call_count == 1
    assert sw.call_args.args[0] is model
    assert len(warnings) == 1


def test_full_fallback_with_pending_deletions_reads_a_copy_without_tracked_changes():
    from plugin.writer import html_export as hx
    from plugin.writer.xhtml_style_postprocess import DELETED_MARK

    source_fodt = (
        "<office:document><office:body><office:text>"
        "<text:tracked-changes>"
        '<text:changed-region text:id="ct1"><text:deletion><text:p>gone</text:p></text:deletion></text:changed-region>'
        "</text:tracked-changes>"
        '<text:p><text:change text:change-id="ct1"/>kept</text:p>'
        "</office:text></office:body></office:document>"
    )
    source_doc = MagicMock()
    copy_doc = MagicMock()
    written: list[str] = []
    warnings: list[str] = []

    def fake_write(content, ext=""):
        written.append(content)
        return MagicMock(__enter__=lambda s: ("/tmp/x.fodt", "file:///tmp/x.fodt"), __exit__=lambda *a: None)

    with patch("plugin.writer.html_export._export_full", side_effect=RuntimeError("boom")), \
         patch("plugin.writer.html_export.TrackedDeletions", return_value=True), \
         patch("plugin.writer.html_export._read_filter_text", return_value=source_fodt), \
         patch("plugin.writer.html_export.with_temp_buffer", side_effect=fake_write), \
         patch("plugin.writer.html_export.load_scratch_document", return_value=copy_doc), \
         patch("plugin.writer.html_export.close_scratch_document") as close_copy, \
         patch("plugin.writer.html_export._export_starwriter_html", return_value=f"<p>kept</p><p>{DELETED_MARK}</p>") as sw:
        out = hx.document_to_content(source_doc, MagicMock(), scope="full", walk_warnings=warnings)

    assert len(written) == 1
    assert "<text:tracked-changes" not in written[0]
    assert "gone" not in written[0]
    assert sw.call_count == 1
    assert sw.call_args.args[0] is copy_doc
    assert close_copy.call_count == 1
    assert "kept" in out
    assert DELETED_MARK not in out
    assert len(warnings) == 1


def test_full_fallback_with_deletions_raises_when_no_scratch_copy():
    from plugin.writer import html_export as hx

    with patch("plugin.writer.html_export._export_full", side_effect=RuntimeError("boom")), \
         patch("plugin.writer.html_export.TrackedDeletions", return_value=True), \
         patch("plugin.writer.html_export._read_filter_text", return_value="<office:document/>"), \
         patch("plugin.writer.html_export.with_temp_buffer", return_value=MagicMock(__enter__=lambda s: ("/tmp/x.fodt", "file:///tmp/x.fodt"), __exit__=lambda *a: None)), \
         patch("plugin.writer.html_export.load_scratch_document", return_value=None):
        with pytest.raises(ToolExecutionError, match="no scratch document"):
            hx.document_to_content(MagicMock(), MagicMock(), scope="full")


def test_full_export_with_hidden_section_and_no_redlines_exports_scratch_copy():
    from plugin.writer import html_export as hx

    source_fodt = '<office:document><text:section text:name="H" text:display="none"/></office:document>'
    source_doc = MagicMock()

    copy_doc = MagicMock()
    written = []

    def fake_write(content, ext=""):
        written.append(content)
        return MagicMock(__enter__=lambda s: ("/tmp/x.fodt", "file:///tmp/x.fodt"), __exit__=lambda *a: None)

    with patch("plugin.writer.html_export.TrackedDeletions", return_value=False), \
         patch("plugin.writer.html_export._read_filter_text", return_value=source_fodt), \
         patch("plugin.writer.html_export.with_temp_buffer", side_effect=fake_write), \
         patch("plugin.writer.html_export.load_scratch_document", return_value=copy_doc), \
         patch("plugin.writer.html_export.close_scratch_document"), \
         patch("plugin.writer.html_export._export_semantic", return_value=("<p>ok</p>", False)) as sem:
        html, _has_ruby = hx._export_full(source_doc, MagicMock())

    assert html == "<p>ok</p>"
    assert written == ['<office:document><text:section text:name="H"/></office:document>']
    assert sem.call_args.args[0] is copy_doc


def test_export_full_simplifies_drawings_before_expanding_tracked_deletions():
    from plugin.writer import html_export as hx
    from plugin.writer.xhtml_style_postprocess import DELETED_MARK

    struck_shape_fodt = (
        '<office:body><office:text>'
        '<text:tracked-changes><text:changed-region text:id="ct1"><text:deletion><text:p/></text:deletion></text:changed-region></text:tracked-changes>'
        '<text:p><text:change text:change-id="ct1"/><draw:rect draw:name="R2"><text:p> </text:p></draw:rect></text:p>'
        '</office:text></office:body>'
    )
    source_doc = MagicMock()
    source_doc.getTextTables.return_value.getElementNames.return_value = []
    written = []

    def fake_write(content, ext=""):
        written.append(content)
        return MagicMock(__enter__=lambda s: ("/tmp/x.fodt", "file:///tmp/x.fodt"), __exit__=lambda *a: None)

    with patch("plugin.writer.html_export.TrackedDeletions", return_value=True), \
         patch("plugin.writer.html_export._read_filter_text", return_value=struck_shape_fodt), \
         patch("plugin.writer.html_export.with_temp_buffer", side_effect=fake_write), \
         patch("plugin.writer.html_export.load_scratch_document", return_value=MagicMock()), \
         patch("plugin.writer.html_export.close_scratch_document"), \
         patch("plugin.writer.html_export._export_semantic", return_value=("<p>ok</p>", False)):
        hx._export_full(source_doc, MagicMock())

    assert len(written) == 1
    assert DELETED_MARK in written[0]
