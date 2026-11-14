import re
from unittest.mock import MagicMock, patch

import pytest

from plugin.framework.errors import DocumentDisposedError, ToolExecutionError
from plugin.writer.html_export import (
    _inject_page_number_attributes,
    _range_to_content_via_temp_doc,
    xtext_to_content,
)


class _Enum:
    def __init__(self, items: list) -> None:
        self._items = list(items)

    def hasMoreElements(self) -> bool:
        return bool(self._items)

    def nextElement(self):
        return self._items.pop(0)


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
    text.createEnumeration.side_effect = lambda: _Enum(elems)
    source = MagicMock()
    source.getText.return_value = text

    if portions_fn is None:
        def portions_fn(element, truncated_out=None):
            yield (_Portion(), "Hello")

    export_kwargs = {"side_effect": export_side_effect} if export_side_effect is not None else {"return_value": xhtml}

    with ExitStack() as stack:
        stack.enter_context(patch("plugin.writer.html_export.new_blank_writer", return_value=temp_doc))
        stack.enter_context(patch("plugin.writer.html_export._visible_portions", side_effect=portions_fn))
        stack.enter_context(patch("plugin.writer.html_export._element_overlaps", return_value=True))
        stack.enter_context(
            patch("plugin.writer.html_export._trim_to_source", side_effect=lambda t, el, para_text, src: (0, len(para_text)))
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
    from unittest.mock import MagicMock

    model = MagicMock()
    model.getText.return_value.getString.return_value = text
    return model


def test_document_to_content_fails_loudly_when_both_exports_fail():
    """#63: both exports failing returned "" and get_document_content said ok with empty content."""
    from unittest.mock import patch

    import pytest

    from plugin.framework.errors import ToolExecutionError
    from plugin.writer import html_export

    model = _content_doc()
    model.storeToURL.side_effect = OSError("No space left on device")
    with patch.object(html_export, "_export_xhtml", side_effect=PermissionError("Permission denied")), \
         pytest.raises(ToolExecutionError) as err:
        html_export.document_to_content(model, None)
    message = str(err.value)
    assert "Permission denied" in message and "No space left on device" in message


def test_empty_xhtml_export_of_a_document_with_text_falls_back():
    from unittest.mock import patch

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
    assert warnings == [
        "XHTML export failed; content came from the legacy HTML export and has no style tokens."
    ]


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
    from unittest.mock import patch

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
    from plugin.writer.html_export import _apply_image_export_options

    data = "data:image/png;base64," + "QUJD" * 100
    html = '<h1><a id="a__%s"><span/></a><img src="%s"/>T</h1>' % ("QUJD" * 100, data)
    with_images = _apply_image_export_options(html, include_images=True)
    assert data in with_images and "a__QUJD" not in with_images
    without = _apply_image_export_options(html, include_images=False)
    assert "QUJD" not in without and 'src=""' in without


def test_xtext_to_content_propagates_export_error_and_closes():
    """The export catch used to return "" and the header tool said ok."""
    from plugin.framework.errors import ToolExecutionError
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
    from plugin.framework.errors import ToolExecutionError
    from plugin.writer import html_export

    with (
        patch.object(html_export, "new_blank_writer", return_value=None),
        pytest.raises(ToolExecutionError, match="no scratch document"),
    ):
        html_export.xtext_to_content(MagicMock(), MagicMock(), MagicMock())


def test_range_export_raises_when_scratch_missing():
    from plugin.framework.errors import ToolExecutionError

    with (
        patch("plugin.writer.html_export.new_blank_writer", return_value=None),
        pytest.raises(ToolExecutionError, match="no scratch document"),
    ):
        _range_to_content_via_temp_doc(MagicMock(), MagicMock(), 0, 5, None)


def test_range_offset_failure_is_not_a_successful_slice():
    """A failed gap used to leave running short and return the wrong slice as ok."""
    from plugin.framework.errors import ToolExecutionError

    class _GapCursor:
        def gotoRange(self, *_args: object, **_kwargs: object) -> None:
            raise RuntimeError("offset")

    text = MagicMock()
    para = _para("Standard")
    text.createEnumeration.return_value = _Enum([para])
    text.createTextCursor.return_value = MagicMock()
    text.createTextCursorByRange.return_value = _GapCursor()
    model = MagicMock()
    model.getText.return_value = text
    temp = MagicMock()
    temp.getText.return_value = MagicMock()

    def _portions(element, truncated_out=None):
        yield (_Portion(), "Hello")

    with (
        patch("plugin.writer.html_export.new_blank_writer", return_value=temp),
        patch("plugin.writer.html_export._visible_portions", side_effect=_portions),
        pytest.raises(ToolExecutionError, match="offset"),
    ):
        _range_to_content_via_temp_doc(model, MagicMock(), 0, 5, None)
    temp.close.assert_called_once_with(True)


class _DisposedException(Exception):
    pass


def test_full_export_disposal_does_not_fall_back():
    from plugin.framework.errors import DocumentDisposedError
    from plugin.writer import html_export

    model = _content_doc()
    with (
        patch.object(html_export, "_export_xhtml", side_effect=_DisposedException("disposed")),
        pytest.raises(DocumentDisposedError),
    ):
        html_export.document_to_content(model, None)
    model.storeToURL.assert_not_called()


def test_starwriter_disposal_is_not_wrapped():
    from plugin.framework.errors import DocumentDisposedError
    from plugin.writer import html_export

    model = _content_doc()
    model.storeToURL.side_effect = _DisposedException("disposed")
    with (
        patch.object(html_export, "_export_xhtml", side_effect=PermissionError("Permission denied")),
        pytest.raises(DocumentDisposedError),
    ):
        html_export.document_to_content(model, None)


def test_range_xhtml_disposal_does_not_fall_back():
    from plugin.framework.errors import DocumentDisposedError

    temp_doc = MagicMock()
    with pytest.raises(DocumentDisposedError):
        _run_range_export(
            [_para("Standard")],
            temp_doc=temp_doc,
            export_side_effect=_DisposedException("disposed"),
            passthrough_semantic=False,
        )
    temp_doc.storeToURL.assert_not_called()


def test_offset_window_slices_by_running_offset():
    from plugin.writer.html_export import _offset_window

    side_effects = ["", "Hello\n", "World\n"]
    text = MagicMock()
    with patch("plugin.writer.html_export.get_string_without_tracked_deletions", side_effect=side_effects):
        past_end, window = _offset_window(text, 2, 8)
        e1, e2, e3 = MagicMock(), MagicMock(), MagicMock()
        assert window(e1, "Hello") == (2, 5)
        assert window(e2, "World") == (0, 2)
        assert window(e3, "Next") is None
        assert past_end(e1) is False


@pytest.mark.parametrize(
    "exc, expected, match",
    [
        (RuntimeError("IllegalArgumentException"), ToolExecutionError, "Could not read that part"),
        (_DisposedException("disposed"), DocumentDisposedError, None),
    ],
)
def test_selection_compare_failure_is_not_a_silent_skip(exc, expected, match):
    temp_doc = MagicMock()
    temp_text = MagicMock()
    temp_doc.getText.return_value = temp_text
    temp_text.createTextCursor.return_value = MagicMock()
    text = MagicMock()
    text.createEnumeration.return_value = _Enum([_para("Standard")])
    text.compareRegionStarts.side_effect = exc
    source = MagicMock()
    source.getText.return_value = text

    def _portions(element, truncated_out=None):
        yield (_Portion(), "Hello")

    with (
        patch("plugin.writer.html_export.new_blank_writer", return_value=temp_doc),
        patch("plugin.writer.html_export._visible_portions", side_effect=_portions),
        patch("plugin.writer.html_export._trim_to_source", side_effect=lambda text, element, para_text, source: (0, len(para_text))),
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
    element.getStart.side_effect = _DisposedException("disposed")
    with pytest.raises(_DisposedException):
        _trim_to_source(MagicMock(), element, "Hello", MagicMock())


def test_range_export_does_not_read_para_style_on_a_table():
    """SwXTextTable has no ParaStyleName. The walk must not abort on it."""
    table = MagicMock()
    table.supportsService.side_effect = lambda name: name == "com.sun.star.text.TextTable"

    def _portions(element, truncated_out=None):
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


def test_extract_page_number_fields_reads_properties():
    from plugin.writer.html_export import extract_page_number_fields

    field = MagicMock()
    field.supportsService.return_value = True
    field.getPropertyValue.side_effect = lambda name: {"SubType": 2, "Offset": 1, "NumberingType": 3}[name]
    portion = MagicMock()
    portion.getPropertyValue.side_effect = lambda name: "TextField" if name == "TextPortionType" else field
    para = MagicMock()
    para.supportsService.return_value = False
    para.createEnumeration.return_value = _Enum([portion])
    text = MagicMock()
    text.createEnumeration.return_value = _Enum([para])
    assert extract_page_number_fields(text) == [
        {"SubType": 2, "Offset": 1, "NumberingType": 3}
    ]


def test_extract_page_number_fields_reads_a_table_cell():
    from plugin.writer.html_export import extract_page_number_fields

    field = MagicMock()
    field.supportsService.return_value = True
    field.getPropertyValue.side_effect = lambda name: {"SubType": 1, "Offset": 2, "NumberingType": 4}[name]
    portion = MagicMock()
    portion.getPropertyValue.side_effect = lambda name: "TextField" if name == "TextPortionType" else field
    para = MagicMock()
    para.supportsService.return_value = False
    para.createEnumeration.return_value = _Enum([portion])

    cell = MagicMock()
    cell.createEnumeration.return_value = _Enum([para])

    table = MagicMock()
    table.supportsService.side_effect = lambda s: s == "com.sun.star.text.TextTable"
    table.getCellNames.return_value = ("A1",)
    table.getCellByName.return_value = cell

    text = MagicMock()
    text.createEnumeration.return_value = _Enum([table])

    assert extract_page_number_fields(text) == [
        {"SubType": 1, "Offset": 2, "NumberingType": 4}
    ]


def test_extract_page_number_fields_no_field_returns_empty():
    from plugin.writer.html_export import extract_page_number_fields

    portion = MagicMock()
    portion.getPropertyValue.side_effect = lambda name: "Text" if name == "TextPortionType" else None
    para = MagicMock()
    para.supportsService.return_value = False
    para.createEnumeration.return_value = _Enum([portion])
    text = MagicMock()
    text.createEnumeration.return_value = _Enum([para])

    assert extract_page_number_fields(text) == []


def test_xtext_injects_nonzero_page_offset():
    from plugin.writer import html_export

    html = '<p><span title="page-number"></span></p>'
    temp = MagicMock()
    temp.getText.return_value = MagicMock()
    fields = [{"SubType": 2, "Offset": 1, "NumberingType": 4}]
    with (
        patch.object(html_export, "new_blank_writer", return_value=temp),
        patch.object(html_export.range_copy, "copy_xtext_into_doc"),
        patch.object(html_export, "extract_page_number_fields", return_value=fields),
        patch.object(html_export, "document_to_content", return_value=html),
    ):
        out = html_export.xtext_to_content(MagicMock(), MagicMock(), MagicMock())
    assert 'data-offset="1"' in out
    assert 'data-subtype="NEXT"' in out
    assert "data-numbering-type" not in out


def test_inject_page_number_attributes_defaults_omitted():
    inp = '<p><span title="page-number"></span></p>'
    fields = [{"SubType": 1, "Offset": 0, "NumberingType": 4}]
    assert _inject_page_number_attributes(inp, fields) == inp


def test_inject_page_number_attributes_non_defaults_in_order():
    inp = '<p><span title="page-number"></span></p>'
    fields = [{"SubType": 2, "Offset": 1, "NumberingType": 3}]
    out = _inject_page_number_attributes(inp, fields)
    assert '<span title="page-number" data-subtype="NEXT" data-offset="1" data-numbering-type="ROMAN_LOWER"></span>' in out


def test_inject_page_number_attributes_self_closing_tag():
    inp = '<p><span title="page-number"/></p>'
    fields = [{"SubType": 2, "Offset": 1, "NumberingType": 3}]
    out = _inject_page_number_attributes(inp, fields)
    assert '<span title="page-number" data-subtype="NEXT" data-offset="1" data-numbering-type="ROMAN_LOWER"/>' in out


def test_inject_page_number_attributes_count_mismatch_returns_unchanged():
    two_spans = '<p><span title="page-number"></span><span title="page-number"></span></p>'
    one_field = [{"SubType": 2, "Offset": 1, "NumberingType": 3}]
    assert _inject_page_number_attributes(two_spans, one_field) == two_spans

    one_span = '<p><span title="page-number"></span></p>'
    two_fields = [
        {"SubType": 2, "Offset": 1, "NumberingType": 3},
        {"SubType": 1, "Offset": 0, "NumberingType": 4},
    ]
    assert _inject_page_number_attributes(one_span, two_fields) == one_span


def test_inject_page_number_attributes_empty_fields_returns_unchanged():
    inp = '<p><span title="page-number"></span></p>'
    assert _inject_page_number_attributes(inp, []) == inp


def test_page_number_converters():
    from types import SimpleNamespace

    from plugin.writer.html_export import (
        _page_number_numbering_type_to_name,
        _page_number_subtype_to_name,
    )

    assert _page_number_subtype_to_name(2) == "NEXT"
    assert _page_number_subtype_to_name("2") == "NEXT"
    assert _page_number_subtype_to_name("next") == "NEXT"
    assert _page_number_subtype_to_name(SimpleNamespace(value="PREV")) == "PREV"
    assert _page_number_subtype_to_name(99) == "CURRENT"

    assert _page_number_numbering_type_to_name(4) == "ARABIC"
    assert _page_number_numbering_type_to_name("ARABIC") == "ARABIC"
    assert _page_number_numbering_type_to_name("roman_upper") == "ROMAN_UPPER"
    assert _page_number_numbering_type_to_name(99) == "ARABIC"


def test_uno_read_probes_raise_on_disposal_and_default_otherwise():
    from plugin.framework.errors import DocumentDisposedError
    from plugin.writer.html_export import _raise_if_empty_export
    from plugin.writer.range_copy import supports_service, _writer_table_copy_layout

    class DisposedException(Exception):
        pass

    class RuntimeException(Exception):
        pass

    closed = MagicMock()
    closed.supportsService.side_effect = DisposedException("closed")
    with pytest.raises(DocumentDisposedError):
        supports_service(closed, "com.sun.star.text.TextTable")

    odd = MagicMock()
    odd.supportsService.side_effect = RuntimeException("uno")
    assert supports_service(odd, "com.sun.star.text.TextTable") is False

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
    autostyle_mock2.assert_called_once_with(temp_doc2)
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


import base64
from plugin.writer.html_export import _apply_image_export_options, strip_embedded_image_data


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
