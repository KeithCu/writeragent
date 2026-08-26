# WriterAgent - tests for notebook Writer import helpers

from __future__ import annotations

import inspect
import re
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from plugin.notebook.writer_importer import (
    _ImportStackCursor,
    _MAX_IMAGE_DECODE_BYTES,
    _MAX_IMPORT_TEXT_CHARS,
    _PARAGRAPH_BREAK,
    _STYLE_MD_H1,
    _STYLE_MD_H2,
    _STYLE_NOTEBOOK_IN,
    _append_body_text_block,
    _append_body_paragraph,
    _append_paragraph_break_at_end,
    _apply_no_spellcheck_for_import,
    _cell_heading,
    _coerce_notebook_text,
    _create_import_para_style,
    _decode_notebook_image,
    _DEFAULT_WIDTH,
    _ensure_notebook_import_styles,
    _FIELD_HEIGHT_PAD,
    _format_in_prompt,
    _height_for_text,
    _inline_backticks_to_html,
    _iter_markdown_blocks,
    _LINE_HEIGHT,
    _looks_like_html,
    _notebook_image_payload,
    _png_pixel_size,
    _prepare_display_text,
    _resolve_para_style,
    _text_area_width_units,
    _unglue_last_paragraph,
    _WRAP_SLACK,
    _wrap_html_fragment,
    format_all_outputs,
    format_output_text,
    import_ipynb_to_writer,
)


def _writer_importer_import_logging_present() -> bool:
    """True when notebook import log.info/debug exist (stripped in ``make release`` bundles)."""
    try:
        source = Path(inspect.getfile(import_ipynb_to_writer)).read_text(encoding="utf-8")
    except OSError:
        return False
    return 'log.info("notebook import start' in source


def test_format_stream_output():
    class Out:
        output_type = "stream"
        name = "stdout"
        text = "hello\n"

    assert format_output_text(Out()) == "hello\n"


def test_format_execute_result_gets_out_prompt():
    out = {"output_type": "execute_result", "data": {"text/plain": "42"}}
    assert format_output_text(out, execution_count=3) == "Out [3]: 42"
    assert format_output_text(out) == "42"


def test_format_error_strips_ansi():
    out = {"output_type": "error", "traceback": "\x1b[31mValueError\x1b[0m: bad"}
    assert "ValueError" in format_output_text(out)
    assert "\x1b" not in format_output_text(out)


def test_format_execute_result_plain():
    out = {"output_type": "execute_result", "data": {"text/plain": "42"}}
    assert format_output_text(out) == "42"


def test_coerce_notebook_text_joins_list():
    assert _coerce_notebook_text(["a\n", "b\n"]) == "a\nb\n"


def test_prepare_display_text_truncates():
    long_text = "x" * (_MAX_IMPORT_TEXT_CHARS + 1000)
    display, truncated = _prepare_display_text(long_text)
    assert truncated is True
    assert len(display) <= _MAX_IMPORT_TEXT_CHARS + 50


def test_format_output_image_empty_for_body():
    out = {"output_type": "display_data", "data": {"image/png": "abc"}}
    assert format_output_text(out) == ""


def test_notebook_image_payload():
    data = {"image/png": "abc", "text/plain": "hi"}
    assert _notebook_image_payload(data) == ("image/png", "abc")


def test_png_pixel_size_1x1():
    # 1x1 PNG IHDR
    raw = (
        b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01"
        b"\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f\x15\xc4\x89"
    )
    assert _png_pixel_size(raw) == (1, 1)


def test_jpeg_pixel_size_reads_sof():
    from plugin.notebook.writer_importer import _jpeg_pixel_size

    # Minimal SOF0: FF C0, length, bits, height=10, width=20
    raw = b"\xff\xd8\xff\xc0\x00\x0b\x08\x00\x0a\x00\x14\x03\x01\x22\x00"
    assert _jpeg_pixel_size(raw) == (20, 10)


def test_decode_notebook_image_rejects_oversize():
    huge = "A" * (_MAX_IMAGE_DECODE_BYTES + 1)
    assert _decode_notebook_image(huge) is None


def test_format_output_plain_mime():
    out = {"output_type": "display_data", "data": {"text/html": "<p>x</p>", "text/plain": "hi"}}
    assert format_output_text(out) == "hi"


def test_format_all_outputs_joins():
    outputs = [
        {"output_type": "stream", "name": "stdout", "text": "a"},
        {"output_type": "execute_result", "data": {"text/plain": "b"}},
    ]
    text = format_all_outputs(outputs)
    assert "a" in text and "b" in text


class _FakePoint:
    def __init__(self, x, y):
        self.X = x
        self.Y = y


def test_import_stack_cursor_place_advances(monkeypatch):
    monkeypatch.setattr("plugin.notebook.writer_importer.Point", _FakePoint)
    dp = MagicMock()
    dp.getCount.return_value = 0
    stack = _ImportStackCursor(dp)
    stack.place(700)
    first_bottom = stack._max_bottom
    stack.place(600)
    assert stack.shape_count == 2
    assert first_bottom == 800 + 400 + 700
    assert stack._max_bottom == first_bottom + 400 + 600


def test_import_stack_cursor_seeds_existing_shapes(monkeypatch):
    monkeypatch.setattr("plugin.notebook.writer_importer.Point", _FakePoint)
    shape = MagicMock()
    shape.getPosition.return_value = MagicMock(Y=1000, X=0)
    shape.getSize.return_value = MagicMock(Height=500, Width=100)
    dp = MagicMock()
    dp.getCount.return_value = 1
    dp.getByIndex.return_value = shape
    stack = _ImportStackCursor(dp)
    assert stack.shape_count == 1
    assert stack._max_bottom == 1500
    stack.place(100)
    assert stack._max_bottom == 1500 + 400 + 100


def _writer_doc_mock(*, with_bookmarks: bool = False):
    body_cursor = MagicMock()
    body_text = MagicMock()
    body_text.createTextCursor.return_value = body_cursor
    doc = MagicMock()
    doc.getText.return_value = body_text
    if with_bookmarks:
        bookmarks = MagicMock()
        bookmarks.hasByName.return_value = False
        doc.getBookmarks.return_value = bookmarks
        doc.createInstance.side_effect = lambda service: MagicMock()
    return doc, body_text, body_cursor


def test_looks_like_html_detects_tags():
    # <a>/<img> stay on the markdown path so mixed cells keep headings and lists.
    assert _looks_like_html('<a href="https://example.com">x</a>') is False
    assert _looks_like_html('<img src="../images/foo.png" alt="x"/>') is False
    assert _looks_like_html("## Heading\n\n<img src=\"x.png\">\n") is False
    assert _looks_like_html("<div>raw</div>") is True
    assert _looks_like_html("## Plain markdown\n\nno tags") is False


def test_wrap_html_fragment_adds_body():
    wrapped = _wrap_html_fragment("<p>Hi</p>")
    assert "<html>" in wrapped and "<body>" in wrapped and "<p>Hi</p>" in wrapped


def test_format_in_prompt_executed_and_unexecuted():
    assert _format_in_prompt(1) == "In [1]:"
    assert _format_in_prompt(None) == "In [ ]:"


def test_height_for_text_fits_last_line():
    """15-line In[2] source must be taller than the old 380 HMM/line clip height."""
    src = "\n".join(f"line{i}" for i in range(15))
    h = _height_for_text(src)
    assert h == 16 * _LINE_HEIGHT + _FIELD_HEIGHT_PAD
    assert h > 15 * 380


def test_height_for_text_includes_one_wrap_line():
    """Two source lines get a third visual line of pad (In[3] long print wraps)."""
    h = _height_for_text("line1\nline2")
    assert h == 3 * _LINE_HEIGHT + _FIELD_HEIGHT_PAD
    assert _WRAP_SLACK == _LINE_HEIGHT


def test_code_field_uses_full_text_area_width():
    from plugin.notebook.writer_importer import _insert_code_input_in_flow

    src = inspect.getsource(_insert_code_input_in_flow)
    assert "_text_area_width_units(doc)" in src
    assert "_RUN_BUTTON_SIZE" not in src
    assert _text_area_width_units(None) == _DEFAULT_WIDTH


def test_unglue_last_paragraph_clears_keep_with_next():
    doc, _body_text, body_cursor = _writer_doc_mock()
    _unglue_last_paragraph(doc)
    body_cursor.setPropertyValue.assert_any_call("ParaKeepWithNext", False)
    body_cursor.setPropertyValue.assert_any_call("ParaKeepTogether", False)


def test_cell_heading_is_in_prompt_only():
    assert _cell_heading(1, "code") == "In [ ]:"
    assert _cell_heading(0, "markdown") == ""
    assert "Cell" not in _cell_heading(0, "code", 2)


def test_create_import_para_style_skips_existing():
    doc = MagicMock()
    para_styles = MagicMock()
    para_styles.hasByName.return_value = True
    assert _create_import_para_style(doc, para_styles, "WriterAgent Notebook In", parent_style="Text Body", property_updates={}) is True
    doc.createInstance.assert_not_called()


def test_ensure_notebook_import_styles_creates_and_resolves():
    doc = MagicMock()
    para_styles = MagicMock()
    para_styles.hasByName.return_value = False
    para_styles.getElementNames.return_value = ["Text Body", _STYLE_NOTEBOOK_IN]
    families = MagicMock()
    families.getByName.return_value = para_styles
    doc.getStyleFamilies.return_value = families
    new_style = MagicMock()
    doc.createInstance.return_value = new_style

    in_style = _ensure_notebook_import_styles(doc)

    assert doc.createInstance.call_count == 1
    assert para_styles.insertByName.call_count == 1
    assert in_style == _STYLE_NOTEBOOK_IN
    new_style.setPropertyValue.assert_any_call("ParaKeepTogether", False)
    new_style.setPropertyValue.assert_any_call("ParaKeepWithNext", False)


def test_apply_no_spellcheck_for_import_sets_zxx():
    doc = MagicMock()
    para_styles = MagicMock()
    para_styles.hasByName.return_value = True
    style = MagicMock()
    para_styles.getByName.return_value = style
    families = MagicMock()
    families.getByName.return_value = para_styles
    doc.getStyleFamilies.return_value = families

    loc = MagicMock()
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr("plugin.notebook.writer_importer._no_spellcheck_locale", lambda: loc)
        _apply_no_spellcheck_for_import(doc)

    doc.setPropertyValue.assert_any_call("CharLocale", loc)
    style.setPropertyValue.assert_any_call("CharLocale", loc)


def test_no_spellcheck_locale_uses_zxx():
    from plugin.notebook.writer_importer import _no_spellcheck_locale

    loc = _no_spellcheck_locale()
    assert loc.Language == "zxx"
    assert loc.Country == ""


def test_resolve_para_style_case_insensitive():
    doc = MagicMock()
    para_styles = MagicMock()
    para_styles.hasByName.side_effect = lambda n: n == "Text Body"
    para_styles.getElementNames.return_value = ["Text Body", "Heading 2"]
    families = MagicMock()
    families.getByName.return_value = para_styles
    doc.getStyleFamilies.return_value = families
    assert _resolve_para_style(doc, "text body") == "Text Body"


def test_append_body_paragraph_applies_resolved_style():
    doc, body_text, body_cursor = _writer_doc_mock()
    body_cursor.getString.return_value = ""
    para_styles = MagicMock()
    para_styles.hasByName.return_value = True
    families = MagicMock()
    families.getByName.return_value = para_styles
    doc.getStyleFamilies.return_value = families
    _append_body_paragraph(doc, "hello", "Text Body", lead_break=False)
    body_cursor.setPropertyValue.assert_called_with("ParaStyleName", "Text Body")


def test_append_body_text_block_single_paragraph():
    doc, body_text, body_cursor = _writer_doc_mock()
    body_cursor.getString.return_value = ""
    _append_body_text_block(doc, "line1\nline2\nline3", "Preformatted Text", lead_break=False)
    assert body_text.insertString.call_count == 1
    body_text.insertControlCharacter.assert_not_called()


@pytest.mark.skipif(
    not _writer_importer_import_logging_present(),
    reason="Release bundle strips log.info/log.debug; import logging verified in source tree only",
)
def test_import_ipynb_to_writer_logs(tmp_path, monkeypatch):
    ipynb = tmp_path / "tiny.ipynb"
    ipynb.write_text(
        '{"nbformat":4,"nbformat_minor":5,"metadata":{},"cells":[{"cell_type":"markdown","metadata":{},"source":"hi"}]}',
        encoding="utf-8",
    )

    doc, body_text, _ = _writer_doc_mock()

    class FakeSize:
        def __init__(self, w, h):
            self.Width = w
            self.Height = h

    doc.createInstance.side_effect = lambda service: MagicMock()
    monkeypatch.setattr("plugin.notebook.writer_importer.Size", FakeSize)

    log_messages: list[str] = []

    def _capture(msg, *args):
        log_messages.append(msg % args if args else msg)

    monkeypatch.setattr("plugin.notebook.writer_importer.log.info", _capture)
    monkeypatch.setattr("plugin.notebook.writer_importer.log.debug", _capture)

    stats = import_ipynb_to_writer(doc, str(ipynb))

    assert stats["cells"] == 1
    assert stats["markdown"] == 1
    body_text.insertTextContent.assert_not_called()
    log_text = "\n".join(log_messages)
    assert "notebook import start" in log_text
    assert "notebook import complete" in log_text
    assert "cell start index=0" in log_text


def test_import_ipynb_code_cells_use_insert_text_content(tmp_path, monkeypatch):
    ipynb = tmp_path / "mixed.ipynb"
    ipynb.write_text(
        '{"nbformat":4,"nbformat_minor":5,"metadata":{},"cells":['
        '{"cell_type":"markdown","metadata":{},"source":"# Title"},'
        '{"cell_type":"code","metadata":{},"source":"x=1","execution_count":1,"outputs":[]},'
        '{"cell_type":"markdown","metadata":{},"source":"more"},'
        '{"cell_type":"code","metadata":{},"source":"y=2","execution_count":2,"outputs":[]}'
        "]}",
        encoding="utf-8",
    )

    doc, body_text, body_cursor = _writer_doc_mock()
    para_styles = MagicMock()
    para_styles.hasByName.return_value = False
    para_styles.getElementNames.return_value = ["Text Body", _STYLE_NOTEBOOK_IN, "Heading 1", "Heading 2", "Heading 3"]
    families = MagicMock()
    families.getByName.return_value = para_styles
    doc.getStyleFamilies.return_value = families

    class FakeSize:
        def __init__(self, w, h):
            self.Width = w
            self.Height = h

    style_instance = MagicMock()

    def create_instance(service):
        if service == "com.sun.star.style.ParagraphStyle":
            return style_instance
        return MagicMock()

    doc.createInstance.side_effect = create_instance
    monkeypatch.setattr("plugin.notebook.writer_importer.Size", FakeSize)

    stats = import_ipynb_to_writer(doc, str(ipynb))

    assert stats["cells"] == 4
    assert stats["code"] == 2
    assert stats["shapes"] == 4
    assert body_text.insertTextContent.call_count == 4
    inserted = [call.args[1] for call in body_text.insertString.call_args_list]
    assert "In [1]:" in inserted
    assert "In [2]:" in inserted
    assert not any("Cell " in str(t) and ": Code" in str(t) for t in inserted)
    assert not any(str(t).strip() == "Output" for t in inserted)
    style_names = [call.args[1] for call in body_cursor.setPropertyValue.call_args_list if call.args[0] == "ParaStyleName"]
    assert _STYLE_NOTEBOOK_IN in style_names
    assert "Heading 1" in style_names


def test_import_ipynb_markdown_html_uses_insert_html(tmp_path, monkeypatch):
    ipynb = tmp_path / "html_md.ipynb"
    ipynb.write_text(
        '{"nbformat":4,"nbformat_minor":5,"metadata":{},"cells":['
        '{"cell_type":"markdown","metadata":{},"source":'
        '"<a href=\\"https://colab.research.google.com/\\">Open</a>"}'
        "]}",
        encoding="utf-8",
    )

    doc, body_text, _ = _writer_doc_mock()
    html_calls: list[str] = []

    class FakeSize:
        def __init__(self, w, h):
            self.Width = w
            self.Height = h

    doc.createInstance.side_effect = lambda service: MagicMock()
    monkeypatch.setattr("plugin.notebook.writer_importer.Size", FakeSize)

    def fake_insert_html(cursor, html, **kwargs):
        html_calls.append(html)
        return True

    monkeypatch.setattr("plugin.writer.html_import.insert_html_fragment_at_cursor", fake_insert_html)

    stats = import_ipynb_to_writer(doc, str(ipynb))

    assert stats["markdown"] == 1
    assert len(html_calls) == 1
    assert "<a href=" in html_calls[0]
    inserted_text = [args[0][1] for args in body_text.insertString.call_args_list]
    assert not any("<a href=" in t for t in inserted_text)


def test_import_ipynb_inserts_image_output(tmp_path, monkeypatch):
    # Minimal valid 1x1 PNG base64
    png_b64 = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
    ipynb = tmp_path / "img.ipynb"
    ipynb.write_text(
        '{"nbformat":4,"nbformat_minor":5,"metadata":{},"cells":['
        '{"cell_type":"code","metadata":{},"source":"plot()","outputs":['
        '{"output_type":"display_data","data":{"image/png":"' + png_b64 + '"}}'
        "]}]}",
        encoding="utf-8",
    )

    doc, body_text, _ = _writer_doc_mock()
    insert_calls: list[Any] = []

    class FakeSize:
        def __init__(self, w, h):
            self.Width = w
            self.Height = h

    doc.createInstance.side_effect = lambda service: MagicMock()
    body_text.insertTextContent.side_effect = lambda cursor, content, absorb: insert_calls.append(content)
    monkeypatch.setattr("plugin.notebook.writer_importer.Size", FakeSize)
    monkeypatch.setattr(
        "plugin.notebook.writer_importer.insert_image_at_locator",
        lambda ctx, model, path, **kw: MagicMock(),
    )

    ctx = MagicMock()
    stats = import_ipynb_to_writer(doc, str(ipynb), ctx=ctx)

    assert stats["code"] == 1
    assert stats["images"] == 1
    assert stats["shapes"] == 2
    assert len(insert_calls) == 2  # run button + code field; image via insert_image_at_locator


def test_import_code_cell_without_outputs_has_no_output_heading(tmp_path, monkeypatch):
    ipynb = tmp_path / "code_only.ipynb"
    ipynb.write_text(
        '{"nbformat":4,"nbformat_minor":5,"metadata":{},"cells":['
        '{"cell_type":"code","metadata":{},"source":"x=1","outputs":[]}'
        "]}",
        encoding="utf-8",
    )

    doc, body_text, _ = _writer_doc_mock(with_bookmarks=True)

    class FakeSize:
        def __init__(self, w, h):
            self.Width = w
            self.Height = h

    monkeypatch.setattr("plugin.notebook.writer_importer.Size", FakeSize)
    monkeypatch.setattr("plugin.notebook.cell_registry.insert_output_start_bookmark", lambda _d, _n: True)

    import_ipynb_to_writer(doc, str(ipynb))

    inserted = [call.args[1] for call in body_text.insertString.call_args_list]
    assert "Output" not in inserted
    assert "In [ ]:" in inserted
    assert not any("Cell " in str(t) for t in inserted)
    # Gutter/control split (first cell has no extra Output heading break).
    breaks = [call.args[1] for call in body_text.insertControlCharacter.call_args_list]
    assert breaks.count(_PARAGRAPH_BREAK) >= 1


def test_import_cells_breaks_before_in_flow_controls():
    from plugin.notebook import writer_importer as wi

    cell_src = inspect.getsource(wi._import_cells)
    assert "_append_paragraph_break_at_end" in cell_src


def test_append_paragraph_break_at_end_inserts_break():
    doc, body_text, body_cursor = _writer_doc_mock()
    _append_paragraph_break_at_end(doc)
    body_text.insertControlCharacter.assert_called_once_with(
        body_cursor, _PARAGRAPH_BREAK, False
    )


def test_import_ipynb_saves_registry_with_two_code_cells(tmp_path, monkeypatch):
    ipynb = tmp_path / "two_code.ipynb"
    ipynb.write_text(
        '{"nbformat":4,"nbformat_minor":5,"metadata":{},"cells":['
        '{"cell_type":"code","metadata":{},"source":"a=1","execution_count":1,"outputs":[]},'
        '{"cell_type":"code","metadata":{},"source":"b=2","execution_count":2,"outputs":[]}'
        "]}",
        encoding="utf-8",
    )

    doc, _, _ = _writer_doc_mock(with_bookmarks=True)

    class FakeSize:
        def __init__(self, w, h):
            self.Width = w
            self.Height = h

    monkeypatch.setattr("plugin.notebook.writer_importer.Size", FakeSize)
    monkeypatch.setattr("plugin.notebook.cell_registry.insert_output_start_bookmark", lambda _d, _n: True)

    saved: list = []

    def capture_save(d, state):
        saved.append(state)

    monkeypatch.setattr("plugin.notebook.writer_importer.save_registry", capture_save)
    monkeypatch.setattr("plugin.notebook.writer_importer.save_notebook_source_path", MagicMock())

    import_ipynb_to_writer(doc, str(ipynb))

    assert len(saved) == 1
    state = saved[0]
    assert state.source_path == str(ipynb)
    assert len(state.code_cells) == 2
    assert state.code_cells[0].code_field_name == "nb_cell_0_code"
    assert state.code_cells[1].code_field_name == "nb_cell_1_code"
    assert state.code_cells[0].execution_count == 1
    assert state.code_cells[1].execution_count == 2
    assert state.code_cells[0].output_start_bookmark.startswith("nb_out_")


def test_iter_markdown_blocks_atx_and_paragraphs():
    blocks = _iter_markdown_blocks(
        "# A Small Introduction to NumPy\n\n"
        "This is a compact version.\n\n"
        "## 1. Creating Arrays\n\n"
        "The primary data structure is the `ndarray`.\n"
    )
    assert blocks[0] == ("h1", "A Small Introduction to NumPy")
    assert blocks[1] == ("p", "This is a compact version.")
    assert blocks[2] == ("h2", "1. Creating Arrays")
    assert blocks[3] == ("p", "The primary data structure is the `ndarray`.")
    assert not any(kind.startswith("#") or text.lstrip().startswith("#") for kind, text in blocks)


def test_iter_markdown_blocks_deeper_atx_maps_to_h2():
    blocks = _iter_markdown_blocks("### Deep\n\nbody")
    assert blocks[0] == ("h2", "Deep")


def test_inline_backticks_to_html_wraps_code():
    html = _inline_backticks_to_html("The primary data structure in NumPy is the `ndarray`.")
    assert "<code>ndarray</code>" in html
    assert "`ndarray`" not in html
    assert "NumPy" in html


class FakeSize:
    def __init__(self, w, h):
        self.Width = w
        self.Height = h


def test_import_small_numpy_notebook_fixture(monkeypatch):
    ipynb = Path(__file__).resolve().parents[1] / "fixtures" / "introduction-to-numpy-small.ipynb"
    assert ipynb.is_file()

    doc, body_text, body_cursor = _writer_doc_mock(with_bookmarks=True)
    para_styles = MagicMock()
    para_styles.hasByName.return_value = False
    para_styles.getElementNames.return_value = [
        "Text Body",
        "Heading 1",
        "Heading 2",
        "Heading 3",
        "Heading 4",
        "Preformatted Text",
        _STYLE_NOTEBOOK_IN,
    ]
    families = MagicMock()
    families.getByName.return_value = para_styles
    doc.getStyleFamilies.return_value = families

    text_fields: list[Any] = []
    style_instance = MagicMock()

    def create_instance(service):
        if service == "com.sun.star.style.ParagraphStyle":
            return style_instance
        model = MagicMock()
        if service == "com.sun.star.form.component.TextField":
            text_fields.append(model)
        return model

    doc.createInstance.side_effect = create_instance
    monkeypatch.setattr("plugin.notebook.writer_importer.Size", FakeSize)
    monkeypatch.setattr("plugin.notebook.cell_registry.insert_output_start_bookmark", lambda _d, _n: True)

    html_calls: list[str] = []

    def fake_insert_html(cursor, html, **kwargs):
        html_calls.append(html)
        return True

    monkeypatch.setattr("plugin.writer.html_import.insert_html_fragment_at_cursor", fake_insert_html)

    stats = import_ipynb_to_writer(doc, str(ipynb))

    assert stats["cells"] == 6
    assert stats["markdown"] == 3
    assert stats["code"] == 3
    assert stats["shapes"] == 6
    assert [f.Name for f in text_fields] == ["nb_cell_1_code", "nb_cell_3_code", "nb_cell_5_code"]

    inserted = [call.args[1] for call in body_text.insertString.call_args_list]
    assert "A Small Introduction to NumPy" in inserted
    assert "1. Creating Arrays" in inserted
    assert "2. Array Operations" in inserted
    assert not any(str(t).lstrip().startswith("#") for t in inserted)

    style_names = [
        call.args[1] for call in body_cursor.setPropertyValue.call_args_list if call.args[0] == "ParaStyleName"
    ]
    assert _STYLE_MD_H1 in style_names
    assert _STYLE_MD_H2 in style_names

    assert any("<code>ndarray</code>" in h for h in html_calls)
    assert not any("# A Small" in h or "## 1." in h for h in html_calls)
    assert not any("Cell " in str(t) and "Markdown" in str(t) for t in inserted)
    assert not any(str(t).strip() == "Output" for t in inserted)
    assert "In [1]:" in inserted
    assert "In [2]:" in inserted
    assert "In [3]:" in inserted


def _list_item_texts(payload):
    texts = []
    for item in payload:
        if isinstance(item, tuple) and len(item) >= 4:
            texts.append(item[3])
        else:
            texts.append(str(item))
    return texts


def test_iter_markdown_blocks_lists():
    from plugin.notebook.writer_importer import _iter_markdown_blocks

    blocks = _iter_markdown_blocks(
        "Key terms:\n"
        "* **Array** - A list of numbers.\n"
        "* **Scalar** - A single number.\n"
        "\n"
        "* `np.array()`\n"
        "* `np.ones()`\n"
    )
    assert blocks[0] == ("p", "Key terms:")
    assert blocks[1][0] == "ul"
    assert _list_item_texts(blocks[1][1])[0].startswith("**Array**")
    assert blocks[2][0] == "ul"
    assert _list_item_texts(blocks[2][1]) == ["`np.array()`", "`np.ones()`"]


def test_iter_markdown_blocks_nested_lists_ol_start_and_blockquote():
    from plugin.notebook.writer_importer import _iter_markdown_blocks, _list_block_to_html

    source = (
        "2. **Search for it** - try these:\n"
        "    * [NumPy documentation](https://numpy.org/doc/stable/index.html) - official\n"
        "    * [Stack Overflow](https://stackoverflow.com/) - questions\n"
        "\n"
        "3. **Ask for help** - after searching.\n"
        "\n"
        "> **Note:** Important to remember `ndarray`\n"
        "\n"
        '> "how to find unique elements in a numpy array"\n'
    )
    blocks = _iter_markdown_blocks(source)
    kinds = [k for k, _p in blocks]
    assert kinds[0] == "ol"
    nested_html = _list_block_to_html(blocks[0][1])
    assert "<ul>" in nested_html
    assert "<li>" in nested_html
    assert "NumPy documentation" in nested_html
    assert "https://numpy.org/doc/stable/index.html" in nested_html
    assert "*" not in nested_html.replace("**", "")
    assert kinds[1] == "ol"
    resume_html = _list_block_to_html(blocks[1][1])
    assert 'start="3"' in resume_html
    assert "Ask for help" in resume_html
    assert kinds[2] == "blockquote"
    assert blocks[2][1].startswith("**Note:**")
    assert not blocks[2][1].lstrip().startswith(">")
    assert kinds[3] == "blockquote"
    assert "how to find unique elements" in blocks[3][1]
    assert ">" not in blocks[3][1]


def test_import_nested_lists_and_blockquotes_fixture(monkeypatch):
    ipynb = Path(__file__).resolve().parents[1] / "fixtures" / "markdown-lists-quotes.ipynb"
    assert ipynb.is_file()
    doc, body_text, _ = _writer_doc_mock()
    html_calls: list[str] = []

    def fake_insert_html(cursor, html, **kwargs):
        html_calls.append(html)
        return True

    monkeypatch.setattr("plugin.writer.html_import.insert_html_fragment_at_cursor", fake_insert_html)
    stats = import_ipynb_to_writer(doc, str(ipynb))
    assert stats["markdown"] == 3
    joined = "\n".join(html_calls)
    assert "<ul>" in joined
    assert "<li>" in joined
    assert 'start="3"' in joined
    assert "Ask for help" in joined
    assert "<blockquote>" in joined
    assert "<strong>Note:</strong>" in joined or "**Note:**" not in joined
    assert "ndarray" in joined
    assert "how to find unique elements" in joined
    assert "https://numpy.org/doc/stable/index.html" in joined
    assert not re.search(r">\s*\*\s", joined)
    inserted = [str(call.args[1]) for call in body_text.insertString.call_args_list]
    assert not any(t.lstrip().startswith("* ") for t in inserted)
    assert not any(t.lstrip().startswith(">") for t in inserted)
    for html in html_calls:
        assert "> **Note" not in html
        assert ">*" not in html.replace("</", "")


def test_inline_markdown_bold_italic_code():
    from plugin.notebook.writer_importer import _inline_markdown_to_html, _paragraph_needs_html

    html = _inline_markdown_to_html("**Array** - A list of `ndarray`s and *italic*.")
    assert "<strong>Array</strong>" in html
    assert "<code>ndarray</code>" in html
    assert "<em>italic</em>" in html
    assert "**" not in html
    assert _paragraph_needs_html("use **bold** here") is True
    assert _paragraph_needs_html("plain text") is False
    link_html = _inline_markdown_to_html("See the [NumPy docs](https://numpy.org/).")
    assert '<a href="https://numpy.org/">NumPy docs</a>' in link_html
    assert "[NumPy docs]" not in link_html
    assert _paragraph_needs_html("See the [NumPy docs](https://numpy.org/).") is True
    code_link = _inline_markdown_to_html("use [`np.sort()`](https://numpy.org/sort).")
    assert "<code>np.sort()</code>" in code_link
    assert 'href="https://numpy.org/sort"' in code_link


def test_import_markdown_lists_and_bold_use_html(tmp_path, monkeypatch):
    ipynb = tmp_path / "lists.ipynb"
    ipynb.write_text(
        '{"nbformat":4,"nbformat_minor":5,"metadata":{},"cells":['
        '{"cell_type":"markdown","metadata":{},"source":'
        '"Key terms:\\n* **Array** - A list.\\n* `np.array()`"}'
        "]}",
        encoding="utf-8",
    )
    doc, body_text, _ = _writer_doc_mock()
    html_calls: list[str] = []

    def fake_insert_html(cursor, html, **kwargs):
        html_calls.append(html)
        return True

    monkeypatch.setattr("plugin.writer.html_import.insert_html_fragment_at_cursor", fake_insert_html)
    stats = import_ipynb_to_writer(doc, str(ipynb))
    assert stats["markdown"] == 1
    joined = "\n".join(html_calls)
    assert "<ul>" in joined and "<li>" in joined
    assert "<strong>Array</strong>" in joined
    assert "<code>np.array()</code>" in joined
    inserted = [call.args[1] for call in body_text.insertString.call_args_list]
    assert not any("Cell " in str(t) for t in inserted)


def test_inline_markdown_nested_code_in_bold():
    from plugin.notebook.writer_importer import _inline_markdown_to_html

    html = _inline_markdown_to_html("**`code` in bold**")
    assert html == "<strong><code>code</code> in bold</strong>"

    html_italic = _inline_markdown_to_html("*`code` in italic*")
    assert html_italic == "<em><code>code</code> in italic</em>"


def test_height_for_text_accounts_for_long_wrapped_lines():
    from plugin.notebook.writer_importer import _height_for_text

    short_lines = "a = 1\nb = 2"
    long_wrapped_line = "a = [" + "1, " * 100 + "]"
    h_short = _height_for_text(short_lines)
    h_long = _height_for_text(long_wrapped_line)
    assert h_long > h_short


_TINY_PNG = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01"
    b"\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f\x15\xc4\x89"
)


def test_html_img_and_a_to_markdown_extracts_tags():
    from plugin.notebook.writer_importer import _html_img_and_a_to_markdown, _iter_markdown_blocks

    src = (
        '<a target="_blank" href="https://colab.research.google.com/">\n'
        '  <img src="https://colab.research.google.com/assets/colab-badge.svg" '
        'alt="Open In Colab"/>\n'
        "</a>\n"
        "\n"
        "[View source](https://github.com/example/nb)\n"
        "\n"
        "## What is NumPy?\n"
        "\n"
        '<img src="../images/numpy-anatomy-of-an-array-updated.png" '
        'alt="anatomy of a numpy array"/>\n'
        "\n"
        "### Anatomy of an array\n"
        "\n"
        "* **Array** - A list of numbers.\n"
    )
    converted = _html_img_and_a_to_markdown(src)
    assert "<img" not in converted.lower()
    assert "<a " not in converted.lower()
    assert "![Open In Colab](https://colab.research.google.com/assets/colab-badge.svg)" in converted
    assert "![anatomy of a numpy array](../images/numpy-anatomy-of-an-array-updated.png)" in converted
    assert "## What is NumPy?" in converted
    blocks = _iter_markdown_blocks(converted)
    kinds = [k for k, _p in blocks]
    assert "h2" in kinds
    assert "img" in kinds
    assert "ul" in kinds
    assert any(k == "h2" and p == "Anatomy of an array" for k, p in blocks)
    assert any(k == "img" and p[1].endswith("numpy-anatomy-of-an-array-updated.png") for k, p in blocks)


def test_resolve_markdown_image_path_relative_to_notebook_dir(tmp_path):
    from plugin.notebook.writer_importer import _resolve_markdown_image_path

    nb_dir = tmp_path / "fixtures"
    img_dir = tmp_path / "images"
    nb_dir.mkdir()
    img_dir.mkdir()
    img = img_dir / "foo.png"
    img.write_bytes(_TINY_PNG)
    resolved = _resolve_markdown_image_path("../images/foo.png", str(nb_dir))
    assert resolved is not None
    assert Path(resolved).resolve() == img.resolve()
    assert _resolve_markdown_image_path("../images/missing.png", str(nb_dir)) is None


def test_resolve_bourke_images_from_fixture_notebook_dir():
    from plugin.notebook.writer_importer import _resolve_markdown_image_path

    fixtures = Path(__file__).resolve().parents[1] / "fixtures"
    for name in (
        "numpy-6-step-ml-framework-tools-numpy-highlight.png",
        "numpy-anatomy-of-an-array-updated.png",
        "numpy-panda.jpeg",
        "numpy-car-photo.png",
        "numpy-dog-photo.png",
    ):
        resolved = _resolve_markdown_image_path(f"../images/{name}", str(fixtures))
        assert resolved is not None, name
        assert Path(resolved).is_file(), name


def test_svg_pixel_size_reads_viewbox():
    from plugin.notebook.writer_importer import _svg_pixel_size

    svg = b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 117 20"></svg>'
    assert _svg_pixel_size(svg) == (117, 20)
    sized = b'<svg width="80" height="20"></svg>'
    assert _svg_pixel_size(sized) == (80, 20)


def test_import_mixed_html_img_cell_renders_markdown_and_image(tmp_path, monkeypatch):
    """HTML <img> + markdown in one cell: heading/list/link, relative image embed."""
    from plugin.notebook.writer_importer import import_ipynb_to_writer

    nb_dir = tmp_path / "fixtures"
    img_dir = tmp_path / "images"
    nb_dir.mkdir()
    img_dir.mkdir()
    (img_dir / "tiny.png").write_bytes(_TINY_PNG)
    ipynb = nb_dir / "mixed.ipynb"
    ipynb.write_text(
        '{"nbformat":4,"nbformat_minor":5,"metadata":{},"cells":['
        '{"cell_type":"markdown","metadata":{},"source":'
        '"## What is NumPy?\\n\\n'
        '[NumPy](https://numpy.org/doc/stable/index.html) stands for numerical Python.\\n\\n'
        '<img src=\\"../images/tiny.png\\" alt=\\"tiny test image\\"/>\\n\\n'
        '### Anatomy of an array\\n\\n'
        '* **Array** - A list of numbers."}'
        "]}",
        encoding="utf-8",
    )

    doc, body_text, body_cursor = _writer_doc_mock()
    para_styles = MagicMock()
    para_styles.hasByName.return_value = True
    families = MagicMock()
    families.getByName.return_value = para_styles
    doc.getStyleFamilies.return_value = families
    html_calls: list[str] = []
    embed_calls: list[str] = []

    def fake_insert_html(cursor, html, **kwargs):
        html_calls.append(html)
        return True

    def fake_embed(d, src, notebook_dir, *, ctx=None):
        embed_calls.append(src)
        return True

    monkeypatch.setattr("plugin.writer.html_import.insert_html_fragment_at_cursor", fake_insert_html)
    monkeypatch.setattr("plugin.notebook.writer_importer._embed_markdown_image", fake_embed)

    stats = import_ipynb_to_writer(doc, str(ipynb))
    assert stats["markdown"] == 1
    inserted = [call.args[1] for call in body_text.insertString.call_args_list]
    assert "What is NumPy?" in inserted
    assert "Anatomy of an array" in inserted
    assert not any(str(t).lstrip().startswith("#") for t in inserted)
    assert not any("[NumPy]" in str(t) for t in inserted)
    assert not any("**Array**" in str(t) for t in inserted)
    joined_html = "\n".join(html_calls)
    assert 'href="https://numpy.org/doc/stable/index.html"' in joined_html
    assert "<strong>Array</strong>" in joined_html
    assert embed_calls == ["../images/tiny.png"]
    style_names = [
        call.args[1] for call in body_cursor.setPropertyValue.call_args_list if call.args[0] == "ParaStyleName"
    ]
    assert _STYLE_MD_H2 in style_names


def test_import_html_img_fixture_notebook(monkeypatch):
    ipynb = Path(__file__).resolve().parents[1] / "fixtures" / "html-img-and-md-link.ipynb"
    assert ipynb.is_file()
    img = Path(__file__).resolve().parents[1] / "images" / "numpy-anatomy-of-an-array-updated.png"
    assert img.is_file()

    doc, body_text, body_cursor = _writer_doc_mock()
    para_styles = MagicMock()
    para_styles.hasByName.return_value = True
    families = MagicMock()
    families.getByName.return_value = para_styles
    doc.getStyleFamilies.return_value = families
    html_calls: list[str] = []
    embed_srcs: list[str] = []
    locator_calls: list[str] = []

    def fake_insert_html(cursor, html, **kwargs):
        html_calls.append(html)
        return True

    def fake_locator(ctx, model, path, **kw):
        locator_calls.append(str(path))
        return MagicMock()

    import plugin.notebook.writer_importer as wi

    real_embed = wi._embed_markdown_image

    def tracking_embed(d, src, notebook_dir, *, ctx=None):
        embed_srcs.append(src)
        return real_embed(d, src, notebook_dir, ctx=ctx)

    monkeypatch.setattr("plugin.writer.html_import.insert_html_fragment_at_cursor", fake_insert_html)
    monkeypatch.setattr("plugin.notebook.writer_importer.insert_image_at_locator", fake_locator)
    monkeypatch.setattr("plugin.notebook.writer_importer._embed_markdown_image", tracking_embed)

    ctx = MagicMock()
    stats = import_ipynb_to_writer(doc, str(ipynb), ctx=ctx)
    assert stats["markdown"] == 1
    inserted = [call.args[1] for call in body_text.insertString.call_args_list]
    assert "What is NumPy?" in inserted
    assert "Anatomy of an array" in inserted
    assert not any("[NumPy]" in str(t) for t in inserted)
    joined_html = "\n".join(html_calls)
    assert "numpy.org" in joined_html
    assert "<strong>Array</strong>" in joined_html
    assert embed_srcs == ["../images/numpy-anatomy-of-an-array-updated.png"]
    assert locator_calls, "relative HTML <img> was not embedded"
