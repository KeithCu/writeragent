# WriterAgent — EditTextObject range formatting on a real Draw shape.
# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

from plugin.draw.transform_engine import _apply_cursor_uno_format
from plugin.testing_runner import native_test
from plugin.tests.testing_utils import with_native_doc


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
