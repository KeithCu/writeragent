# WriterAgent - tests for notebook Writer import helpers

from __future__ import annotations

import inspect
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from plugin.notebook.writer_importer import (
    _ImportStackCursor,
    _MAX_IMAGE_DECODE_BYTES,
    _MAX_IMPORT_TEXT_CHARS,
    _STYLE_NOTEBOOK_IN,
    _append_body_text_block,
    _append_body_paragraph,
    _cell_heading,
    _coerce_notebook_text,
    _create_import_para_style,
    _decode_notebook_image,
    _ensure_notebook_import_styles,
    _format_in_prompt,
    _looks_like_html,
    _notebook_image_payload,
    _png_pixel_size,
    _prepare_display_text,
    _resolve_para_style,
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

    assert format_output_text(Out()) == "[stdout]\nhello\n"


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
    assert _looks_like_html('<a href="https://example.com">x</a>') is True
    assert _looks_like_html("## Plain markdown\n\nno tags") is False


def test_wrap_html_fragment_adds_body():
    wrapped = _wrap_html_fragment("<p>Hi</p>")
    assert "<html>" in wrapped and "<body>" in wrapped and "<p>Hi</p>" in wrapped


def test_format_in_prompt_executed_and_unexecuted():
    assert _format_in_prompt(1) == "[In [1]]"
    assert _format_in_prompt(None) == "[In [ ]]"


def test_cell_heading_includes_in_prompt():
    assert _cell_heading(1, "code") == "[In [ ]]\tCell 2: Code"
    assert "[In" not in _cell_heading(0, "markdown")


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
    para_styles.getElementNames.return_value = ["Text Body", _STYLE_NOTEBOOK_IN, "Heading 2", "Heading 3"]
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
    assert "[In [1]]\tCell 2: Code" in inserted
    assert "[In [2]]\tCell 4: Code" in inserted
    style_names = [call.args[1] for call in body_cursor.setPropertyValue.call_args_list if call.args[0] == "ParaStyleName"]
    assert _STYLE_NOTEBOOK_IN in style_names
    assert "Heading 3" in style_names


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

    def fake_insert_html(cursor, html):
        html_calls.append(html)
        return True

    monkeypatch.setattr("plugin.writer.ops.insert_html_at_cursor", fake_insert_html)

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


def test_import_code_cell_without_outputs_still_adds_output_heading(tmp_path, monkeypatch):
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
    assert "Output" in inserted


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
