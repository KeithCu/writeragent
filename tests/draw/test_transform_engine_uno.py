# WriterAgent — EditTextObject range formatting on a real Draw shape.
# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

from plugin.draw.transform_engine import _apply_cursor_uno_format
from plugin.testing_runner import native_test
from tests.testing_utils import with_native_doc


@native_test
@with_native_doc("draw")
def test_range_bold_does_not_format_the_rest_of_the_shape(ctx, doc):
    """Bold on a text cursor must stick to that range.

    Dispatch selects the shape, so ``.uno:Bold`` used to mark every character.
    The cursor properties are plain UNO numbers (FontWeight.BOLD is 150).
    """
    page = doc.getDrawPages().getByIndex(0)
    shape = doc.createInstance("com.sun.star.drawing.TextShape")
    page.add(shape)
    shape.setString("Hello")
    selected = shape.getText().createTextCursor()
    selected.gotoStart(False)
    selected.goRight(2, True)
    assert selected.getString() == "He"
    assert _apply_cursor_uno_format(selected, ".uno:Bold", {})

    bold = shape.getText().createTextCursor()
    bold.gotoStart(False)
    bold.goRight(2, True)
    rest = shape.getText().createTextCursor()
    rest.gotoStart(False)
    rest.goRight(2, False)
    rest.goRight(3, True)
    assert float(bold.CharWeight) >= 150.0
    assert float(rest.CharWeight) < 150.0
    assert rest.getString() == "llo"
    assert _apply_cursor_uno_format(rest, ".uno:Italic", {})
    # PyUNO exposes the enum name. 2 is FontSlant.ITALIC; the bold range stays upright.
    assert str(rest.CharPosture.value) == "ITALIC"
    assert str(bold.CharPosture.value) != "ITALIC"


@native_test
@with_native_doc("draw")
def test_select_text_same_paragraph_absolute_span(ctx, doc):
    """[0, 1, 0, 3] selects absolute span [1, 3] (length 2), not start + end."""
    from plugin.draw.transform_engine import SlideCommandEngine
    from plugin.framework.tool import ToolContext

    page = doc.getDrawPages().getByIndex(0)
    shape = doc.createInstance("com.sun.star.drawing.TextShape")
    page.add(shape)
    shape.setString("Hello world")

    tctx = ToolContext(doc, ctx, "draw", None, "test")
    eng = SlideCommandEngine(tctx)
    xtext = shape.getText()
    cursor = xtext.createTextCursor()
    selected, mode = eng._select_text(xtext, cursor, [0, 1, 0, 3])
    # Absolute span from index 1 to 3 covers 'el' (length 2), not 'ell' (length 3).
    assert selected.getString() == "el"
    assert mode == "range"


@native_test
@with_native_doc("impress")
def test_select_text_five_forms_on_impress_text_shape(ctx, doc):
    """All five SelectText forms select correct spans on an Impress text shape."""
    from plugin.doc.text_helpers import normalize_linebreaks
    from plugin.draw.transform_engine import SlideCommandEngine, _set_shape_text
    from plugin.framework.tool import ToolContext

    page = doc.getDrawPages().getByIndex(0)
    shape = doc.createInstance("com.sun.star.drawing.TextShape")
    page.add(shape)
    _set_shape_text(shape, "first paragraph\nsecond paragraph\nthird paragraph")

    tctx = ToolContext(doc, ctx, "impress", None, "test")
    eng = SlideCommandEngine(tctx)
    xtext = shape.getText()

    cursor = xtext.createTextCursor()
    selected, mode = eng._select_text(xtext, cursor, [])
    assert mode == "all", f"expected mode 'all', got {mode!r}"
    assert normalize_linebreaks(selected.getString()) == "first paragraph\nsecond paragraph\nthird paragraph", (
        f"unexpected selection: {selected.getString()!r}"
    )

    cursor = xtext.createTextCursor()
    selected, mode = eng._select_text(xtext, cursor, [1])
    assert mode == "range", f"expected mode 'range', got {mode!r}"
    assert normalize_linebreaks(selected.getString()) == "second paragraph", (
        f"unexpected selection: {selected.getString()!r}"
    )

    cursor = xtext.createTextCursor()
    selected, mode = eng._select_text(xtext, cursor, [0, 6])
    assert mode == "caret", f"expected mode 'caret', got {mode!r}"
    # Impress TextCursor.getString() often returns the whole shape even for a caret.
    is_collapsed = getattr(selected, "isCollapsed", None)
    if callable(is_collapsed) and is_collapsed():
        pass
    elif normalize_linebreaks(selected.getString()) not in ("", "first paragraph\nsecond paragraph\nthird paragraph"):
        raise AssertionError(f"unexpected caret selection: {selected.getString()!r}")

    cursor = xtext.createTextCursor()
    selected, mode = eng._select_text(xtext, cursor, [0, 6, 1])
    assert mode == "range", f"expected mode 'range', got {mode!r}"
    assert normalize_linebreaks(selected.getString()) == "paragraph\nsecond paragraph", (
        f"unexpected selection: {selected.getString()!r}"
    )

    cursor = xtext.createTextCursor()
    selected, mode = eng._select_text(xtext, cursor, [0, 6, 1, 6])
    assert mode == "range", f"expected mode 'range', got {mode!r}"
    assert normalize_linebreaks(selected.getString()) == "paragraph\nsecond", (
        f"unexpected selection: {selected.getString()!r}"
    )

    cursor = xtext.createTextCursor()
    selected, mode = eng._select_text(xtext, cursor, [99])
    assert mode is None, f"expected mode None, got {mode!r}"

    cursor = xtext.createTextCursor()
    selected, mode = eng._select_text(xtext, cursor, [0, 0, 99])
    assert mode is None, f"expected mode None, got {mode!r}"

    cursor = xtext.createTextCursor()
    selected, mode = eng._select_text(xtext, cursor, [0, 0, 0, 99])
    assert mode is None, f"expected mode None, got {mode!r}"


@native_test
@with_native_doc("impress")
def test_insert_text_leaves_selection_for_formatting(ctx, doc):
    """InsertText leaves text selected so subsequent .uno:Bold and .uno:Italic format it."""
    from plugin.draw.transform_engine import SlideCommandEngine
    from plugin.framework.tool import ToolContext

    tctx = ToolContext(doc, ctx, "impress", None, "test")
    eng = SlideCommandEngine(tctx)
    res = eng.apply(
        {
            "Transforms": {
                "SlideCommands": [
                    {"ChangeLayoutByName": "AUTOLAYOUT_TITLE_CONTENT"},
                    {"SetText.1": "First line\nSecond line"},
                    {
                        "EditTextObject.1": [
                            {"SelectParagraph": 0},
                            {"InsertText": "Revenue grew 15% YoY"},
                            {"UnoCommand": ".uno:Bold"},
                            {"UnoCommand": ".uno:Italic"},
                        ]
                    },
                ]
            }
        }
    )
    assert res.get("status") == "ok", res
    page = doc.getDrawPages().getByIndex(0)
    shape = page.getByIndex(1)
    if hasattr(shape, "IsEmptyPresentationObject"):
        assert shape.IsEmptyPresentationObject is False

    xtext = shape.getText()
    first, second = "Revenue grew 15% YoY", "Second line"
    assert xtext.getString().replace("\r\n", "\n").split("\n")[:2] == [first, second]
    cursor = xtext.createTextCursor()
    cursor.gotoStart(False)
    cursor.goRight(len(first), True)
    assert cursor.getString() == first
    assert float(cursor.CharWeight) >= 150.0
    assert str(cursor.CharPosture.value) == "ITALIC"

    cursor.gotoStart(False)
    cursor.goRight(len(first) + 1, False)
    cursor.goRight(len(second), True)
    assert cursor.getString() == second
    assert str(cursor.CharPosture.value) != "ITALIC"


@native_test
@with_native_doc("impress")
def test_whole_shape_selection_formats_cursor_without_toggle(ctx, doc):
    """SelectText: [] applies mapped UNO formatting to full cursor without toggling."""
    from plugin.draw.transform_engine import SlideCommandEngine, _set_shape_text
    from plugin.framework.tool import ToolContext

    page = doc.getDrawPages().getByIndex(0)
    shape = doc.createInstance("com.sun.star.drawing.TextShape")
    page.add(shape)
    shape_idx = page.getCount() - 1
    _set_shape_text(shape, "Hello\nHello")

    tctx = ToolContext(doc, ctx, "impress", None, "test")
    eng = SlideCommandEngine(tctx)
    res = eng.apply(
        {
            "Transforms": {
                "SlideCommands": [
                    {
                        f"EditTextObject.{shape_idx}": [
                            {"SelectText": []},
                            {"UnoCommand": ".uno:Bold"},
                            {"UnoCommand": ".uno:CenterPara"},
                        ]
                    }
                ]
            }
        }
    )
    assert res.get("status") == "ok", f"apply failed: {res}"

    xtext = shape.getText()
    cursor = xtext.createTextCursor()
    cursor.gotoStart(False)
    cursor.goRight(len("Hello"), True)
    assert cursor.getString() == "Hello", f"expected 'Hello', got {cursor.getString()!r}"
    assert float(cursor.CharWeight) >= 150.0, f"first paragraph not bold: weight={cursor.CharWeight}"

    # Second paragraph not left behind
    cursor.gotoStart(False)
    cursor.goRight(len("Hello") + 1, False)
    cursor.goRight(len("Hello"), True)
    assert cursor.getString() == "Hello", f"expected 'Hello', got {cursor.getString()!r}"
    assert float(cursor.CharWeight) >= 150.0, f"second paragraph not bold: weight={cursor.CharWeight}"

    # Second .uno:Bold on full span does not clear weight
    res2 = eng.apply(
        {
            "Transforms": {
                "SlideCommands": [
                    {
                        f"EditTextObject.{shape_idx}": [
                            {"SelectText": []},
                            {"UnoCommand": ".uno:Bold"},
                        ]
                    }
                ]
            }
        }
    )
    assert res2.get("status") == "ok", f"second apply failed: {res2}"
    cursor.gotoStart(False)
    cursor.goRight(len("Hello"), True)
    assert cursor.getString() == "Hello", f"expected 'Hello', got {cursor.getString()!r}"
    assert float(cursor.CharWeight) >= 150.0, f"bold cleared on second apply: weight={cursor.CharWeight}"


