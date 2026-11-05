# WriterAgent — unit tests for Draw/Impress page comment tools
# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest

from plugin.draw.base import DRAW_AND_IMPRESS, IMPRESS_DOCUMENT_SERVICE, ToolDrawCommentBase
from plugin.draw.comments import AddPageComment, DeletePageComment, ListPageComments
from plugin.framework.errors import DocumentDisposedError


class _DisposedException(Exception):
    pass


class _FakePoint:
    def __init__(self, x: float, y: float):
        self.X = x
        self.Y = y


class _XText:
    def __init__(self, value: str = ""):
        self.value = value

    def getString(self):
        return self.value

    def setString(self, value: str):
        self.value = value


class _FakeAnnotation:
    def __init__(
        self,
        text: str = "",
        *,
        author: str = "",
        initials: str = "",
        date: Any = None,
        x: float | None = None,
        y: float | None = None,
    ):
        self.TextRange = _XText(text)
        self.Author = author
        self.Initials = initials
        self.DateTime = date
        self.Position = _FakePoint(x, y) if x is not None and y is not None else None


class _FakeEnum:
    def __init__(self, items: list[Any]):
        self._items = list(items)
        self._pos = 0

    def hasMoreElements(self) -> bool:
        return self._pos < len(self._items)

    def nextElement(self) -> Any:
        if self._pos >= len(self._items):
            raise IndexError("NoSuchElementException")
        item = self._items[self._pos]
        self._pos += 1
        return item


class _MockUndoManager:
    def __init__(self):
        self.entered = False
        self.left = False
        self.titles: list[str] = []
        self.current_title: str | None = None

    def isLocked(self) -> bool:
        return False

    def enterUndoContext(self, title: str) -> None:
        self.entered = True
        self.current_title = title

    def leaveUndoContext(self) -> None:
        self.left = True
        if self.current_title is not None:
            self.titles.insert(0, self.current_title)
            self.current_title = None

    def getAllUndoActionTitles(self):
        return tuple(self.titles)

    def undo(self) -> None:
        if self.titles:
            self.titles.pop(0)


class _FakePage:
    def __init__(self, annotations: list[_FakeAnnotation] | None = None, *, supports: bool = True):
        self._anns = list(annotations or [])
        self._supports = supports
        self._hide_new_in_enum = False

    def createAnnotationEnumeration(self):
        if not self._supports:
            raise AttributeError("no annotations")
        if not self._anns:
            return None
        if self._hide_new_in_enum:
            return _FakeEnum([])
        return _FakeEnum(self._anns)

    def createAndInsertAnnotation(self) -> _FakeAnnotation:
        ann = _FakeAnnotation()
        self._anns.append(ann)
        return ann

    def removeAnnotation(self, ann: Any) -> None:
        self._anns.remove(ann)


class _BrokenEnumPage(_FakePage):
    def createAnnotationEnumeration(self):
        raise _DisposedException("page disposed")


class _PageContainer:
    def __init__(self, pages: list[Any]):
        self._pages = pages

    def getCount(self) -> int:
        return len(self._pages)

    def getByIndex(self, index: int) -> Any:
        return self._pages[index]


class _FakeDoc:
    def __init__(self, pages: list[Any], *, undo: _MockUndoManager | None = None):
        self._pages = _PageContainer(pages)
        self._undo = undo if undo is not None else _MockUndoManager()

    def getUndoManager(self):
        return self._undo


class _FakeContext:
    def __init__(self, doc: Any, active_page_index: int | None = None):
        self.doc = doc
        self.active_page_index = active_page_index


def _ctx(doc: Any, active_page_index: int | None = None) -> _FakeContext:
    return _FakeContext(doc, active_page_index)


def test_list_empty_when_enumeration_is_none():
    page = _FakePage([])
    doc = _FakeDoc([page])
    ctx = _ctx(doc)
    with patch("plugin.draw.comments.DrawBridge") as bridge_cls:
        bridge = bridge_cls.return_value
        bridge.get_pages.return_value = doc._pages
        bridge.resolve_page.return_value = (page, 0)
        res = ListPageComments().execute(ctx, page=0)
    assert res["status"] == "ok"
    assert res["comments"] == []
    assert res["total"] == 0
    assert res["truncated"] is False
    assert "hint" not in res


def test_list_reads_fields():
    struct_date = type("DT", (), {"Year": 2026, "Month": 11, "Day": 5, "Hours": 9, "Minutes": 4})()
    ann = _FakeAnnotation(
        "body",
        author="Alice",
        initials="AB",
        date=struct_date,
        x=10.5,
        y=20.0,
    )
    ann2 = _FakeAnnotation("keep", date="already formatted")
    page = _FakePage([ann, ann2])
    doc = _FakeDoc([page])
    ctx = _ctx(doc)
    with patch("plugin.draw.comments.DrawBridge") as bridge_cls:
        bridge_cls.return_value.resolve_page.return_value = (page, 0)
        res = ListPageComments().execute(ctx, page=0)
    assert res["count"] == 2
    row = res["comments"][0]
    assert row["date"] == "2026-11-05 09:04"
    assert res["comments"][1]["date"] == "already formatted"
    assert row["text"] == "body"
    assert row["author"] == "Alice"
    assert row["initials"] == "AB"
    assert row["index"] == 0
    assert row["x"] == 10.5
    assert row["y"] == 20.0


def test_list_all_pages_orders_by_page_then_index():
    p0 = _FakePage([_FakeAnnotation("a0"), _FakeAnnotation("a1")])
    p1 = _FakePage([_FakeAnnotation("b0")])
    doc = _FakeDoc([p0, p1])
    ctx = _ctx(doc)
    with patch("plugin.draw.comments.DrawBridge") as bridge_cls:
        bridge = bridge_cls.return_value
        bridge.get_pages.return_value = doc._pages
        res = ListPageComments().execute(ctx)
    texts = [(c["page"], c["index"], c["text"]) for c in res["comments"]]
    assert texts == [(0, 0, "a0"), (0, 1, "a1"), (1, 0, "b0")]


def test_list_one_page_uses_resolve_page():
    p0 = _FakePage([_FakeAnnotation("only")])
    p1 = _FakePage([_FakeAnnotation("other")])
    doc = _FakeDoc([p0, p1])
    ctx = _ctx(doc)
    with patch("plugin.draw.comments.DrawBridge") as bridge_cls:
        bridge = bridge_cls.return_value
        bridge.resolve_page.return_value = (p0, 0)
        ListPageComments().execute(ctx, page=2)
        bridge.resolve_page.assert_called_once_with(2, active_page_index=ctx.active_page_index)
        bridge.get_pages.assert_not_called()


def test_list_cap_hint_and_offset():
    anns = [_FakeAnnotation(str(i)) for i in range(101)]
    page = _FakePage(anns)
    doc = _FakeDoc([page])
    ctx = _ctx(doc)
    with patch("plugin.draw.comments.DrawBridge") as bridge_cls:
        bridge_cls.return_value.resolve_page.return_value = (page, 0)
        first = ListPageComments().execute(ctx, page=0)
    assert first["count"] == 100
    assert first["total"] == 101
    assert first["truncated"] is True
    assert "offset=100" in first["hint"]

    with patch("plugin.draw.comments.DrawBridge") as bridge_cls:
        bridge_cls.return_value.resolve_page.return_value = (page, 0)
        second = ListPageComments().execute(ctx, page=0, offset=100)
    assert second["count"] == 1
    assert second["truncated"] is False
    assert "hint" not in second

    with patch("plugin.draw.comments.DrawBridge") as bridge_cls:
        bridge_cls.return_value.resolve_page.return_value = (page, 0)
        past = ListPageComments().execute(ctx, page=0, offset=200)
    assert past["comments"] == []
    assert past["truncated"] is False
    assert "hint" not in past


def test_list_rejects_bool_offset():
    ctx = _ctx(_FakeDoc([_FakePage()]))
    res = ListPageComments().execute(ctx, offset=True)
    assert res["status"] == "error"
    assert res["code"] == "INVALID_PARAM"


def test_missing_interface():
    page = object()
    doc = _FakeDoc([page])
    ctx = _ctx(doc)
    with patch("plugin.draw.comments.DrawBridge") as bridge_cls:
        bridge_cls.return_value.resolve_page.return_value = (page, 3)
        res = ListPageComments().execute(ctx, page=3)
    assert res["code"] == "ANNOTATIONS_UNAVAILABLE"
    assert "3" in res["message"]


def test_add_sets_text_author_and_index():
    page = _FakePage([])
    undo = _MockUndoManager()
    doc = _FakeDoc([page], undo=undo)
    ctx = _ctx(doc)
    with patch("plugin.draw.comments.DrawBridge") as bridge_cls:
        bridge_cls.return_value.resolve_page.return_value = (page, 0)
        res = AddPageComment().execute(ctx, text="one\r\ntwo", page=0)
    assert res["status"] == "ok"
    assert res["text"] == "one\ntwo"
    assert res["index"] == 0
    assert undo.entered is True
    assert undo.titles == ["WriterAgent: Page comment"]
    assert page._anns[0].TextRange.getString() == "one\ntwo"

    with patch("plugin.draw.comments.DrawBridge") as bridge_cls:
        bridge_cls.return_value.resolve_page.return_value = (page, 0)
        AddPageComment().execute(ctx, text="x", author="   ", page=0)
    assert page._anns[1].Author == ""

    with patch("plugin.draw.comments.DrawBridge") as bridge_cls:
        bridge_cls.return_value.resolve_page.return_value = (page, 0)
        named = AddPageComment().execute(ctx, text="signed", author="Agent", page=0)
    assert named["author"] == "Agent"
    assert page._anns[2].Author == "Agent"


def test_add_rejects_blank_text():
    page = _FakePage([])
    doc = _FakeDoc([page])
    ctx = _ctx(doc)
    with patch("plugin.draw.comments.DrawBridge") as bridge_cls:
        bridge_cls.return_value.resolve_page.return_value = (page, 0)
        res = AddPageComment().execute(ctx, text="  ", page=0)
    assert res["code"] == "INVALID_PARAM"
    assert len(page._anns) == 0


def test_add_rolls_back_when_not_enumerated():
    page = _FakePage([])
    page._hide_new_in_enum = True
    undo = _MockUndoManager()
    doc = _FakeDoc([page], undo=undo)
    ctx = _ctx(doc)
    with patch("plugin.draw.comments.DrawBridge") as bridge_cls:
        bridge_cls.return_value.resolve_page.return_value = (page, 0)
        res = AddPageComment().execute(ctx, text="ghost", page=0)
    assert res["code"] == "ANNOTATION_NOT_LISTED"
    assert undo.left is True


def test_add_undo_unavailable():
    page = _FakePage([])
    doc = _FakeDoc([page])
    ctx = _ctx(doc)
    doc.getUndoManager = lambda: None  # type: ignore[method-assign]
    with patch("plugin.draw.comments.DrawBridge") as bridge_cls:
        bridge_cls.return_value.resolve_page.return_value = (page, 0)
        res = AddPageComment().execute(ctx, text="nope", page=0)
    assert res["code"] == "UNDO_UNAVAILABLE"
    assert len(page._anns) == 0


def test_delete_by_index():
    a0 = _FakeAnnotation("keep")
    a1 = _FakeAnnotation("go")
    page = _FakePage([a0, a1])
    doc = _FakeDoc([page])
    ctx = _ctx(doc)
    with patch("plugin.draw.comments.DrawBridge") as bridge_cls:
        bridge_cls.return_value.resolve_page.return_value = (page, 0)
        res = DeletePageComment().execute(ctx, index=1, page=0)
    assert res["status"] == "ok"
    assert [ann.TextRange.getString() for ann in page._anns] == ["keep"]


def test_delete_missing_index():
    page = _FakePage([_FakeAnnotation("only")])
    doc = _FakeDoc([page])
    ctx = _ctx(doc)
    with patch("plugin.draw.comments.DrawBridge") as bridge_cls:
        bridge_cls.return_value.resolve_page.return_value = (page, 0)
        res = DeletePageComment().execute(ctx, index=5, page=0)
    assert res["code"] == "COMMENT_NOT_FOUND"
    assert "page_comment_list" in res["details"]["hint"]


def test_delete_rejects_bool_index():
    ctx = _ctx(_FakeDoc([_FakePage()]))
    res = DeletePageComment().execute(ctx, index=True)
    assert res["code"] == "INVALID_PARAM"


def test_disposal_reraises():
    page = _BrokenEnumPage([])
    doc = _FakeDoc([page])
    ctx = _ctx(doc)
    with patch("plugin.draw.comments.DrawBridge") as bridge_cls:
        bridge_cls.return_value.resolve_page.return_value = (page, 0)
        with pytest.raises(DocumentDisposedError):
            ListPageComments().execute(ctx, page=0)


def test_domain_services():
    import plugin.draw.comments as comments_mod

    names = {AddPageComment.name, DeletePageComment.name, ListPageComments.name}
    assert names == {"page_comment_add", "page_comment_delete", "page_comment_list"}
    for cls in (AddPageComment, DeletePageComment, ListPageComments):
        assert issubclass(cls, ToolDrawCommentBase)
        assert cls.uno_services == DRAW_AND_IMPRESS
    assert IMPRESS_DOCUMENT_SERVICE in DRAW_AND_IMPRESS
    assert comments_mod.ListPageComments.specialized_domain == "page_comments"
