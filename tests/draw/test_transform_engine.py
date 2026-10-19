# WriterAgent — SlideCommandEngine current-slide and text-range formatting
# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from plugin.draw.transform_engine import SlideCommandEngine
import pytest


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
        self.active_page = 0

    def get_pages(self) -> _Pages:
        return self.pages

    def move_slide(self, from_idx: int, to_idx: int) -> bool:
        if from_idx < 0 or from_idx >= self.pages.getCount() or to_idx < 0 or to_idx >= self.pages.getCount():
            return False
        self.moved = (from_idx, to_idx)
        return not self.fail

    def delete_slide(self, index: int) -> None:
        self.deleted = index
        self.pages.count -= 1

    def get_active_page_index(self) -> int:
        return self.active_page

    def set_current_page_index(self, index: int) -> None:
        self.active_page = index

    def insert_slide_from_master(self, **kwargs: object) -> tuple[object, int]:
        return SimpleNamespace(), 0


def _engine(current: int, pages: int) -> tuple[Any, _Pages, _Bridge]:
    deck = _Pages(pages)
    bridge = _Bridge(deck)
    bridge.active_page = current
    eng: Any = SlideCommandEngine.__new__(SlideCommandEngine)
    eng.doc = SimpleNamespace()
    eng.tctx = SimpleNamespace(doc=eng.doc, active_page_index=current, ctx=SimpleNamespace())
    eng.applied = []
    eng.warnings = []
    eng.current_slide = current
    eng.pages = deck
    eng.bridge = bridge
    eng._selection_mode = "none"
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


@pytest.mark.parametrize(
    "value, value_2",
    [
        pytest.param(2, 2, id="test_delete_current_slide_selects_previous"),
        pytest.param(1, 3, id="test_delete_after_current_keeps_index"),
    ],
)
def test_delete_current_slide_selects_previous(value, value_2) -> None:
    eng, _deck, _bridge = _engine(value, 4)
    eng._delete_slide(value_2)
    assert eng.current_slide == 1

def test_delete_past_end_clamps_current() -> None:
    # Index 99 is out of range: warn instead of deleting the last slide.
    eng, _deck, bridge = _engine(3, 4)
    eng._delete_slide(99)
    assert bridge.deleted is None
    assert "Invalid DeleteSlide: 99 (no such slide)" in eng.warnings
    assert eng.current_slide == 3


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


def _dispatch_engine() -> tuple[Any, list[str], _Controller]:
    calls: list[str] = []
    controller = _Controller()

    class _Doc:
        def getCurrentController(self) -> _Controller:
            return controller

    class _Smgr:
        def createInstanceWithContext(self, name: str, ctx: object) -> _Dispatcher:
            assert name == "com.sun.star.frame.DispatchHelper"
            return _Dispatcher(calls)

    eng: Any = SlideCommandEngine.__new__(SlideCommandEngine)
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

def test_insert_master_not_found_raises_warning() -> None:
    from plugin.draw.transform_engine import SlideCommandEngine
    from unittest.mock import MagicMock

    class MockBridge:
        def get_pages(self) -> Any:
            m = MagicMock()
            m.getCount.return_value = 1
            return m

        def insert_slide_from_master(self, **kwargs: object) -> tuple[object, int]:
            raise ValueError("Master page not found")

    engine: Any = SlideCommandEngine.__new__(SlideCommandEngine)
    engine.bridge = MockBridge()
    engine.pages = engine.bridge.get_pages()
    engine.applied = []
    engine.warnings = []
    engine.current_slide = 0

    engine._apply_command({"InsertMasterSlide": 99})
    assert "InsertMasterSlide: Master page not found" in engine.warnings
    assert not any("InsertMasterSlide" in item for item in engine.applied)


def test_move_slide_rollback_error_propagates_from_engine() -> None:
    from plugin.draw.bridge import PageExchangeRollbackError

    eng, _deck, bridge = _engine(0, 2)

    def _boom(from_idx: int, to_idx: int) -> bool:
        raise PageExchangeRollbackError("temporary page")

    setattr(bridge, "move_slide", _boom)
    with pytest.raises(PageExchangeRollbackError):
        eng._move_slide(0, 1)
    assert eng.current_slide == 0
    assert eng.applied == []


class _MultiParaCursor:
    def __init__(self, paragraphs: list[str]) -> None:
        self.paras = list(paragraphs)
        self.pos_p = 0
        self.pos_c = 0
        self.anchor_p = 0
        self.anchor_c = 0
        self.props: dict[str, object] = {}

    def gotoStart(self, expand: bool) -> None:
        self.pos_p = 0
        self.pos_c = 0
        if not expand:
            self.anchor_p = 0
            self.anchor_c = 0

    def gotoEnd(self, expand: bool) -> None:
        self.pos_p = len(self.paras) - 1
        self.pos_c = len(self.paras[self.pos_p])
        if not expand:
            self.anchor_p = self.pos_p
            self.anchor_c = self.pos_c

    def gotoStartOfParagraph(self, expand: bool) -> None:
        self.pos_c = 0
        if not expand:
            self.anchor_p = self.pos_p
            self.anchor_c = 0

    def gotoEndOfParagraph(self, expand: bool) -> None:
        self.pos_c = len(self.paras[self.pos_p])
        if not expand:
            self.anchor_p = self.pos_p
            self.anchor_c = self.pos_c

    def gotoNextParagraph(self, expand: bool) -> bool:
        if self.pos_p + 1 < len(self.paras):
            self.pos_p += 1
            self.pos_c = 0
            if not expand:
                self.anchor_p = self.pos_p
                self.anchor_c = 0
            return True
        return False

    def goRight(self, n: int, expand: bool) -> None:
        self.pos_c += n
        if not expand:
            self.anchor_p = self.pos_p
            self.anchor_c = self.pos_c

    def getString(self) -> str:
        if self.anchor_p == self.pos_p:
            start = min(self.anchor_c, self.pos_c)
            end = max(self.anchor_c, self.pos_c)
            return self.paras[self.pos_p][start:end]
        if self.anchor_p < self.pos_p:
            p_start, c_start = self.anchor_p, self.anchor_c
            p_end, c_end = self.pos_p, self.pos_c
        else:
            p_start, c_start = self.pos_p, self.pos_c
            p_end, c_end = self.anchor_p, self.anchor_c
        parts = [self.paras[p_start][c_start:]]
        for mid in range(p_start + 1, p_end):
            parts.append(self.paras[mid])
        parts.append(self.paras[p_end][:c_end])
        return "\n".join(parts)

    def setPropertyValue(self, name: str, value: object) -> None:
        self.props[name] = value


def test_set_text_object_index_and_getText_requirement() -> None:
    class _Obj0:
        def __init__(self) -> None:
            self.text = ""
        def getString(self) -> str:
            return self.text
        def setString(self, val: str) -> None:
            self.text = val

    class _Obj1Text:
        def __init__(self) -> None:
            self.text = ""
            self.fail = False
        def setString(self, val: str) -> None:
            if self.fail:
                raise RuntimeError("write failed")
            self.text = val

    class _Obj1:
        def __init__(self) -> None:
            self.text_obj = _Obj1Text()
        def getText(self) -> _Obj1Text:
            return self.text_obj

    obj0 = _Obj0()
    obj1 = _Obj1()
    shapes = [obj0, obj1]

    class _PageWithShapes:
        def getCount(self) -> int:
            return len(shapes)
        def getByIndex(self, idx: int) -> object:
            return shapes[idx]

    deck = _Pages(1)
    bridge = _Bridge(deck)
    eng: Any = SlideCommandEngine.__new__(SlideCommandEngine)
    eng.applied = []
    eng.warnings = []
    eng.current_slide = 0
    eng.pages = SimpleNamespace(getCount=lambda: 1, getByIndex=lambda idx: _PageWithShapes())
    eng.bridge = bridge
    eng._selection_mode = "none"

    eng._set_text_index(0, "attempt on obj0")
    assert obj0.text == ""
    assert "SetText.0" not in eng.applied
    assert any("no callable getText" in w for w in eng.warnings)

    eng._set_text_index(1, "hello obj1")
    assert obj1.text_obj.text == "hello obj1"
    assert "SetText.1" in eng.applied

    obj1.text_obj.fail = True
    eng.applied.clear()
    eng._set_text_index(1, "failing write")
    assert eng.applied == []


def test_select_text_forms_and_caret_bold() -> None:
    eng, calls, controller = _dispatch_engine()
    eng.applied = []
    paragraphs = ["first paragraph", "second paragraph", "third paragraph"]

    # [] selects whole text
    cursor = _MultiParaCursor(paragraphs)
    eng._select_text(SimpleNamespace(), cursor, [])
    assert cursor.getString() == "first paragraph\nsecond paragraph\nthird paragraph"
    assert eng._selection_mode == "all"

    # [para] selects that paragraph
    cursor = _MultiParaCursor(paragraphs)
    eng._select_text(SimpleNamespace(), cursor, [1])
    assert cursor.getString() == "second paragraph"
    assert eng._selection_mode == "range"

    # [para, char] moves to caret and selects nothing
    cursor = _MultiParaCursor(paragraphs)
    eng._select_text(SimpleNamespace(), cursor, [0, 6])
    assert cursor.getString() == ""
    assert eng._selection_mode == "caret"

    # [para, char, endPara] selects from [para, char] through end of endPara
    cursor = _MultiParaCursor(paragraphs)
    eng._select_text(SimpleNamespace(), cursor, [0, 6, 1])
    assert cursor.getString() == "paragraph\nsecond paragraph"
    assert eng._selection_mode == "range"

    # [para, char, endPara, endChar] on same paragraph: absolute span (endChar - char)
    cursor = _MultiParaCursor(paragraphs)
    eng._select_text(SimpleNamespace(), cursor, [0, 6, 0, 15])
    assert cursor.getString() == "paragraph"
    assert eng._selection_mode == "range"

    # [para, char, endPara, endChar] across paragraphs
    cursor = _MultiParaCursor(paragraphs)
    eng._select_text(SimpleNamespace(), cursor, [0, 6, 1, 6])
    assert cursor.getString() == "paragraph\nsecond"
    assert eng._selection_mode == "range"

    # Caret plus .uno:Bold does not dispatch and does not set CharWeight
    cursor = _MultiParaCursor(paragraphs)
    eng._select_text(SimpleNamespace(), cursor, [0, 6])
    shape = _Shape("first paragraph\nsecond paragraph\nthird paragraph")
    res = eng._dispatch_uno_string(".uno:Bold", cursor=cursor, shape=shape, selection_mode=eng._selection_mode)
    assert res is False
    assert calls == []
    assert controller.selected == []
    assert "CharWeight" not in cursor.props
    assert any("caret" in w for w in eng.warnings)


def test_failed_insert_master_stops_sequence() -> None:
    eng, _deck, bridge = _engine(0, 1)
    text_called: list[str] = []

    class _MockPage:
        def getCount(self) -> int:
            return 1
        def getByIndex(self, idx: int) -> object:
            return SimpleNamespace(getText=lambda: SimpleNamespace(setString=lambda t: text_called.append(t)))

    eng.pages = SimpleNamespace(getCount=lambda: 1, getByIndex=lambda idx: _MockPage())

    def _boom_insert(**kwargs: object) -> tuple[object, int]:
        raise ValueError("Master not found")

    setattr(bridge, "insert_slide_from_master", _boom_insert)
    payload = {
        "Transforms": {
            "SlideCommands": [
                {"InsertMasterSlide": 99},
                {"SetText.0": "Should not write"},
            ]
        }
    }
    res = eng.apply(payload)
    assert res["status"] == "error"
    assert res["failed_at"] == 0
    assert text_called == []
    assert eng.applied == []


def test_jump_to_slide_by_name_miss_stops_sequence() -> None:
    eng, _deck, _bridge = _engine(0, 2)
    payload = {
        "Transforms": {
            "SlideCommands": [
                {"JumpToSlideByName": "NonExistent"},
                {"SetText.0": "Should not run"},
            ]
        }
    }
    res = eng.apply(payload)
    assert res["status"] == "error"
    assert res["failed_at"] == 0
    assert "JumpToSlideByName: slide not found" in res["message"]


def test_move_slide_stops_on_invalid_or_out_of_range() -> None:
    eng, _deck, _bridge = _engine(0, 2)
    res_bool = eng.apply({"Transforms": {"SlideCommands": [{"MoveSlide": True}, {"SetText.0": "no"}]}})
    assert res_bool["status"] == "error"
    assert res_bool["failed_at"] == 0

    eng, _deck, _bridge = _engine(0, 2)
    res_neg = eng.apply({"Transforms": {"SlideCommands": [{"MoveSlide": -1}, {"SetText.0": "no"}]}})
    assert res_neg["status"] == "error"
    assert res_neg["failed_at"] == 0

    eng, _deck, _bridge = _engine(0, 2)
    res_past = eng.apply({"Transforms": {"SlideCommands": [{"MoveSlide": 99}, {"SetText.0": "no"}]}})
    assert res_past["status"] == "error"
    assert res_past["failed_at"] == 0


def test_change_layout_invalid_id_does_not_set_layout() -> None:
    eng, _deck, _bridge = _engine(0, 1)
    page = SimpleNamespace(Layout=0)
    eng.pages = SimpleNamespace(getCount=lambda: 1, getByIndex=lambda idx: page)

    res_neg = eng.apply({"Transforms": {"SlideCommands": [{"ChangeLayout": -1}]}})
    assert res_neg["status"] == "error"
    assert page.Layout == 0

    res_999 = eng.apply({"Transforms": {"SlideCommands": [{"ChangeLayout": 999}]}})
    assert res_999["status"] == "error"
    assert page.Layout == 0


def test_jump_to_slide_bool_does_not_land_on_slide_one() -> None:
    eng, _deck, _bridge = _engine(0, 4)
    res = eng.apply({"Transforms": {"SlideCommands": [{"JumpToSlide": True}]}})
    assert res["status"] == "ok"
    assert eng.current_slide == 0
    assert any("Invalid JumpToSlide" in w for w in eng.warnings)


def test_construction_clamps_active_page_index() -> None:
    deck = _Pages(3)
    bridge = _Bridge(deck)
    tctx = SimpleNamespace(
        doc=SimpleNamespace(getDrawPages=lambda: deck, getCurrentController=lambda: None),
        active_page_index=99,
        ctx=SimpleNamespace(ServiceManager=None),
    )
    eng: Any = SlideCommandEngine(tctx)  # type: ignore
    eng.pages = deck
    eng.bridge = bridge
    assert eng.current_slide == 2

    eng._apply_command({"MoveSlide": 0})
    assert bridge.moved == (2, 0)


def test_top_level_uno_updates_current_slide_before_commands() -> None:
    deck = _Pages(4)
    bridge = _Bridge(deck)
    bridge.active_page = 0
    tctx = SimpleNamespace(
        doc=SimpleNamespace(getDrawPages=lambda: deck, getCurrentController=lambda: None),
        active_page_index=0,
        ctx=SimpleNamespace(ServiceManager=None),
    )
    eng: Any = SlideCommandEngine(tctx)  # type: ignore
    eng.pages = deck
    eng.bridge = bridge

    def _mock_top_uno(spec: object) -> None:
        bridge.active_page = 3

    setattr(eng, "_apply_top_level_uno", _mock_top_uno)
    res = eng.apply(
        {
            "UnoCommand": ".uno:GoToSlide4",
            "Transforms": {
                "SlideCommands": [{"MoveSlide": 0}],
            },
        }
    )
    assert res["status"] == "ok"
    assert bridge.moved == (3, 0)

