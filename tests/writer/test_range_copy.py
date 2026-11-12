"""Unit tests for plugin/writer/range_copy.py."""
from unittest.mock import MagicMock, patch

from plugin.writer import range_copy as rc


class _Enum:
    def __init__(self, items: list) -> None:
        self._items = list(items)

    def hasMoreElements(self) -> bool:
        return bool(self._items)

    def nextElement(self):
        return self._items.pop(0)


def test_copy_cell_xtext_copies_direct_char_formatting() -> None:
    portion = MagicMock()
    portion.getPropertyValue.return_value = "Text"
    portion.getString.return_value = "Bold"

    para = MagicMock()
    para.supportsService.return_value = False
    para.createEnumeration.return_value = _Enum([portion])

    src_cell = MagicMock()
    src_cell.createEnumeration.return_value = _Enum([para])

    dest_doc = MagicMock()
    dest_cell = MagicMock()

    with patch.object(rc, "_copy_properties") as copy_props, \
            patch.object(rc, "_copy_math_or_text") as math_or_text:
        rc._copy_cell_xtext(src_cell, dest_doc, dest_cell)

    assert [call.args[2] for call in copy_props.call_args_list] == [rc._COPIED_CHAR_PROPERTIES]
    math_or_text.assert_not_called()


def test_apply_temp_para_style_returns_bool_status() -> None:
    temp_doc = MagicMock()
    temp_cursor = MagicMock()
    families = temp_doc.getStyleFamilies.return_value
    para_styles = families.getByName.return_value

    # Empty style returns False
    assert rc.apply_temp_para_style(temp_doc, temp_cursor, "") is False
    temp_cursor.setPropertyValue.assert_not_called()

    # Style missing in temp doc returns False
    para_styles.hasByName.return_value = False
    assert rc.apply_temp_para_style(temp_doc, temp_cursor, "CustomStyle") is False
    temp_cursor.setPropertyValue.assert_not_called()

    # Style present in temp doc applies and returns True
    para_styles.hasByName.return_value = True
    assert rc.apply_temp_para_style(temp_doc, temp_cursor, "Header") is True
    temp_cursor.setPropertyValue.assert_called_once_with("ParaStyleName", "Header")

    # Exception during setPropertyValue returns False
    temp_cursor.setPropertyValue.side_effect = RuntimeError("fail")
    assert rc.apply_temp_para_style(temp_doc, temp_cursor, "Header") is False


def test_every_para_writer_property_is_copied_by_range_reads(monkeypatch) -> None:
    """Every UNO property written from data-lo-para must be in _COPIED_PARA_PROPERTIES.

    Range and header reads copy paragraphs into a scratch document first, so any
    property missing from this list is dropped on range and header content reads.
    """
    import sys
    from plugin.writer import html_import_styles

    class _FakeStruct:
        Mode = 0
        Height = 0

    monkeypatch.setitem(sys.modules["uno"].__dict__, "createUnoStruct", lambda _name: _FakeStruct())
    writers = html_import_styles._para_writers()
    assert writers

    samples = {
        "text-align": "center",
        "line-height": "150%",
        "background-color": "#ff0000",
    }
    written_props: set[str] = set()
    for name, converter in writers.items():
        sample = samples.get(name, "1cm")
        result = converter(sample)
        assert result is not None, f"converter failed for {name}"
        written_props.add(result[0])

    assert written_props.issubset(set(rc._COPIED_PARA_PROPERTIES))


def test_begin_next_paragraph_steps_into_the_new_paragraph_and_survives_a_refused_break() -> None:
    dest_text = MagicMock()
    cursor = MagicMock()
    rc._begin_next_paragraph(dest_text, cursor)
    dest_text.insertControlCharacter.assert_called_once_with(cursor, rc.PARAGRAPH_BREAK, False)
    cursor.gotoNextParagraph.assert_called_once_with(False)

    dest_text.reset_mock()
    cursor.reset_mock()
    dest_text.insertControlCharacter.side_effect = RuntimeError("break refused")
    rc._begin_next_paragraph(dest_text, cursor)
    cursor.gotoNextParagraph.assert_not_called()


def test_copy_xtext_by_portions_moves_view_cursor_once_per_table() -> None:
    src_text = MagicMock()
    table = MagicMock()
    table.supportsService.side_effect = lambda s: s == "com.sun.star.text.TextTable"
    src_text.createEnumeration.return_value = _Enum([table])

    dest_doc = MagicMock()
    dest_doc.getText.return_value = MagicMock()

    with patch.object(rc, "_writer_table_copy_layout", return_value=(1, 1, [])), \
            patch.object(rc, "_goto_doc_end") as goto_doc_end:
        rc._copy_xtext_by_portions(src_text, dest_doc)

    assert goto_doc_end.call_count == 1

