import re

from unittest.mock import MagicMock, patch

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
