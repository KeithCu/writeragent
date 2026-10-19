import re
from unittest.mock import MagicMock, patch

import pytest

from plugin.writer.html_export import _range_to_content_via_temp_doc, inject_ruby_into_html, xtext_to_content


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


def _range_export(style: str, *, has_style: bool, set_style_raises: bool = False) -> tuple[str, MagicMock]:
    """Run a two-paragraph selection export against a mocked scratch document."""
    temp_doc = MagicMock()
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

    text = MagicMock()
    text.createEnumeration.return_value = _Enum([_para(style), _para(style)])
    source = MagicMock()
    source.getText.return_value = text

    def _portions(element, truncated_out=None):
        yield (_Portion(), "Hello")

    with (
        patch("plugin.writer.html_export.new_blank_writer", return_value=temp_doc),
        patch("plugin.writer.html_export._visible_portions", side_effect=_portions),
        patch("plugin.writer.html_export._element_overlaps", return_value=True),
        patch("plugin.writer.html_export._trim_to_source", side_effect=lambda text, element, para_text, source: (0, len(para_text))),
        patch("plugin.writer.html_export._starts_at_or_after", return_value=False),
        patch("plugin.writer.html_export._paint_direct_formatting"),
        patch("plugin.writer.html_export._hyperlink_urls", return_value=[]),
        patch("plugin.writer.html_export._ruby_spans_in_window", return_value=[]),
        patch("plugin.writer.html_export._export_xhtml", return_value="<p>Hello</p><p>Hello</p>"),
        patch("plugin.writer.html_export._autostyle_maps", return_value=({}, {}, False)),
        patch("plugin.writer.html_export._inject_exported_math_tex", side_effect=lambda model, ctx, content: content),
        patch(
            "plugin.writer.html_export.xhtml_post.xhtml_to_semantic_html",
            side_effect=lambda xhtml, parents, overrides: xhtml,
        ),
    ):
        html = _range_to_content_via_temp_doc(
            MagicMock(), MagicMock(), 0, 0, None, None, source_range=source,
        )
    return html, temp_cursor


def test_range_export_keeps_text_when_paragraph_style_is_missing():
    """A style absent from the scratch doc must not turn the selection into \"\"."""
    html, cursor = _range_export("Petition Heading", has_style=False)
    assert "Hello" in html
    assert html != ""
    styled = [call for call in cursor.setPropertyValue.call_args_list if call.args and call.args[0] == "ParaStyleName"]
    assert styled == []
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


def test_ops_uno_skips_windows_leftover_text_offsets() -> None:
    """GHA 34689136372: leftover reuse offset mismatch, not a product range bug."""
    from pathlib import Path

    src = Path(__file__).with_name("test_ops_uno.py").read_text(encoding="utf-8")
    assert "skip_windows_leftover_hidden_load" in src
    assert "ops_uno leftover text offsets" in src
    assert "34689136372" in src
    assert "normalize_linebreaks" in src
    assert "35466498641" in src


def test_html_export_uno_skips_windows_leftover_hidden_temp_doc() -> None:
    """GHA 34690797019: leftover Hidden _default hung on temp_doc.close."""
    from pathlib import Path

    src = Path(__file__).with_name("test_html_export_uno.py").read_text(encoding="utf-8")
    assert "skip_windows_leftover_hidden_load" in src
    assert "html_export Hidden _default temp_doc" in src
    assert "34690797019" in src


def test_inject_ruby_rewrites_glued_xhtml():
    """Full XHTML filter concatenates base+reading; rewrite must unglue them."""
    html = '<p data-lo-style="Standard">漢字かんじです</p>'
    out = inject_ruby_into_html(html, [("漢字", "かんじ")])
    assert "漢字かんじです" not in out
    assert "<ruby>漢字<rt>かんじ</rt></ruby>です" in out


def test_inject_ruby_wraps_base_when_reading_was_dropped():
    """Range/portion-copy drops RubyText; wrap the surviving base."""
    html = '<p data-lo-style="Standard">漢字です</p>'
    out = inject_ruby_into_html(html, [("漢字", "かんじ")])
    assert "<ruby>漢字<rt>かんじ</rt></ruby>です" in out
    assert "漢字かんじです" not in out


def test_inject_ruby_wraps_styled_base_when_reading_was_dropped():
    """Contiguous base inside a span stays inside that span (same as glued-in-span)."""
    html = '<p><span style="font-weight:bold">漢字</span>です</p>'
    out = inject_ruby_into_html(html, [("漢字", "かんじ")])
    assert '<span style="font-weight:bold"><ruby>漢字<rt>かんじ</rt></ruby></span>です' in out


def test_inject_ruby_keeps_inner_tags_on_base():
    html = '<p><span style="font-weight:bold">漢字</span>かんじです</p>'
    out = inject_ruby_into_html(html, [("漢字", "かんじ")])
    assert '<span style="font-weight:bold"><ruby>漢字<rt>かんじ</rt></ruby></span>です' in out
    assert "漢字かんじ" not in out.replace("<rt>かんじ</rt>", "")


def test_inject_ruby_inside_one_span_is_contiguous():
    html = '<p><span style="font-weight:bold">漢字かんじです</span></p>'
    out = inject_ruby_into_html(html, [("漢字", "かんじ")])
    assert '<span style="font-weight:bold"><ruby>漢字<rt>かんじ</rt></ruby>です</span>' in out


def test_inject_ruby_is_idempotent():
    html = "<p><ruby>漢字<rt>かんじ</rt></ruby>です</p>"
    out = inject_ruby_into_html(html, [("漢字", "かんじ")])
    assert out.count("<ruby>") == 1
    assert out == html


def test_inject_ruby_sequential_spans():
    html = "<p>東京とうきょうと京都きょうと</p>"
    out = inject_ruby_into_html(html, [("東京", "とうきょう"), ("京都", "きょうと")])
    assert "<ruby>東京<rt>とうきょう</rt></ruby>と<ruby>京都<rt>きょうと</rt></ruby>" in out


def test_inject_ruby_empty_is_noop():
    assert inject_ruby_into_html("<p>漢字です</p>", []) == "<p>漢字です</p>"
    assert inject_ruby_into_html("", [("漢字", "かんじ")]) == ""
    assert inject_ruby_into_html("<p>x</p>", [("漢字", "")]) == "<p>x</p>"


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
        html_export.document_to_content(model, None, None)
    message = str(err.value)
    assert "Permission denied" in message and "No space left on device" in message


def test_empty_xhtml_export_of_a_document_with_text_falls_back():
    from unittest.mock import patch

    from plugin.writer import html_export

    with patch.object(html_export, "_export_xhtml", return_value=""), \
         patch.object(html_export, "_autostyle_maps", return_value=({}, {}, None)), \
         patch.object(html_export.format_mod, "_with_temp_buffer") as buf, \
         patch("builtins.open") as opener:
        buf.return_value.__enter__.return_value = ("/tmp/x.html", "file:///tmp/x.html")
        opener.return_value.__enter__.return_value.read.return_value = "<p>Texto do documento</p>"
        out = html_export.document_to_content(_content_doc(), None, None)
    assert "Texto do documento" in out


def test_an_empty_document_still_reads_as_empty():
    from unittest.mock import patch

    from plugin.writer import html_export

    with patch.object(html_export, "_export_xhtml", return_value=""), \
         patch.object(html_export, "_autostyle_maps", return_value=({}, {}, None)):
        assert html_export.document_to_content(_content_doc(""), None, None) == ""
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


def test_copy_field_into_sets_offset():
    from plugin.writer.html_export import _copy_field_into

    src_field = MagicMock()
    src_field.getSupportedServiceNames.return_value = ["com.sun.star.text.textfield.PageNumber"]

    def _get_prop(name: str):
        if name == "Offset":
            return 1
        if name == "SubType":
            return 0
        if name == "NumberingType":
            return 2
        return None

    src_field.getPropertyValue.side_effect = _get_prop

    dest_doc = MagicMock()
    new_field = MagicMock()
    dest_doc.createInstance.return_value = new_field
    dest_text = MagicMock()
    dest_cursor = MagicMock()

    res = _copy_field_into(dest_doc, dest_text, dest_cursor, src_field)
    assert res is True
    new_field.setPropertyValue.assert_any_call("Offset", 1)
    new_field.setPropertyValue.assert_any_call("SubType", 0)
    new_field.setPropertyValue.assert_any_call("NumberingType", 2)


def test_copy_flow_into_xtext_one_paragraph_text_then_field():
    from plugin.writer.html_export import (
        _COPIED_CHAR_PROPERTIES,
        _COPIED_PARA_PROPERTIES,
        _copy_flow_into_xtext,
    )

    doc = MagicMock()
    src_text = MagicMock()

    portion_text = MagicMock()
    portion_text.getPropertyValue.return_value = "Text"
    portion_text.getString.return_value = "Page "

    portion_field = MagicMock()
    portion_field.getPropertyValue.side_effect = (
        lambda name: "TextField" if name == "TextPortionType" else MagicMock()
    )

    para = MagicMock()
    para.supportsService.return_value = False
    para.createEnumeration.return_value = _Enum([portion_text, portion_field])

    src_text.createEnumeration.return_value = _Enum([para])

    dest_text = MagicMock()
    cursor = MagicMock()
    dest_text.createTextCursor.return_value = cursor

    with patch("plugin.writer.html_export._copy_field_into", return_value=True) as mock_field_into, \
         patch("plugin.writer.html_export._copy_properties") as mock_props, \
         patch("plugin.writer.html_export.new_blank_writer") as mock_blank:
        res = _copy_flow_into_xtext(doc, src_text, dest_text)

    assert res == 1
    cursor.setString.assert_called_with("")
    dest_text.insertString.assert_any_call(cursor, "Page ", False)
    dest_text.insertControlCharacter.assert_not_called()
    mock_field_into.assert_called_once()
    mock_blank.assert_not_called()

    prop_tuples = [call.args[2] for call in mock_props.call_args_list]
    assert _COPIED_CHAR_PROPERTIES in prop_tuples
    assert _COPIED_PARA_PROPERTIES in prop_tuples


def test_copy_flow_into_xtext_breaks_only_between_paragraphs():
    from plugin.writer.html_export import _PARAGRAPH_BREAK, _copy_flow_into_xtext

    doc = MagicMock()
    src_text = MagicMock()

    p1_portion = MagicMock()
    p1_portion.getPropertyValue.return_value = "Text"
    p1_portion.getString.return_value = "A"
    para1 = MagicMock()
    para1.supportsService.return_value = False
    para1.createEnumeration.return_value = _Enum([p1_portion])

    p2_portion = MagicMock()
    p2_portion.getPropertyValue.return_value = "Text"
    p2_portion.getString.return_value = "B"
    para2 = MagicMock()
    para2.supportsService.return_value = False
    para2.createEnumeration.return_value = _Enum([p2_portion])

    src_text.createEnumeration.return_value = _Enum([para1, para2])

    dest_text = MagicMock()
    cursor = MagicMock()
    dest_text.createTextCursor.return_value = cursor

    with patch("plugin.writer.html_export._copy_properties"):
        _copy_flow_into_xtext(doc, src_text, dest_text)

    dest_text.insertControlCharacter.assert_called_once_with(cursor, _PARAGRAPH_BREAK, False)

    insert_calls = [c.args[1] for c in dest_text.insertString.call_args_list]
    assert insert_calls == ["A", "B"]

    calls = dest_text.mock_calls
    break_idx = None
    insert_a_idx = None
    for idx, c in enumerate(calls):
        if c[0] == "insertControlCharacter":
            break_idx = idx
        elif c[0] == "insertString" and c.args[1] == "A":
            insert_a_idx = idx

    assert break_idx is not None
    assert insert_a_idx is not None
    assert insert_a_idx < break_idx


def test_xtext_to_content_propagates_export_error_and_closes():
    """The export catch used to return "" and the header tool said ok."""
    from plugin.framework.errors import ToolExecutionError
    from plugin.writer import html_export

    temp = MagicMock()
    temp.getText.return_value = MagicMock()
    with (
        patch.object(html_export, "_open_hidden_writer", return_value=temp),
        patch.object(html_export, "_copy_xtext_into_doc"),
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
        patch.object(html_export, "_open_hidden_writer", return_value=None),
        pytest.raises(ToolExecutionError, match="no scratch document"),
    ):
        html_export.xtext_to_content(MagicMock(), MagicMock(), MagicMock())


def test_range_export_raises_when_scratch_missing():
    from plugin.framework.errors import ToolExecutionError

    with (
        patch("plugin.writer.html_export.new_blank_writer", return_value=None),
        pytest.raises(ToolExecutionError, match="no scratch document"),
    ):
        _range_to_content_via_temp_doc(MagicMock(), MagicMock(), 0, 5, None, None)


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
        _range_to_content_via_temp_doc(model, MagicMock(), 0, 5, None, None)
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
        html_export.document_to_content(model, None, None)
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
        html_export.document_to_content(model, None, None)


def test_range_xhtml_disposal_does_not_fall_back():
    from plugin.framework.errors import DocumentDisposedError

    temp_doc = MagicMock()
    temp_text = MagicMock()
    temp_doc.getText.return_value = temp_text
    temp_text.createTextCursor.return_value = MagicMock()
    text = MagicMock()
    text.createEnumeration.return_value = _Enum([_para("Standard")])
    source = MagicMock()
    source.getText.return_value = text

    def _portions(element, truncated_out=None):
        yield (_Portion(), "Hello")

    with (
        patch("plugin.writer.html_export.new_blank_writer", return_value=temp_doc),
        patch("plugin.writer.html_export._visible_portions", side_effect=_portions),
        patch("plugin.writer.html_export._element_overlaps", return_value=True),
        patch("plugin.writer.html_export._trim_to_source", side_effect=lambda text, element, para_text, source: (0, len(para_text))),
        patch("plugin.writer.html_export._starts_at_or_after", return_value=False),
        patch("plugin.writer.html_export._paint_direct_formatting"),
        patch("plugin.writer.html_export._hyperlink_urls", return_value=[]),
        patch("plugin.writer.html_export._ruby_spans_in_window", return_value=[]),
        patch("plugin.writer.html_export._export_xhtml", side_effect=_DisposedException("disposed")),
        pytest.raises(DocumentDisposedError),
    ):
        _range_to_content_via_temp_doc(
            MagicMock(), MagicMock(), 0, 0, None, None, source_range=source,
        )
    temp_doc.storeToURL.assert_not_called()


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
    text = MagicMock()
    text.createEnumeration.return_value = _Enum([table, _para("Standard")])
    source = MagicMock()
    source.getText.return_value = text

    def _portions(element, truncated_out=None):
        if element is not table:
            yield (_Portion(), "Hello")

    temp_doc = MagicMock()
    temp_text = MagicMock()
    temp_cursor = MagicMock()
    temp_doc.getText.return_value = temp_text
    temp_text.createTextCursor.return_value = temp_cursor
    styles = MagicMock()
    styles.hasByName.return_value = True
    temp_doc.getStyleFamilies.return_value.getByName.return_value = styles
    with (
        patch("plugin.writer.html_export.new_blank_writer", return_value=temp_doc),
        patch("plugin.writer.html_export._visible_portions", side_effect=_portions),
        patch("plugin.writer.html_export._element_overlaps", return_value=True),
        patch("plugin.writer.html_export._trim_to_source", side_effect=lambda text, element, para_text, source: (0, len(para_text))),
        patch("plugin.writer.html_export._starts_at_or_after", return_value=False),
        patch("plugin.writer.html_export._paint_direct_formatting"),
        patch("plugin.writer.html_export._hyperlink_urls", return_value=[]),
        patch("plugin.writer.html_export._ruby_spans_in_window", return_value=[]),
        patch("plugin.writer.html_export._export_xhtml", return_value="<p>Hello</p>"),
        patch("plugin.writer.html_export._autostyle_maps", return_value=({}, {}, False)),
        patch("plugin.writer.html_export._inject_exported_math_tex", side_effect=lambda model, ctx, content: content),
        patch(
            "plugin.writer.html_export.xhtml_post.xhtml_to_semantic_html",
            side_effect=lambda xhtml, parents, overrides: xhtml,
        ),
    ):
        html = _range_to_content_via_temp_doc(
            MagicMock(), MagicMock(), 0, 0, None, None, source_range=source,
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
    from plugin.writer import html_export

    long_html = '<p><a href="https://example.com/very-long-path">visible</a></p>'
    with (
        patch.object(html_export, "_export_xhtml", return_value=long_html),
        patch.object(html_export, "_autostyle_maps", return_value=({}, {}, False)),
        patch.object(html_export, "_inject_exported_math_tex", side_effect=lambda model, ctx, content: content),
        patch.object(html_export, "_inject_ruby_from_model", side_effect=lambda model, content, fodt_has_ruby=None: content),
        patch(
            "plugin.writer.html_export.xhtml_post.xhtml_to_semantic_html",
            side_effect=lambda xhtml, parents, overrides: xhtml,
        ),
    ):
        out = html_export.document_to_content(_content_doc(), None, None, max_chars=12)
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


def test_xtext_injects_nonzero_page_offset():
    from plugin.writer import html_export

    html = '<p><span title="page-number"></span></p>'
    temp = MagicMock()
    temp.getText.return_value = MagicMock()
    fields = [{"SubType": 2, "Offset": 1, "NumberingType": 4}]
    with (
        patch.object(html_export, "_open_hidden_writer", return_value=temp),
        patch.object(html_export, "_copy_xtext_into_doc"),
        patch.object(html_export, "extract_page_number_fields", return_value=fields),
        patch.object(html_export, "document_to_content", return_value=html),
    ):
        out = html_export.xtext_to_content(MagicMock(), MagicMock(), MagicMock())
    assert 'data-offset="1"' in out
    assert 'data-subtype="NEXT"' in out
    assert "data-numbering-type" not in out


def test_copy_xtext_into_doc_does_not_select_and_clones_graphic():
    from plugin.writer.html_export import _copy_xtext_into_doc

    src_text = MagicMock()
    src_text.createEnumeration.return_value = _Enum([])
    src_doc = MagicMock()
    graphic = object()
    shape = MagicMock()
    shape.getAnchor.return_value.getText.return_value = src_text
    shape.getAnchor.return_value.getStart.return_value = object()
    shape.supportsService.side_effect = lambda name: name == "com.sun.star.text.TextGraphicObject"
    shape.getPropertyValue.side_effect = lambda name: {
        "AnchorType": 1, "Graphic": graphic, "Width": 100, "Height": 100,
    }.get(name)
    draw_page = MagicMock()
    draw_page.getCount.return_value = 1
    draw_page.getByIndex.return_value = shape
    src_doc.getDrawPage.return_value = draw_page

    dest_text = MagicMock()
    dest_cursor = MagicMock()
    dest_text.createTextCursor.return_value = dest_cursor
    dest_text.getString.return_value = ""
    dest_doc = MagicMock()
    dest_doc.getText.return_value = dest_text
    clone = MagicMock()
    dest_doc.createInstance.return_value = clone

    measure = MagicMock()
    measure.getString.return_value = ""
    src_text.createTextCursor.return_value = measure
    src_text.getString.return_value = ""

    _copy_xtext_into_doc(src_doc, src_text, dest_doc)

    src_doc.getCurrentController().select.assert_not_called()
    dest_doc.createInstance.assert_called_with("com.sun.star.text.TextGraphicObject")
    clone.setPropertyValue.assert_any_call("Graphic", graphic)
    dest_text.insertTextContent.assert_called()




def test_uno_read_probes_raise_on_disposal_and_default_otherwise():
    from plugin.framework.errors import DocumentDisposedError
    from plugin.writer.html_export import _raise_if_empty_export, _supports_service, _writer_table_copy_layout

    class DisposedException(Exception):
        pass

    class RuntimeException(Exception):
        pass

    closed = MagicMock()
    closed.supportsService.side_effect = DisposedException("closed")
    with pytest.raises(DocumentDisposedError):
        _supports_service(closed, "com.sun.star.text.TextTable")

    odd = MagicMock()
    odd.supportsService.side_effect = RuntimeException("uno")
    assert _supports_service(odd, "com.sun.star.text.TextTable") is False

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
    from plugin.writer.html_export import _substitute_math_placeholders
    from plugin.writer.math.math_mml_export import MathExportResult

    p1 = chr(0xE000)
    p2 = chr(0xE001)
    p3 = chr(0xE002)

    html = f"<p>{p1} and &#xe001; and &#57346;</p>"
    records = [
        (p1, "a + b", False),
        (p2, "x", True),
        (p3, "bad starmath", False),
    ]

    def fake_convert(ctx: object, starmath: str) -> MathExportResult:
        if starmath == "a + b":
            return MathExportResult(True, "a + b", None, None)
        if starmath == "x":
            return MathExportResult(True, "x", None, None)
        return MathExportResult(False, None, None, "error")

    with patch("plugin.writer.math.math_mml_export.convert_starmath_to_latex", side_effect=fake_convert):
        out = _substitute_math_placeholders(html, records, object())

    assert "$a + b$" in out
    assert "$$x$$" in out
    assert "[Math export fallback] bad starmath" in out

def test_range_export_skips_flat_odf_when_no_autostyle():
    """Flat ODF is skipped when XHTML contains no paragraph-P<digits> autostyle classes."""
    text = MagicMock()
    text.createEnumeration.side_effect = lambda: _Enum([_para("Text body")])
    source = MagicMock()
    source.getText.return_value = text

    def _portions(element, truncated_out=None):
        yield (_Portion(), "Hello")

    temp_doc = MagicMock()
    temp_text = MagicMock()
    temp_cursor = MagicMock()
    temp_doc.getText.return_value = temp_text
    temp_text.createTextCursor.return_value = temp_cursor
    styles = MagicMock()
    styles.hasByName.return_value = True
    temp_doc.getStyleFamilies.return_value.getByName.return_value = styles

    # Case 1: XHTML with paragraph-Text_20_body does not call _autostyle_maps
    xhtml_named = '<html><body><p class="paragraph-Text_20_body">Hello</p></body></html>'
    with (
        patch("plugin.writer.html_export.new_blank_writer", return_value=temp_doc),
        patch("plugin.writer.html_export._visible_portions", side_effect=_portions),
        patch("plugin.writer.html_export._element_overlaps", return_value=True),
        patch("plugin.writer.html_export._trim_to_source", side_effect=lambda text, element, para_text, source: (0, len(para_text))),
        patch("plugin.writer.html_export._starts_at_or_after", return_value=False),
        patch("plugin.writer.html_export._paint_direct_formatting"),
        patch("plugin.writer.html_export._hyperlink_urls", return_value=[]),
        patch("plugin.writer.html_export._ruby_spans_in_window", return_value=[]),
        patch("plugin.writer.html_export._export_xhtml", return_value=xhtml_named),
        patch("plugin.writer.html_export._autostyle_maps", return_value=({"P1": "Standard"}, {"P1": "margin:0"}, False)) as autostyle_mock,
        patch("plugin.writer.html_export._inject_exported_math_tex", side_effect=lambda model, ctx, content: content),
        patch("plugin.writer.html_export.xhtml_post.xhtml_to_semantic_html", side_effect=lambda xhtml, parents, overrides: xhtml) as semantic_mock,
    ):
        _range_to_content_via_temp_doc(
            MagicMock(), MagicMock(), 0, 0, None, None, source_range=source,
        )
        autostyle_mock.assert_not_called()
        semantic_mock.assert_called_once_with(xhtml_named, {}, {})

    # Case 2: XHTML with paragraph-P1 calls _autostyle_maps and forwards maps to semantic_html
    xhtml_auto = '<html><body><p class="paragraph-P1">Hello</p></body></html>'
    expected_parents = {"P1": "Standard"}
    expected_overrides = {"P1": "color:red"}
    with (
        patch("plugin.writer.html_export.new_blank_writer", return_value=temp_doc),
        patch("plugin.writer.html_export._visible_portions", side_effect=_portions),
        patch("plugin.writer.html_export._element_overlaps", return_value=True),
        patch("plugin.writer.html_export._trim_to_source", side_effect=lambda text, element, para_text, source: (0, len(para_text))),
        patch("plugin.writer.html_export._starts_at_or_after", return_value=False),
        patch("plugin.writer.html_export._paint_direct_formatting"),
        patch("plugin.writer.html_export._hyperlink_urls", return_value=[]),
        patch("plugin.writer.html_export._ruby_spans_in_window", return_value=[]),
        patch("plugin.writer.html_export._export_xhtml", return_value=xhtml_auto),
        patch("plugin.writer.html_export._autostyle_maps", return_value=(expected_parents, expected_overrides, False)) as autostyle_mock,
        patch("plugin.writer.html_export._inject_exported_math_tex", side_effect=lambda model, ctx, content: content),
        patch("plugin.writer.html_export.xhtml_post.xhtml_to_semantic_html", side_effect=lambda xhtml, parents, overrides: xhtml) as semantic_mock,
    ):
        _range_to_content_via_temp_doc(
            MagicMock(), MagicMock(), 0, 0, None, None, source_range=source,
        )
        autostyle_mock.assert_called_once_with(temp_doc, None)
        semantic_mock.assert_called_once_with(xhtml_auto, expected_parents, expected_overrides)

