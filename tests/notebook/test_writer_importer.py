# WriterAgent - tests for notebook Writer import helpers

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

from plugin.notebook.writer_importer import (
    _ImportStackCursor,
    _MAX_IMAGE_DECODE_BYTES,
    _MAX_IMPORT_TEXT_CHARS,
    _append_body_text_block,
    _coerce_notebook_text,
    _decode_notebook_image,
    _notebook_image_payload,
    _png_pixel_size,
    _prepare_display_text,
    format_all_outputs,
    format_output_text,
    import_ipynb_to_writer,
)


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


def _writer_doc_mock():
    body_cursor = MagicMock()
    body_text = MagicMock()
    body_text.createTextCursor.return_value = body_cursor
    doc = MagicMock()
    doc.getText.return_value = body_text
    return doc, body_text, body_cursor


def test_append_body_text_block_single_paragraph():
    doc, body_text, body_cursor = _writer_doc_mock()
    body_cursor.getString.return_value = ""
    _append_body_text_block(doc, "line1\nline2\nline3", "Preformatted Text", lead_break=False)
    assert body_text.insertString.call_count == 1
    body_text.insertControlCharacter.assert_not_called()


def test_import_ipynb_to_writer_logs(caplog, tmp_path, monkeypatch):
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

    with caplog.at_level("DEBUG", logger="writeragent.notebook"):
        stats = import_ipynb_to_writer(doc, str(ipynb))

    assert stats["cells"] == 1
    assert stats["markdown"] == 1
    body_text.insertTextContent.assert_not_called()
    assert "notebook import start" in caplog.text
    assert "notebook import complete" in caplog.text
    assert "cell start index=0" in caplog.text


def test_import_ipynb_code_cells_use_insert_text_content(tmp_path, monkeypatch):
    ipynb = tmp_path / "mixed.ipynb"
    ipynb.write_text(
        '{"nbformat":4,"nbformat_minor":5,"metadata":{},"cells":['
        '{"cell_type":"markdown","metadata":{},"source":"# Title"},'
        '{"cell_type":"code","metadata":{},"source":"x=1","outputs":[]},'
        '{"cell_type":"markdown","metadata":{},"source":"more"},'
        '{"cell_type":"code","metadata":{},"source":"y=2","outputs":[]}'
        "]}",
        encoding="utf-8",
    )

    doc, body_text, _ = _writer_doc_mock()

    class FakeSize:
        def __init__(self, w, h):
            self.Width = w
            self.Height = h

    doc.createInstance.side_effect = lambda service: MagicMock()
    monkeypatch.setattr("plugin.notebook.writer_importer.Size", FakeSize)

    stats = import_ipynb_to_writer(doc, str(ipynb))

    assert stats["cells"] == 4
    assert stats["code"] == 2
    assert stats["shapes"] == 2
    # Two code fields + no image outputs in this fixture
    assert body_text.insertTextContent.call_count == 2


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
        "plugin.notebook.writer_importer._create_embedded_graphic",
        lambda model, inside, url: MagicMock(),
    )
    monkeypatch.setattr("plugin.notebook.writer_importer._apply_graphic_properties", lambda *a, **k: None)
    monkeypatch.setattr("plugin.notebook.writer_importer._file_url_for_path", lambda p: "file:///tmp/x.png")

    stats = import_ipynb_to_writer(doc, str(ipynb))

    assert stats["code"] == 1
    assert stats["images"] == 1
    assert stats["shapes"] == 1
    assert len(insert_calls) == 2  # code field + image
