"""Unit tests for plugin/writer/range_copy.py."""
from unittest.mock import MagicMock, call, patch

import pytest

from plugin.doc.text_helpers import TrackedDeletions
from plugin.framework.errors import DocumentDisposedError
from plugin.writer import range_copy as rc


@pytest.fixture(autouse=True)
def _clear_scratch_style_holds():
    rc._scratch_style_holds.clear()
    yield
    rc._scratch_style_holds.clear()


class _Enum:
    def __init__(self, items: list) -> None:
        self._items = list(items)

    def hasMoreElements(self) -> bool:
        return bool(self._items)

    def nextElement(self):
        return self._items.pop(0)


def test_copy_cell_xtext_copies_direct_char_formatting() -> None:
    portion = MagicMock()
    portion.getPropertyValue.side_effect = lambda name: (
        "Text" if name == "TextPortionType" else 150.0 if name == "CharWeight" else None
    )
    portion.getPropertyValues.side_effect = lambda names: tuple(
        150.0 if n == "CharWeight" else None for n in names
    )
    portion.getString.return_value = "Bold"

    para = MagicMock()
    para.supportsService.return_value = False
    para.createEnumeration.return_value = _Enum([portion])

    src_cell = MagicMock()
    src_cell.createEnumeration.return_value = _Enum([para])

    dest_doc = MagicMock()
    dest_cell = MagicMock()
    dest_cursor = MagicMock()
    run = MagicMock()
    run.getPropertyValues.side_effect = lambda names: tuple(
        100.0 if n == "CharWeight" else None for n in names
    )
    dest_cell.createTextCursor.return_value = dest_cursor
    dest_cell.createTextCursorByRange.return_value = run

    rc._copy_cell_xtext(MagicMock(), src_cell, dest_doc, dest_cell, TrackedDeletions(MagicMock()))

    dest_cell.insertString.assert_any_call(dest_cursor, "Bold", False)
    run.setPropertyValue.assert_any_call("CharWeight", 150.0)


def test_apply_temp_para_style_returns_bool_status() -> None:
    temp_doc = MagicMock()
    temp_cursor = MagicMock()
    families = temp_doc.getStyleFamilies.return_value
    para_styles = families.getByName.return_value

    # Empty style returns False
    assert rc.apply_temp_para_style(temp_doc, temp_cursor, "") is False
    temp_cursor.setPropertyValue.assert_not_called()

    # Style missing in temp doc with no src_doc returns False
    para_styles.hasByName.return_value = False
    assert rc.apply_temp_para_style(temp_doc, temp_cursor, "CustomStyle", src_doc=None) is False
    temp_cursor.setPropertyValue.assert_not_called()

    # src_doc present, source family has no such name -> False
    src_doc = MagicMock()
    src_styles = src_doc.getStyleFamilies.return_value.getByName.return_value
    src_styles.hasByName.return_value = False
    assert rc.apply_temp_para_style(temp_doc, temp_cursor, "CustomStyle", src_doc=src_doc) is False
    temp_cursor.setPropertyValue.assert_not_called()

    # src_doc present, but createInstance returns None -> False
    src_styles.hasByName.return_value = True
    temp_doc.createInstance.return_value = None
    assert rc.apply_temp_para_style(temp_doc, temp_cursor, "CustomStyle", src_doc=src_doc) is False
    temp_cursor.setPropertyValue.assert_not_called()

    # Style present in temp doc applies and returns True
    para_styles.hasByName.return_value = True
    assert rc.apply_temp_para_style(temp_doc, temp_cursor, "Header") is True
    temp_cursor.setPropertyValue.assert_called_once_with("ParaStyleName", "Header")

    # Exception during setPropertyValue returns False
    temp_cursor.setPropertyValue.side_effect = RuntimeError("fail")
    assert rc.apply_temp_para_style(temp_doc, temp_cursor, "Header") is False


def test_apply_temp_para_style_clones_missing_style_parents_first() -> None:
    from typing import Any
    from plugin.writer.styles import _STYLE_KEEPALIVE

    temp_doc = MagicMock()
    temp_cursor = MagicMock()
    dest_family = temp_doc.getStyleFamilies.return_value.getByName.return_value

    inserted_styles: dict[str, Any] = {}

    def _dest_has_by_name(name: str) -> bool:
        return name == "Standard" or name in inserted_styles

    dest_family.hasByName.side_effect = _dest_has_by_name

    def _dest_insert_by_name(name: str, clone: Any) -> None:
        inserted_styles[name] = clone

    dest_family.insertByName.side_effect = _dest_insert_by_name

    parent_clone = MagicMock()
    child_clone = MagicMock()
    temp_doc.createInstance.side_effect = [parent_clone, child_clone]

    src_doc = MagicMock()
    src_family = src_doc.getStyleFamilies.return_value.getByName.return_value
    src_family.hasByName.side_effect = lambda name: name in ("LetterChild", "LetterParent", "Standard")

    source_child = MagicMock()
    source_child.getParentStyle.return_value = "LetterParent"
    source_child.getPropertyValue.return_value = ""

    source_parent = MagicMock()
    source_parent.getParentStyle.return_value = "Standard"
    source_parent.getPropertyValue.return_value = ""

    def _src_get_by_name(name: str) -> Any:
        if name == "LetterChild":
            return source_child
        if name == "LetterParent":
            return source_parent
        return MagicMock()

    src_family.getByName.side_effect = _src_get_by_name

    initial_keepalive_len = len(_STYLE_KEEPALIVE)

    with patch("plugin.writer.styles.copy_direct_style_properties", return_value=([], [])):
        res = rc.apply_temp_para_style(temp_doc, temp_cursor, "LetterChild", src_doc=src_doc)

    assert res is True
    assert temp_doc.createInstance.call_count == 2
    assert dest_family.insertByName.call_args_list == [
        call("LetterParent", parent_clone),
        call("LetterChild", child_clone),
    ]
    parent_clone.setParentStyle.assert_called_once_with("Standard")
    child_clone.setParentStyle.assert_called_once_with("LetterParent")
    temp_cursor.setPropertyValue.assert_called_once_with("ParaStyleName", "LetterChild")
    assert len(_STYLE_KEEPALIVE) == initial_keepalive_len
    rc.release_scratch_style_holds(temp_doc)


def test_apply_temp_para_style_missing_follow_style_returns_true() -> None:
    from typing import Any

    # Case 1: Source FollowStyle is "AbsentFollow" (absent in dest)
    temp_doc = MagicMock()
    temp_cursor = MagicMock()
    dest_family = temp_doc.getStyleFamilies.return_value.getByName.return_value
    inserted_styles: dict[str, Any] = {}
    dest_family.hasByName.side_effect = lambda name: name in inserted_styles
    dest_family.insertByName.side_effect = lambda name, clone: inserted_styles.setdefault(name, clone)

    clone1 = MagicMock()
    temp_doc.createInstance.return_value = clone1

    src_doc = MagicMock()
    src_family = src_doc.getStyleFamilies.return_value.getByName.return_value
    src_family.hasByName.return_value = True
    source_style = MagicMock()
    source_style.getParentStyle.return_value = ""

    def _get_prop1(name: str) -> Any:
        if name == "FollowStyle":
            return "AbsentFollow"
        return None

    source_style.getPropertyValue.side_effect = _get_prop1
    src_family.getByName.return_value = source_style

    with patch("plugin.writer.styles.copy_direct_style_properties", return_value=([], [])):
        res = rc.apply_temp_para_style(temp_doc, temp_cursor, "MyStyle", src_doc=src_doc)

    assert res is True
    temp_cursor.setPropertyValue.assert_called_with("ParaStyleName", "MyStyle")
    follow_calls = [c for c in clone1.setPropertyValue.call_args_list if c.args and c.args[0] == "FollowStyle"]
    assert follow_calls == []

    # Case 2: FollowStyle equals the style's own name (present after insert)
    temp_doc2 = MagicMock()
    temp_cursor2 = MagicMock()
    dest_family2 = temp_doc2.getStyleFamilies.return_value.getByName.return_value
    inserted_styles2: dict[str, Any] = {}
    dest_family2.hasByName.side_effect = lambda name: name in inserted_styles2

    call_order: list[str] = []

    def _insert2(name: str, cl: Any) -> None:
        call_order.append(f"insert:{name}")
        inserted_styles2[name] = cl

    dest_family2.insertByName.side_effect = _insert2

    clone2 = MagicMock()

    def _set_prop2(name: str, val: Any) -> None:
        if name == "FollowStyle":
            call_order.append(f"follow:{val}")

    clone2.setPropertyValue.side_effect = _set_prop2
    temp_doc2.createInstance.return_value = clone2

    src_doc2 = MagicMock()
    src_family2 = src_doc2.getStyleFamilies.return_value.getByName.return_value
    src_family2.hasByName.return_value = True
    source_style2 = MagicMock()
    source_style2.getParentStyle.return_value = ""
    source_style2.getPropertyValue.side_effect = lambda name: "SelfFollow" if name == "FollowStyle" else None
    src_family2.getByName.return_value = source_style2

    with patch("plugin.writer.styles.copy_direct_style_properties", return_value=([], [])):
        res2 = rc.apply_temp_para_style(temp_doc2, temp_cursor2, "SelfFollow", src_doc=src_doc2)

    assert res2 is True
    temp_cursor2.setPropertyValue.assert_called_with("ParaStyleName", "SelfFollow")
    assert call_order == ["insert:SelfFollow", "follow:SelfFollow"]
    rc.release_scratch_style_holds(temp_doc)
    rc.release_scratch_style_holds(temp_doc2)


def test_apply_temp_para_style_disposal_propagates() -> None:
    temp_doc = MagicMock()
    temp_cursor = MagicMock()
    temp_doc.getStyleFamilies.return_value.getByName.return_value.hasByName.return_value = False

    src_doc = MagicMock()
    src_family = src_doc.getStyleFamilies.return_value.getByName.return_value
    src_family.hasByName.return_value = True
    src_family.getByName.side_effect = DocumentDisposedError("test document disposed")

    with pytest.raises(DocumentDisposedError):
        rc.apply_temp_para_style(temp_doc, temp_cursor, "CustomStyle", src_doc=src_doc)


def test_apply_temp_para_style_non_str_parent_does_not_recurse() -> None:
    from typing import Any

    temp_doc = MagicMock()
    temp_cursor = MagicMock()
    dest_family = temp_doc.getStyleFamilies.return_value.getByName.return_value
    inserted_styles: dict[str, Any] = {}
    dest_family.hasByName.side_effect = lambda name: name in inserted_styles
    dest_family.insertByName.side_effect = lambda name, clone: inserted_styles.setdefault(name, clone)

    src_doc = MagicMock()
    src_family = src_doc.getStyleFamilies.return_value.getByName.return_value
    src_family.hasByName.return_value = True
    source_style = MagicMock()
    # Mock getParentStyle returns a truthy non-str (MagicMock)
    source_style.getParentStyle.return_value = MagicMock()
    source_style.getPropertyValue.return_value = ""
    src_family.getByName.return_value = source_style

    with patch("plugin.writer.styles.copy_direct_style_properties", return_value=([], [])):
        res = rc.apply_temp_para_style(temp_doc, temp_cursor, "MyStyle", src_doc=src_doc)

    assert res is True
    assert temp_doc.createInstance.call_count == 1
    assert temp_cursor.setPropertyValue.call_args_list == [call("ParaStyleName", "MyStyle")]


def test_release_scratch_style_holds_drops_the_proxy() -> None:
    temp_doc = MagicMock()
    temp_cursor = MagicMock()
    dest_family = temp_doc.getStyleFamilies.return_value.getByName.return_value
    dest_family.hasByName.return_value = False

    clone = MagicMock()
    temp_doc.createInstance.return_value = clone

    src_doc = MagicMock()
    src_family = src_doc.getStyleFamilies.return_value.getByName.return_value
    src_family.hasByName.return_value = True
    source_style = MagicMock()
    source_style.getParentStyle.return_value = ""
    source_style.getPropertyValue.return_value = ""
    src_family.getByName.return_value = source_style

    with patch("plugin.writer.styles.copy_direct_style_properties", return_value=([], [])):
        res = rc.apply_temp_para_style(temp_doc, temp_cursor, "CustomStyle", src_doc=src_doc)

    assert res is True
    doc_id = id(temp_doc)
    assert doc_id in rc._scratch_style_holds
    assert rc._scratch_style_holds[doc_id] == [clone]

    rc.release_scratch_style_holds(temp_doc)
    assert doc_id not in rc._scratch_style_holds

    # Second release is a no-op
    rc.release_scratch_style_holds(temp_doc)
    assert doc_id not in rc._scratch_style_holds


def test_every_para_writer_property_is_copied_by_range_reads(monkeypatch) -> None:
    """Every UNO property written from data-lo-para must be in _COPIED_PARA_PROPERTIES.

    Range and header reads copy paragraphs into a scratch document first, so any
    property missing from this list is dropped on range and header content reads.
    """
    from plugin.writer import html_import_styles, styles

    class _FakeStruct:
        Mode = 0
        Height = 0

    monkeypatch.setitem(styles.uno.__dict__, "createUnoStruct", lambda _name: _FakeStruct())
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
    rc.begin_next_paragraph(dest_text, cursor)
    dest_text.insertControlCharacter.assert_called_once_with(cursor, rc.PARAGRAPH_BREAK, False)
    cursor.gotoNextParagraph.assert_called_once_with(False)

    dest_text.reset_mock()
    cursor.reset_mock()
    dest_text.insertControlCharacter.side_effect = RuntimeError("break refused")
    rc.begin_next_paragraph(dest_text, cursor)
    cursor.gotoNextParagraph.assert_not_called()


def test_copy_field_into_sets_offset():
    from plugin.writer.range_copy import _copy_field_into

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

    # Input fields copy Content and Hint; other services (e.g. TableFormula) do not.
    for svc, expect_content in (
        ("com.sun.star.text.textfield.Input", True),
        ("com.sun.star.text.textfield.TableFormula", False),
    ):
        fld = MagicMock()
        fld.getSupportedServiceNames.return_value = [svc]
        fld.getPropertyValue.side_effect = lambda name: {"Content": "val", "Hint": "hint"}.get(name)
        dest_doc2 = MagicMock()
        copied_fld = MagicMock()
        dest_doc2.createInstance.return_value = copied_fld
        assert _copy_field_into(dest_doc2, dest_text, dest_cursor, fld) is True
        copied_props = [c.args[0] for c in copied_fld.setPropertyValue.call_args_list]
        assert ("Content" in copied_props) is expect_content
        assert ("Hint" in copied_props) is expect_content


def test_copy_flow_into_xtext_one_paragraph_text_then_field():
    from plugin.writer.range_copy import (
        _COPIED_PARA_PROPERTIES,
        copy_flow_into_xtext,
    )

    doc = MagicMock()
    src_text = MagicMock()

    portion_text = MagicMock()
    portion_text.getPropertyValue.return_value = "Text"
    portion_text.getPropertyValues.side_effect = lambda names: tuple(
        150.0 if n == "CharWeight" else None for n in names
    )
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
    run = MagicMock()
    run.getPropertyValues.side_effect = lambda names: tuple(
        100.0 if n == "CharWeight" else None for n in names
    )
    dest_text.createTextCursor.return_value = cursor
    dest_text.createTextCursorByRange.return_value = run

    with patch("plugin.writer.range_copy._copy_field_into", return_value=True) as mock_field_into, \
         patch("plugin.writer.range_copy._copy_properties") as mock_props, \
         patch("plugin.writer.html_export.new_blank_writer") as mock_blank:
        res = copy_flow_into_xtext(doc, src_text, dest_text)

    assert res == 1
    cursor.setString.assert_called_with("")
    dest_text.insertString.assert_any_call(cursor, "Page ", False)
    dest_text.insertControlCharacter.assert_not_called()
    mock_field_into.assert_called_once()
    mock_blank.assert_not_called()
    run.setPropertyValue.assert_called_once_with("CharWeight", 150.0)

    prop_tuples = [call.args[2] for call in mock_props.call_args_list]
    assert _COPIED_PARA_PROPERTIES in prop_tuples


def test_copy_flow_into_xtext_breaks_only_between_paragraphs():
    from plugin.writer.range_copy import PARAGRAPH_BREAK, copy_flow_into_xtext

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

    with patch("plugin.writer.range_copy._copy_properties"):
        copy_flow_into_xtext(doc, src_text, dest_text)

    dest_text.insertControlCharacter.assert_called_once_with(cursor, PARAGRAPH_BREAK, False)

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


def test_copy_xtext_into_doc_does_not_select_and_clones_graphic():
    from types import SimpleNamespace
    from plugin.writer.range_copy import copy_xtext_into_doc

    src_doc = MagicMock()
    graphic = object()
    shape = MagicMock()
    shape.supportsService.side_effect = lambda name: name == "com.sun.star.text.TextGraphicObject"
    shape.getPropertyValue.side_effect = lambda name: {
        "AnchorType": SimpleNamespace(value="AT_CHARACTER"), "Graphic": graphic, "Width": 100, "Height": 100,
    }.get(name)
    frame_portion = MagicMock()
    frame_portion.getPropertyValue.side_effect = lambda name: "Frame" if name == "TextPortionType" else None
    frame_portion.getString.return_value = ""
    frame_portion.createContentEnumeration.return_value = _Enum([shape])
    para = MagicMock()
    para.supportsService.return_value = False
    para.createEnumeration.return_value = _Enum([frame_portion])
    para.createContentEnumeration.return_value = _Enum([])
    src_text = MagicMock()
    src_text.createEnumeration.return_value = _Enum([para])

    dest_text = MagicMock()
    dest_cursor = MagicMock()
    dest_text.createTextCursor.return_value = dest_cursor
    dest_text.getString.return_value = ""
    dest_doc = MagicMock()
    dest_doc.getText.return_value = dest_text
    clone = MagicMock()
    dest_doc.createInstance.return_value = clone

    copy_xtext_into_doc(src_doc, src_text, dest_doc)

    src_doc.getCurrentController().select.assert_not_called()
    dest_doc.createInstance.assert_called_with("com.sun.star.text.TextGraphicObject")
    clone.setPropertyValue.assert_any_call("Graphic", graphic)
    dest_text.insertTextContent.assert_called()




def test_copy_properties_skips_values_the_style_already_gives():
    """Copying an inherited value would make it a hand-set one on the copy, and the read would
    then report margin-right:0cm on every paragraph as if the user had set it."""
    from plugin.writer import range_copy as rc

    src, dst, style = MagicMock(), MagicMock(), MagicMock()
    src.getPropertyValue.side_effect = {"ParaLeftMargin": 3251, "ParaRightMargin": 0}.get
    style.getPropertyValue.side_effect = {"ParaLeftMargin": 0, "ParaRightMargin": 0}.get

    rc._copy_properties(src, dst, ("ParaLeftMargin", "ParaRightMargin"), style)

    dst.setPropertyValue.assert_called_once_with("ParaLeftMargin", 3251)


def test_copy_properties_without_a_style_copies_everything_readable():
    from plugin.writer import range_copy as rc

    src, dst = MagicMock(), MagicMock()
    src.getPropertyValue.side_effect = {"CharWeight": 150.0}.get

    rc._copy_properties(src, dst, ("CharWeight",))

    dst.setPropertyValue.assert_called_once_with("CharWeight", 150.0)


def test_copy_properties_continues_past_one_the_target_refuses():
    """The temp document is a plain Writer doc and need not offer every source property."""
    from plugin.writer import range_copy as rc

    src, dst = MagicMock(), MagicMock()
    src.getPropertyValue.side_effect = {"CharPosture": 1, "CharWeight": 150.0}.get

    def refuse_posture(name, _value):
        if name == "CharPosture":
            raise RuntimeError("unsupported")

    dst.setPropertyValue.side_effect = refuse_posture

    rc._copy_properties(src, dst, ("CharPosture", "CharWeight"))

    assert [c.args[0] for c in dst.setPropertyValue.call_args_list] == ["CharPosture", "CharWeight"]


def test_paint_direct_formatting_still_paints_char_when_para_copy_raises():
    """A refused Para* must not skip the portion Char* loop — that paint is why the
    temp-doc path exists (bold/colour on a range read)."""
    from plugin.writer import range_copy as rc

    para = MagicMock()
    portion = MagicMock()
    portion.getPropertyValue.side_effect = lambda name: 150.0 if name == "CharWeight" else None
    para_start = MagicMock(name="para_start")
    para_cursor = MagicMock()
    para_cursor.getStart.return_value = para_start
    para_cursor.gotoEndOfParagraph.side_effect = RuntimeError("refused Para*")
    run = MagicMock()
    temp_text = MagicMock()
    temp_text.createTextCursor.return_value = para_cursor
    temp_text.createTextCursorByRange.return_value = run

    rc.paint_direct_formatting(para, [(portion, "bold")], temp_text, 0, 4)

    run.setPropertyValue.assert_any_call("CharWeight", 150.0)


def test_paint_direct_formatting_puts_paragraph_font_on_the_paragraph_not_each_run():
    """Paragraph-level font/colour is painted on the paragraph; runs only get what differs."""
    from plugin.writer import range_copy as rc

    para = MagicMock()
    para.getPropertyValue.side_effect = lambda name: 150.0 if name == "CharWeight" else None
    style = MagicMock()
    style.getPropertyValue.side_effect = lambda name: 100.0 if name == "CharWeight" else None
    portion = MagicMock()
    portion.getPropertyValue.side_effect = (
        lambda name: 150.0 if name == "CharWeight" else (2 if name == "CharPosture" else None)
    )

    para_start = MagicMock(name="para_start")
    para_cursor = MagicMock()
    para_cursor.getStart.return_value = para_start
    run = MagicMock()
    temp_text = MagicMock()
    temp_text.createTextCursor.return_value = para_cursor
    temp_text.createTextCursorByRange.return_value = run

    rc.paint_direct_formatting(para, [(portion, "bold")], temp_text, 0, 4, style)

    para_cursor.setPropertyValue.assert_any_call("CharWeight", 150.0)
    run_props = [c.args[0] for c in run.setPropertyValue.call_args_list]
    assert "CharWeight" not in run_props
    run.setPropertyValue.assert_any_call("CharPosture", 2)


def test_source_style_is_cached_and_tolerates_a_missing_style():
    from plugin.writer import range_copy as rc

    model = MagicMock()
    families = model.getStyleFamilies.return_value.getByName.return_value
    families.getByName.side_effect = lambda n: "STYLE" if n == "Standard" else (_ for _ in ()).throw(KeyError(n))

    cache = {}
    assert rc.source_style(model, "Standard", cache) == "STYLE"
    assert rc.source_style(model, "Standard", cache) == "STYLE"
    assert families.getByName.call_count == 1  # second read served from the cache
    assert rc.source_style(model, "Nope", cache) is None
    assert rc.source_style(model, "", cache) is None


def test_copy_cell_xtext_clones_frame_graphic_only_when_graphics_true():
    from plugin.writer import range_copy as rc

    class _Enum:
        def __init__(self, items):
            self._items = list(items)

        def hasMoreElements(self):
            return bool(self._items)

        def nextElement(self):
            return self._items.pop(0)

    frame_portion = MagicMock()
    frame_portion.getPropertyValue.side_effect = lambda k: "Frame" if k == "TextPortionType" else None

    para = MagicMock()
    para.supportsService.return_value = False
    para.createEnumeration.return_value = _Enum([frame_portion])

    src_cell = MagicMock()
    src_cell.createEnumeration.side_effect = lambda: _Enum([para])
    dest_cell = MagicMock()

    with (
        patch.object(rc, "visible_portions", return_value=[(frame_portion, "")]),
        patch.object(rc, "_copy_math_or_text", return_value=""),
        patch.object(rc, "clone_graphics", return_value=1) as clone,
    ):
        rc._copy_cell_xtext(MagicMock(), src_cell, MagicMock(), dest_cell, TrackedDeletions(MagicMock()))
        assert clone.call_count == 2
        assert clone.call_args_list[0].args[0] is frame_portion
        assert clone.call_args_list[1].args[0] is para


def test_iter_nested_texts_walks_nested_tables():
    from plugin.doc.text_helpers import DeletionState
    from plugin.writer.range_copy import iter_nested_texts

    class _Enum:
        def __init__(self, items):
            self._items = list(items)

        def hasMoreElements(self):
            return bool(self._items)

        def nextElement(self):
            return self._items.pop(0)

    def _rows():
        r = MagicMock()
        r.getPropertyValue.return_value = True
        rows = MagicMock()
        rows.getCount.return_value = 1
        rows.getByIndex.return_value = r
        return rows

    inner_cell = MagicMock()
    inner_cell.createEnumeration.return_value = _Enum([])
    inner_tbl = MagicMock()
    inner_tbl.supportsService.side_effect = lambda s: s == "com.sun.star.text.TextTable"
    inner_tbl.getRows.return_value = _rows()
    inner_tbl.getCellNames.return_value = ["A1"]
    inner_tbl.getCellByName.return_value = inner_cell

    outer_cell = MagicMock()
    outer_cell.createEnumeration.return_value = _Enum([inner_tbl])
    outer_tbl = MagicMock()
    outer_tbl.supportsService.side_effect = lambda s: s == "com.sun.star.text.TextTable"
    outer_tbl.getRows.return_value = _rows()
    outer_tbl.getCellNames.return_value = ["A1"]
    outer_tbl.getCellByName.return_value = outer_cell

    root = MagicMock()
    root.createEnumeration.return_value = _Enum([outer_tbl])
    assert list(iter_nested_texts(MagicMock(), root, DeletionState())) == [root, outer_cell, inner_cell]


def test_iter_nested_texts_skips_struck_table():
    from plugin.doc.text_helpers import DeletionState
    from plugin.writer.range_copy import iter_nested_texts

    class _Enum:
        def __init__(self, items):
            self._items = list(items)

        def hasMoreElements(self):
            return bool(self._items)

        def nextElement(self):
            return self._items.pop(0)

    cell = MagicMock()
    tbl = MagicMock()
    tbl.supportsService.side_effect = lambda s: s == "com.sun.star.text.TextTable"
    tbl.getCellNames.return_value = ["A1"]
    tbl.getCellByName.return_value = cell

    root = MagicMock()
    root.createEnumeration.return_value = _Enum([tbl])
    with patch("plugin.writer.range_copy.table_is_struck", return_value=True):
        assert list(iter_nested_texts(MagicMock(), root, DeletionState())) == [root]


def test_table_is_struck():
    from plugin.doc.text_helpers import DeletionState
    from plugin.writer.range_copy import table_is_struck

    def _table(row_flags):
        tbl = MagicMock()
        rows = MagicMock()
        rows.getCount.return_value = len(row_flags)
        objs = []
        for flag in row_flags:
            r = MagicMock()
            r.getPropertyValue.side_effect = lambda p, f=flag: f if p == "HasTextChangesOnly" else None
            objs.append(r)
        rows.getByIndex.side_effect = lambda i: objs[i]
        tbl.getRows.return_value = rows
        return tbl

    doc = MagicMock()
    assert table_is_struck(doc, _table([True]), DeletionState(True)) is True
    with patch("plugin.writer.range_copy.table_is_all_deleted", return_value=True):
        assert table_is_struck(doc, _table([False, False]), DeletionState(False)) is True
        assert table_is_struck(doc, _table([False, True]), DeletionState(False)) is False
    with patch("plugin.writer.range_copy.table_is_all_deleted", return_value=False):
        assert table_is_struck(doc, _table([False, False]), DeletionState(False)) is False


def test_clone_graphics_keeps_automatic_name_when_taken():
    from plugin.writer.range_copy import clone_graphics

    shape = MagicMock()
    shape.supportsService.side_effect = lambda s: s == "com.sun.star.text.TextGraphicObject"
    shape.getName.return_value = "Image1"
    shape.getPropertyValue.return_value = MagicMock(value="AT_PARAGRAPH")

    enum = MagicMock()
    enum.hasMoreElements.side_effect = [True, False]
    enum.nextElement.return_value = shape
    host = MagicMock()
    host.createContentEnumeration.return_value = enum

    clone = MagicMock()
    dest_doc = MagicMock()
    dest_doc.createInstance.return_value = clone
    dest_doc.getGraphicObjects.return_value.hasByName.return_value = True

    clone_graphics(host, dest_doc, MagicMock(), MagicMock())
    clone.setName.assert_not_called()


def test_copy_cell_xtext_marks_struck_only_when_requested():
    from plugin.writer import range_copy as rc
    from plugin.writer.xhtml_style_postprocess import DELETED_MARK

    def _run(*, mark_struck: bool):
        para = MagicMock()
        para.supportsService.return_value = False
        para.getString.return_value = "gone"
        redline_start = MagicMock()
        redline_start.getPropertyValue.side_effect = lambda k: (
            "Redline" if k == "TextPortionType" else ("Delete" if k == "RedlineType" else True)
        )
        redline_end = MagicMock()
        redline_end.getPropertyValue.side_effect = lambda k: (
            "Redline" if k == "TextPortionType" else ("Delete" if k == "RedlineType" else False)
        )
        text_portion = MagicMock()
        text_portion.getPropertyValue.side_effect = lambda k: "Text" if k == "TextPortionType" else None
        text_portion.getString.return_value = "gone"
        p_enum = MagicMock()
        p_enum.hasMoreElements.side_effect = [True, True, True, False]
        p_enum.nextElement.side_effect = [redline_start, text_portion, redline_end]
        para.createEnumeration.return_value = p_enum
        para.createContentEnumeration.return_value.hasMoreElements.return_value = False

        cell_enum = MagicMock()
        cell_enum.hasMoreElements.side_effect = [True, False]
        cell_enum.nextElement.return_value = para
        src_cell = MagicMock()
        src_cell.createEnumeration.return_value = cell_enum

        dest_cell = MagicMock()
        dest_cursor = MagicMock()
        dest_cell.createTextCursor.return_value = dest_cursor

        with patch.object(rc, "clone_graphics", return_value=0):
            rc._copy_cell_xtext(MagicMock(), src_cell, MagicMock(), dest_cell, TrackedDeletions(MagicMock()), mark_struck=mark_struck)
        return dest_cell

    marked = _run(mark_struck=True)
    marked.insertString.assert_any_call(marked.createTextCursor.return_value, DELETED_MARK, False)

    unmarked = _run(mark_struck=False)
    unmarked.insertString.assert_not_called()


def test_copy_cell_xtext_skips_struck_nested_table():
    from plugin.writer import range_copy as rc

    nested_tbl = MagicMock()
    nested_tbl.supportsService.side_effect = lambda s: s == "com.sun.star.text.TextTable"
    cell_enum = MagicMock()
    cell_enum.hasMoreElements.side_effect = [True, False]
    cell_enum.nextElement.return_value = nested_tbl
    src_cell = MagicMock()
    src_cell.createEnumeration.return_value = cell_enum

    with (
        patch.object(rc, "table_is_struck", return_value=True),
        patch.object(rc, "copy_table") as mock_copy_table,
    ):
        rc._copy_cell_xtext(MagicMock(), src_cell, MagicMock(), MagicMock(), TrackedDeletions(MagicMock()), mark_struck=True)
    mock_copy_table.assert_not_called()


def test_cells_all_tracked_deleted_accepts_struck_marker_without_raw_text():
    from plugin.doc.text_helpers import DeletionState
    from plugin.writer.range_copy import cells_all_tracked_deleted

    cell = MagicMock()
    cell.getString.return_value = ""

    def _state_with_delete(_c):
        return DeletionState(saw_delete=True)

    def _state_blank(_c):
        return DeletionState(saw_delete=False)

    with (
        patch("plugin.writer.range_copy.TrackedDeletions") as td_cls,
        patch("plugin.writer.range_copy.get_string_without_tracked_deletions", return_value=""),
    ):
        td_cls.return_value.state_at.side_effect = _state_with_delete
        assert cells_all_tracked_deleted([cell], MagicMock()) is True

        td_cls.return_value.state_at.side_effect = _state_blank
        assert cells_all_tracked_deleted([cell], MagicMock()) is False
