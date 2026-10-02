"""Unit tests for DrawBridge chat-context helper (no UNO)."""

import pytest
from unittest.mock import MagicMock, patch

from plugin.draw.bridge import get_draw_context_for_chat
from plugin.framework.errors import UnoObjectError


def test_get_draw_context_for_chat_disposed_returns_fallback():
    with patch("plugin.draw.bridge.check_disposed", side_effect=UnoObjectError("gone")):
        out = get_draw_context_for_chat(MagicMock())
    assert "Unable to read Draw/Impress context" in out


def test_get_draw_context_for_chat_summarizes_active_page():
    page = MagicMock()
    pages = MagicMock()
    pages.getCount.return_value = 1
    pages.getByIndex.return_value = page

    shape = MagicMock()
    shape.getShapeType.return_value = "com.sun.star.drawing.TextShape"
    pos = MagicMock(X=10, Y=20)
    size = MagicMock(Width=100, Height=50)
    shape.getPosition.return_value = pos
    shape.getSize.return_value = size
    shape.getString.return_value = "Hello"

    model = MagicMock()
    model.supportsService.return_value = False
    model.getURL.return_value = "file:///tmp/demo.odg"

    bridge = MagicMock()
    bridge.get_pages.return_value = pages
    bridge.get_active_page.return_value = page
    bridge.get_shapes.return_value = [shape]

    with (
        patch("plugin.draw.bridge.check_disposed"),
        patch("plugin.draw.bridge.DrawBridge", return_value=bridge),
        patch("plugin.draw.bridge.safe_call", side_effect=lambda fn, _msg, *args: fn(*args) if args else fn()),
    ):
        out = get_draw_context_for_chat(model, 8000)

    assert "Draw Document" in out
    assert "Total Pages: 1" in out
    assert "Hello" in out


class _FakeDrawPages:
    def __init__(self, n=2):
        self._n = n

    def getCount(self):
        return self._n

    def getByIndex(self, i):
        if i < 0 or i >= self._n:
            raise IndexError(i)
        return object()


def test_draw_bridge_uses_get_draw_pages_when_present():
    from plugin.draw.bridge import DrawBridge

    page_coll = _FakeDrawPages(3)

    class Doc:
        def getDrawPages(self):
            return page_coll

    bridge = DrawBridge(Doc())
    assert bridge.get_pages() is page_coll
    assert bridge.get_pages().getCount() == 3


def test_draw_bridge_writer_single_draw_page():
    from plugin.draw.bridge import DrawBridge

    page = object()

    class WriterDoc:
        def getDrawPage(self):
            return page

    bridge = DrawBridge(WriterDoc())
    pages = bridge.get_pages()
    assert pages.getCount() == 1
    assert pages.getByIndex(0) is page


def test_draw_bridge_calc_active_sheet_draw_page():
    from plugin.draw.bridge import DrawBridge

    page = object()

    class Sheet:
        def getDrawPage(self):
            return page

    class CalcDoc:
        def getSheets(self):
            return MagicMock()

        def getCurrentController(self):
            ctrl = MagicMock()
            ctrl.getActiveSheet.return_value = Sheet()
            return ctrl

    bridge = DrawBridge(CalcDoc())
    assert bridge.get_pages().getCount() == 1
    assert bridge.get_pages().getByIndex(0) is page


def test_draw_bridge_rejects_document_without_draw_page():
    from plugin.draw.bridge import DrawBridge

    class Empty:
        pass

    with pytest.raises(RuntimeError, match="no draw page"):
        DrawBridge(Empty())


def test_get_slide_for_tool_returns_resolved_slide():
    from plugin.draw.bridge import DrawBridge

    slide = object()
    with patch.object(DrawBridge, "resolve_slide", return_value=slide) as resolve:
        out = DrawBridge.get_slide_for_tool("doc", 1)
    assert out is slide
    resolve.assert_called_once_with("doc", 1)


def test_get_slide_for_tool_index_error_is_tool_error():
    from plugin.draw.bridge import DrawBridge
    from plugin.framework.errors import ToolExecutionError

    with patch.object(DrawBridge, "resolve_slide", side_effect=IndexError("Page index 9 out of range.")):
        with pytest.raises(ToolExecutionError, match="Page index 9 out of range"):
            DrawBridge.get_slide_for_tool("doc", 9)


def test_get_slide_for_tool_reraises_disposed():
    from plugin.draw.bridge import DrawBridge

    class DisposedException(Exception):
        pass

    boom = DisposedException("Binary URP bridge disposed during call")
    with patch.object(DrawBridge, "resolve_slide", side_effect=boom):
        with pytest.raises(DisposedException, match="URP"):
            DrawBridge.get_slide_for_tool("doc", 0)


def test_get_active_page_index_matches_via_uno_same():
    from plugin.draw.bridge import DrawBridge

    pages = MagicMock()
    pages.getCount.return_value = 2
    first, second = object(), object()
    pages.getByIndex.side_effect = lambda i: (first, second)[i]

    class Doc:
        def getDrawPages(self):
            return pages

    bridge = DrawBridge(Doc())
    with patch.object(bridge, "get_active_page", return_value=second):
        assert bridge.get_active_page_index() == 1


def test_get_slide_for_tool_wraps_other_errors():
    from plugin.draw.bridge import DrawBridge
    from plugin.framework.errors import ToolExecutionError

    with patch.object(DrawBridge, "resolve_slide", side_effect=RuntimeError("No draw page available.")):
        with pytest.raises(ToolExecutionError, match="No draw page available"):
            DrawBridge.get_slide_for_tool("doc")


class _MoveShape:
    def __init__(self, text: str) -> None:
        self._text = text
        self.ShapeType = "com.sun.star.drawing.RectangleShape"
        self.Name = ""
        self.payload: object = None
        self.from_duplicate = False

    def getShapeType(self) -> str:
        return self.ShapeType

    def getString(self) -> str:
        return self._text

    def setString(self, text: str) -> None:
        self._text = text

    def getPosition(self) -> object:
        return type("P", (), {"X": 1, "Y": 2})()

    def setPosition(self, pos: object) -> None:
        return None

    def getSize(self) -> object:
        return type("S", (), {"Width": 3, "Height": 4})()

    def setSize(self, size: object) -> None:
        return None

    def getPropertyValue(self, name: str) -> object:
        if name == "String":
            return self._text
        raise AttributeError(name)

    def setPropertyValue(self, name: str, value: object) -> None:
        if name == "String":
            self._text = str(value)


class _MovePage:
    def __init__(self, name: str, shapes: list[_MoveShape]) -> None:
        self.Name = name
        self.shapes = shapes

    def getCount(self) -> int:
        return len(self.shapes)

    def getByIndex(self, index: int) -> _MoveShape:
        return self.shapes[index]

    def add(self, shape: _MoveShape) -> None:
        self.shapes.append(shape)

    def remove(self, shape: _MoveShape) -> None:
        self.shapes.remove(shape)


class _MovePages:
    def __init__(self, pages: list[_MovePage]) -> None:
        self.pages = pages

    def getCount(self) -> int:
        return len(self.pages)

    def getByIndex(self, index: int) -> _MovePage:
        return self.pages[index]

    def insertNewByIndex(self, index: int) -> _MovePage:
        page = _MovePage("", [])
        if not self.pages:
            self.pages.append(page)
            return page
        # InsertSdPage inserts after min(count-1, nIndex), never at index 0.
        land = min(len(self.pages) - 1, index) + 1
        self.pages.insert(land, page)
        return page

    def remove(self, page: _MovePage) -> None:
        self.pages.remove(page)


class _MoveDoc:
    def __init__(self, pages: _MovePages) -> None:
        self._pages = pages

    def getDrawPages(self) -> _MovePages:
        return self._pages

    def createInstance(self, shape_type: str) -> _MoveShape:
        return _MoveShape("")

    def duplicate(self, page: _MovePage) -> _MovePage:
        """Same slot as ``SdXImpressDocument::duplicate``: the clone is inserted after *page*."""
        idx = self._pages.pages.index(page)
        clones: list[_MoveShape] = []
        for shape in page.shapes:
            clone = _MoveShape(shape.getString())
            clone.payload = getattr(shape, "payload", None)
            clone.from_duplicate = True
            clones.append(clone)
        copy = _MovePage("", clones)
        self._pages.pages.insert(idx + 1, copy)
        return copy


def test_move_slide_copies_then_removes_source():
    from plugin.draw.bridge import DrawBridge

    rich = _MoveShape("c")
    rich.payload = ("group", 2)
    pages = _MovePages(
        [
            _MovePage("A", [_MoveShape("a")]),
            _MovePage("B", [_MoveShape("b-text")]),
            _MovePage("C", [rich]),
        ]
    )
    bridge = DrawBridge(_MoveDoc(pages))
    assert bridge.move_slide(2, 0) is True
    assert [page.Name for page in pages.pages] == ["C", "A", "B"]
    moved = pages.pages[0].shapes[0]
    assert moved.getString() == "c"
    # The surviving shape is the duplicate's clone, not a createInstance shell.
    assert moved.from_duplicate is True
    assert moved.payload == ("group", 2)
    assert bridge.move_slide(0, 2) is True
    assert [page.Name for page in pages.pages] == ["A", "B", "C"]
    assert pages.pages[2].shapes[0].getString() == "c"
    assert pages.pages[2].shapes[0].payload == ("group", 2)
    assert bridge.move_slide(0, 0) is True
    assert bridge.move_slide(99, 99) is False
    assert [page.Name for page in pages.pages] == ["A", "B", "C"]


def test_move_slide_keeps_source_when_copy_fails():
    from plugin.draw.bridge import DrawBridge

    original = _MoveShape("a")
    pages = _MovePages(
        [
            _MovePage("A", [original]),
            _MovePage("B", [_MoveShape("b")]),
        ]
    )
    doc = _MoveDoc(pages)

    def _boom(page: _MovePage) -> _MovePage:
        raise RuntimeError("no duplicate")

    doc.duplicate = _boom  # type: ignore[method-assign]
    bridge = DrawBridge(doc)
    assert bridge.move_slide(0, 1) is False
    assert [page.Name for page in pages.pages] == ["A", "B"]
    assert pages.pages[0].shapes[0] is original


def test_move_slide_to_front_restores_order_when_reorder_fails():
    """A failed bubble must not leave the clone parked between other slides."""
    from plugin.draw.bridge import DrawBridge

    rich = _MoveShape("c")
    rich.payload = ("group", 2)
    pages = _MovePages(
        [
            _MovePage("A", [_MoveShape("a")]),
            _MovePage("B", [_MoveShape("b")]),
            _MovePage("C", [rich]),
        ]
    )
    bridge = DrawBridge(_MoveDoc(pages))
    calls = {"n": 0}
    real_exchange = bridge._exchange_page_contents

    def _flaky(first: object, second: object) -> None:
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("exchange failed")
        real_exchange(first, second)

    bridge._exchange_page_contents = _flaky  # type: ignore[method-assign]
    assert bridge.move_slide(2, 0) is False
    assert [page.Name for page in pages.pages] == ["A", "B", "C"]
    survivor = pages.pages[2].shapes[0]
    assert survivor.getString() == "c"
    assert survivor.payload == ("group", 2)
    assert survivor.from_duplicate is True
    assert len(pages.pages) == 3


def test_move_slide_restores_shapes_when_exchange_fails_midway():
    from plugin.draw.bridge import DrawBridge

    pages = _MovePages(
        [
            _MovePage("A", [_MoveShape("a")]),
            _MovePage("B", [_MoveShape("b")]),
            _MovePage("C", [_MoveShape("c")]),
        ]
    )
    bridge = DrawBridge(_MoveDoc(pages))
    calls = {"n": 0}
    real_move = bridge._move_shapes

    def _flaky(shapes: list[_MoveShape], src: _MovePage, dest: _MovePage) -> None:
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("shape move failed")
        real_move(shapes, src, dest)

    bridge._move_shapes = _flaky  # type: ignore[method-assign]
    assert bridge.move_slide(2, 0) is False
    assert [page.Name for page in pages.pages] == ["A", "B", "C"]
    assert [page.shapes[0].getString() for page in pages.pages] == ["a", "b", "c"]
    assert len(pages.pages) == 3
