# WriterAgent — SlideCommandEngine current-slide and text-range formatting
# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

from types import SimpleNamespace

from plugin.draw.transform_engine import SlideCommandEngine


class _Pages:
    def __init__(self, count: int) -> None:
        self.count = count
        self.indexes: list[int] = []

    def getCount(self) -> int:
        return self.count

    def getByIndex(self, index: int) -> tuple[str, int]:
        self.indexes.append(index)
        return ("page", index)


class _Bridge:
    def __init__(self, pages: _Pages) -> None:
        self.pages = pages
        self.moved: tuple[int, int] | None = None
        self.deleted: int | None = None
        self.fail = False

    def get_pages(self) -> _Pages:
        return self.pages

    def move_slide(self, from_idx: int, to_idx: int) -> bool:
        self.moved = (from_idx, to_idx)
        return not self.fail

    def delete_slide(self, index: int) -> None:
        self.deleted = index
        self.pages.count -= 1


def _engine(current: int, pages: int) -> tuple[SlideCommandEngine, _Pages, _Bridge]:
    deck = _Pages(pages)
    bridge = _Bridge(deck)
    eng = SlideCommandEngine.__new__(SlideCommandEngine)
    eng.applied = []
    eng.warnings = []
    eng.current_slide = current
    eng.pages = deck
    eng.bridge = bridge
    return eng, deck, bridge


def test_move_other_slide_shifts_current_with_the_logical_page() -> None:
    # Current slide 2, move slide 0 to index 3. LO follows the page that
    # was current (it shifts down), it does not jump onto the moved slide.
    eng, deck, bridge = _engine(2, 4)
    eng._apply_command({"MoveSlide.0": 3})
    assert bridge.moved == (0, 3)
    assert eng.current_slide == 1
    assert eng._current_page() == ("page", 1)
    assert deck.indexes[-1] == 1


def test_move_current_slide_follows_destination() -> None:
    eng, _deck, _bridge = _engine(2, 4)
    eng._apply_command({"MoveSlide": 0})
    assert eng.current_slide == 0


def test_move_from_after_current_shifts_index_up() -> None:
    eng, _deck, _bridge = _engine(1, 4)
    eng._move_slide(3, 0)
    assert eng.current_slide == 2


def test_failed_move_leaves_current_slide() -> None:
    eng, _deck, bridge = _engine(2, 4)
    bridge.fail = True
    eng._move_slide(0, 3)
    assert eng.current_slide == 2
    assert eng.warnings


def test_delete_slide_at_or_before_current_decrements() -> None:
    # 4-slide deck on slide 2. Deleting slide 0 must not leave current on 2.
    eng, _deck, bridge = _engine(2, 4)
    eng._apply_command({"DeleteSlide": 0})
    assert bridge.deleted == 0
    assert eng.current_slide == 1


def test_delete_current_slide_selects_previous() -> None:
    eng, _deck, _bridge = _engine(2, 4)
    eng._delete_slide(2)
    assert eng.current_slide == 1


def test_delete_after_current_keeps_index() -> None:
    eng, _deck, _bridge = _engine(1, 4)
    eng._delete_slide(3)
    assert eng.current_slide == 1


def test_delete_past_end_clamps_current() -> None:
    # Index 99 is clamped to the last slide, then current follows that delete.
    eng, _deck, bridge = _engine(3, 4)
    eng._delete_slide(99)
    assert bridge.deleted == 3
    assert eng.current_slide == 2


def test_delete_only_slide_is_refused() -> None:
    eng, _deck, bridge = _engine(0, 1)
    eng._delete_slide(0)
    assert bridge.deleted is None
    assert eng.current_slide == 0
    assert eng.warnings


class _Cursor:
    def __init__(self, text: str) -> None:
        self._text = text
        self.props: dict[str, object] = {}

    def getString(self) -> str:
        return self._text

    def setPropertyValue(self, name: str, value: object) -> None:
        self.props[name] = value


class _Shape:
    def __init__(self, text: str) -> None:
        self._text = text

    def getText(self) -> "_Shape":
        return self

    def getString(self) -> str:
        return self._text


class _Controller:
    def __init__(self) -> None:
        self.selected: list[object] = []

    def getFrame(self) -> str:
        return "frame"

    def select(self, obj: object) -> None:
        self.selected.append(obj)


class _Dispatcher:
    def __init__(self, calls: list[str]) -> None:
        self._calls = calls

    def executeDispatch(self, frame: object, name: str, frame_name: str, search: int, props: tuple[object, ...]) -> None:
        self._calls.append(name)


def _dispatch_engine() -> tuple[SlideCommandEngine, list[str], _Controller]:
    calls: list[str] = []
    controller = _Controller()

    class _Doc:
        def getCurrentController(self) -> _Controller:
            return controller

    class _Smgr:
        def createInstanceWithContext(self, name: str, ctx: object) -> _Dispatcher:
            assert name == "com.sun.star.frame.DispatchHelper"
            return _Dispatcher(calls)

    eng = SlideCommandEngine.__new__(SlideCommandEngine)
    eng.warnings = []
    eng.doc = _Doc()
    eng.tctx = SimpleNamespace(ctx=SimpleNamespace(ServiceManager=_Smgr()))
    return eng, calls, controller


def test_partial_bold_formats_the_cursor_not_the_shape() -> None:
    eng, calls, controller = _dispatch_engine()
    cursor = _Cursor("Hello")
    shape = _Shape("Hello world")
    eng._dispatch_uno_string(".uno:Bold", cursor=cursor, shape=shape)
    assert cursor.props["CharWeight"] == 150.0
    assert calls == []
    assert controller.selected == []


def test_partial_italic_and_color_stay_on_the_selection() -> None:
    eng, calls, _controller = _dispatch_engine()
    cursor = _Cursor("ab")
    shape = _Shape("abcd")
    eng._dispatch_uno_string(".uno:Italic", cursor=cursor, shape=shape)
    eng._dispatch_uno_string('.uno:Color {"Color.Color":{"type":"long","value":2777241}}', cursor=cursor, shape=shape)
    assert cursor.props["CharPosture"] == 2
    assert cursor.props["CharColor"] == 2777241
    assert calls == []


def test_whole_object_bold_still_dispatches_on_the_shape() -> None:
    eng, calls, controller = _dispatch_engine()
    cursor = _Cursor("Title")
    shape = _Shape("Title")
    eng._dispatch_uno_string(".uno:Bold", cursor=cursor, shape=shape)
    assert calls == [".uno:Bold"]
    assert controller.selected == [shape]
    assert "CharWeight" not in cursor.props


def test_bold_without_a_range_still_dispatches() -> None:
    eng, calls, controller = _dispatch_engine()
    eng._dispatch_uno_string(".uno:Italic")
    assert calls == [".uno:Italic"]
    assert controller.selected == []


def test_unmapped_command_on_a_range_does_not_format_the_whole_shape() -> None:
    eng, calls, controller = _dispatch_engine()
    eng._dispatch_uno_string(".uno:DefaultBullet", cursor=_Cursor("item"), shape=_Shape("item\nmore"))
    assert calls == []
    assert controller.selected == []
    assert any("DefaultBullet" in warning for warning in eng.warnings)

def test_insert_master_not_found_raises_warning():
    from plugin.draw.transform_engine import SlideCommandEngine
    from unittest.mock import MagicMock

    class MockBridge:
        def get_pages(self):
            m = MagicMock()
            m.getCount.return_value = 1
            return m
        def insert_slide_from_master(self, **kwargs):
            raise ValueError("Master page not found")

    engine = SlideCommandEngine(MagicMock())
    engine.bridge = MockBridge()
    engine.pages = engine.bridge.get_pages()

    engine._apply_command({"InsertMasterSlide": 99})
    assert "InsertMasterSlide: Master page not found" in engine.warnings
    assert not any("InsertMasterSlide" in item for item in engine.applied)
