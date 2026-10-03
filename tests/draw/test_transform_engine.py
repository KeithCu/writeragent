# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Current-slide bookkeeping and EditTextObject selection (no LibreOffice)."""

from plugin.draw.transform_engine import (
    SlideCommandEngine,
    apply_text_cursor_command,
    current_slide_after_delete,
    current_slide_after_move,
    insert_text_leave_selected,
    text_selection_bounds,
)


def test_move_other_slide_shifts_current_instead_of_following_destination():
    # current 2, MoveSlide.0 → 3. Former index 2 is now index 1.
    assert current_slide_after_move(2, 0, 3) == 1


def test_move_active_slide_follows_it():
    assert current_slide_after_move(2, 2, 0) == 0


def test_move_from_after_current_to_before_shifts_up():
    assert current_slide_after_move(1, 3, 0) == 2


def test_move_that_does_not_cross_current_keeps_it():
    assert current_slide_after_move(2, 0, 1) == 2
    assert current_slide_after_move(1, 3, 2) == 1


def test_delete_at_or_before_current_decrements():
    # 4 slides, current 2, delete 0 → the old current is now index 1.
    assert current_slide_after_delete(2, 0, 3) == 1
    # Deleting the current slide itself also decrements (not "stay and clamp").
    assert current_slide_after_delete(2, 2, 3) == 1


def test_delete_after_current_keeps_index():
    assert current_slide_after_delete(2, 3, 3) == 2


def test_delete_first_while_on_it_clamps_to_zero():
    assert current_slide_after_delete(0, 0, 2) == 0


class _Pages:
    def __init__(self, count: int) -> None:
        self.count = count
        self.moved: tuple[int, int] | None = None

    def getCount(self) -> int:
        return self.count

    def get_pages(self) -> "_Pages":
        return self

    def move_slide(self, from_idx: int, to_idx: int) -> bool:
        self.moved = (from_idx, to_idx)
        return True

    def delete_slide(self, index: int) -> None:
        self.count -= 1


def _engine(current: int, pages: _Pages) -> SlideCommandEngine:
    engine = SlideCommandEngine.__new__(SlideCommandEngine)
    engine.current_slide = current
    engine.applied = []
    engine.warnings = []
    engine.bridge = pages
    engine.pages = pages
    return engine


def test_move_slide_uses_lo_current_rule():
    pages = _Pages(4)
    engine = _engine(2, pages)
    engine._move_slide(0, 3)
    assert pages.moved == (0, 3)
    assert engine.current_slide == 1
    assert engine.applied == ["MoveSlide:0->3"]


def test_delete_slide_decrements_current():
    pages = _Pages(4)
    engine = _engine(2, pages)
    engine._delete_slide(0)
    assert engine.current_slide == 1
    assert engine.applied == ["DeleteSlide:0"]


def test_select_text_bounds_match_eselection():
    assert text_selection_bounds("HelloWorld", [0, 0, 0, 4]) == (0, 4)
    assert text_selection_bounds("Hello\nWorld", [0]) == (0, 5)
    assert text_selection_bounds("Hello\nWorld", [1]) == (6, 11)
    assert text_selection_bounds("Hello\nWorld", [0, 1, 1, 2]) == (1, 8)
    assert text_selection_bounds("Hello", [0, 2]) == (2, 2)
    assert text_selection_bounds("Hello", []) is None


class _Cursor:
    def __init__(self, *, collapsed: bool, selected: str) -> None:
        self.collapsed = collapsed
        self.selected = selected
        self.left: tuple[int, bool] | None = None

    def setString(self, text: str) -> None:
        self.selected = "" if self.collapsed else text
        if not self.collapsed:
            self.selected = text

    def isCollapsed(self) -> bool:
        return self.collapsed

    def getString(self) -> str:
        return self.selected

    def goLeft(self, count: int, expand: bool) -> None:
        self.left = (count, expand)


def test_insert_text_reselects_when_setstring_collapses():
    cursor = _Cursor(collapsed=True, selected="")
    insert_text_leave_selected(cursor, "Hi")
    assert cursor.left == (2, True)


def test_insert_text_keeps_selection_setstring_already_made():
    cursor = _Cursor(collapsed=False, selected="")
    insert_text_leave_selected(cursor, "Hi")
    assert cursor.getString() == "Hi"
    assert cursor.left is None


class _Props:
    def __init__(self) -> None:
        self.CharWeight = 100.0
        self.NumberingLevel = None
        self.ParaAdjust = 0
        self.CharColor = -1


def test_bold_toggles_weight_on_the_cursor():
    cursor = _Props()
    assert apply_text_cursor_command(cursor, ".uno:Bold") is True
    assert cursor.CharWeight == 150.0
    assert apply_text_cursor_command(cursor, ".uno:Bold") is True
    assert cursor.CharWeight == 100.0


def test_bullet_and_center_and_color_hit_the_cursor():
    cursor = _Props()
    assert apply_text_cursor_command(cursor, ".uno:DefaultBullet") is True
    assert cursor.NumberingLevel == 0
    assert apply_text_cursor_command(cursor, ".uno:CenterPara") is True
    assert cursor.ParaAdjust == 3
    assert apply_text_cursor_command(cursor, ".uno:Color", {"Color.Color": 255}) is True
    assert cursor.CharColor == 255
    assert apply_text_cursor_command(cursor, ".uno:Color", {"Color": {"type": "long", "value": "16711680"}}) is True
    assert cursor.CharColor == 16711680
    assert apply_text_cursor_command(cursor, ".uno:NotATextCommand") is False
