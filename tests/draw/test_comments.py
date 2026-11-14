# WriterAgent — unit tests for Draw/Impress page comment tools
# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

from contextlib import contextmanager
from typing import Any, Iterator
from unittest.mock import patch

import pytest

from plugin.draw.comments import (
    _ANNOTATION_ACCESS_SVC,
    AddPageComment,
    DeletePageComment,
    ListPageComments,
)
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

    def isLocked(self) -> bool:
        return False

    def enterUndoContext(self, title: str) -> None:
        self.entered = True
        self._current_title = title

    def leaveUndoContext(self) -> None:
        self.left = True
        if hasattr(self, "_current_title"):
            self.titles.insert(0, self._current_title)
            del self._current_title

    def getAllUndoActionTitles(self):
        return tuple(self.titles)

    def undo(self) -> None:
        if self.titles:
            self.titles.pop(0)


class _NoAnnotationPage:
    """Page object with neither the service nor XAnnotationAccess methods."""

    def supportsService(self, name: str) -> bool:
        return False


class _FakePage:
    def __init__(
        self,
        annotations: list[_FakeAnnotation] | None = None,
        *,
        annotation_access: bool = True,
    ):
        self._anns = list(annotations or [])
        self._annotation_access = annotation_access
        self._hide_new_in_enum = False

    def supportsService(self, name: str) -> bool:
        return name == _ANNOTATION_ACCESS_SVC and self._annotation_access

    def createAnnotationEnumeration(self):
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


@contextmanager
def _bridge(
    *,
    page: _FakePage | None = None,
    pages: _PageContainer | None = None,
    resolve: tuple[Any, int] | None = None,
) -> Iterator[Any]:
    with patch("plugin.draw.comments.DrawBridge") as bridge_cls:
        bridge = bridge_cls.return_value
        if page is not None:
            bridge.resolve_page.return_value = resolve or (page, 0)
        if pages is not None:
            bridge.get_pages.return_value = pages
        yield bridge


def test_list_empty_when_enumeration_is_none():
    page = _FakePage([])
    doc = _FakeDoc([page])
    ctx = _FakeContext(doc)
    with _bridge(page=page, pages=doc._pages):
        res = ListPageComments().execute(ctx, page=0)
    assert res == {
        "status": "ok",
        "comments": [],
        "count": 0,
        "total": 0,
        "truncated": False,
        "offset": 0,
        "page": 0,
    }


def test_list_reads_fields_and_single_page_resolution():
    struct_date = type("DT", (), {"Year": 2026, "Month": 11, "Day": 5, "Hours": 9, "Minutes": 4})()
    page = _FakePage(
        [
            _FakeAnnotation("body", author="Alice", initials="AB", date=struct_date, x=10.5, y=20.0),
            _FakeAnnotation("keep", date="already formatted"),
        ]
    )
    ctx = _FakeContext(_FakeDoc([page]))
    with _bridge(page=page, resolve=(page, 0)) as bridge:
        res = ListPageComments().execute(ctx, page=2)
        bridge.resolve_page.assert_called_once_with(2, active_page_index=ctx.active_page_index)
    row = res["comments"][0]
    assert row["date"] == "2026-11-05 09:04"
    assert res["comments"][1]["date"] == "already formatted"
    assert row["text"] == "body" and row["author"] == "Alice" and row["index"] == 0
    assert row["x"] == 10.5 and row["y"] == 20.0


def test_list_all_pages_orders_by_page_then_index():
    doc = _FakeDoc(
        [
            _FakePage([_FakeAnnotation("a0"), _FakeAnnotation("a1")]),
            _FakePage([_FakeAnnotation("b0")]),
        ]
    )
    ctx = _FakeContext(doc)
    with _bridge(pages=doc._pages):
        res = ListPageComments().execute(ctx)
    assert [(c["page"], c["index"], c["text"]) for c in res["comments"]] == [
        (0, 0, "a0"),
        (0, 1, "a1"),
        (1, 0, "b0"),
    ]


def test_list_cap_hint_and_offset():
    page = _FakePage([_FakeAnnotation(str(i)) for i in range(101)])
    ctx = _FakeContext(_FakeDoc([page]))
    with _bridge(page=page):
        first = ListPageComments().execute(ctx, page=0)
    assert first["count"] == 100 and first["total"] == 101 and first["truncated"] is True
    assert "offset=100" in first["hint"]

    with _bridge(page=page):
        second = ListPageComments().execute(ctx, page=0, offset=100)
    assert second["count"] == 1 and second["truncated"] is False and "hint" not in second

    with _bridge(page=page):
        past = ListPageComments().execute(ctx, page=0, offset=200)
    assert past["comments"] == [] and past["truncated"] is False


@pytest.mark.parametrize(
    ("tool", "kwargs", "code"),
    [
        (ListPageComments(), {"offset": True}, "INVALID_PARAM"),
        (DeletePageComment(), {"index": True}, "INVALID_PARAM"),
        (AddPageComment(), {"text": "  ", "page": 0}, "INVALID_PARAM"),
    ],
)
def test_validation_rejects_bad_params(tool, kwargs, code):
    page = _FakePage([])
    ctx = _FakeContext(_FakeDoc([page]))
    with _bridge(page=page):
        res = tool.execute(ctx, **kwargs)
    assert res["code"] == code


def test_missing_annotation_access_service():
    page = _NoAnnotationPage()
    ctx = _FakeContext(_FakeDoc([page]))
    with _bridge(page=page, resolve=(page, 3)):
        res = ListPageComments().execute(ctx, page=3)
    assert res["code"] == "ANNOTATIONS_UNAVAILABLE"


def test_add_sets_text_author_undo_and_index():
    page = _FakePage([])
    undo = _MockUndoManager()
    ctx = _FakeContext(_FakeDoc([page], undo=undo))
    with _bridge(page=page):
        res = AddPageComment().execute(ctx, text="one\r\ntwo", page=0)
    assert res["status"] == "ok" and res["index"] == 0 and res["text"] == "one\ntwo"
    assert len(undo.titles) == 1
    with _bridge(page=page):
        AddPageComment().execute(ctx, text="signed", author="Agent", page=0)
    assert page._anns[1].Author == "Agent"


def test_add_rolls_back_when_not_enumerated():
    page = _FakePage([])
    page._hide_new_in_enum = True
    undo = _MockUndoManager()
    ctx = _FakeContext(_FakeDoc([page], undo=undo))
    with _bridge(page=page):
        res = AddPageComment().execute(ctx, text="ghost", page=0)
    assert res["code"] == "ANNOTATION_NOT_LISTED" and undo.left is True


def test_add_undo_unavailable():
    page = _FakePage([])
    doc = _FakeDoc([page])
    doc.getUndoManager = lambda: None  # type: ignore[method-assign]
    ctx = _FakeContext(doc)
    with _bridge(page=page):
        res = AddPageComment().execute(ctx, text="nope", page=0)
    assert res["code"] == "UNDO_UNAVAILABLE" and len(page._anns) == 0


def test_delete_by_index_and_missing_index():
    page = _FakePage([_FakeAnnotation("keep"), _FakeAnnotation("go")])
    ctx = _FakeContext(_FakeDoc([page]))
    with _bridge(page=page):
        ok = DeletePageComment().execute(ctx, index=1, page=0)
        miss = DeletePageComment().execute(ctx, index=5, page=0)
    assert ok["status"] == "ok"
    assert [ann.TextRange.getString() for ann in page._anns] == ["keep"]
    assert miss["code"] == "COMMENT_NOT_FOUND"
    assert "page_comment_list" in miss["details"]["hint"]


def test_disposal_reraises():
    page = _BrokenEnumPage([])
    ctx = _FakeContext(_FakeDoc([page]))
    with _bridge(page=page):
        with pytest.raises(DocumentDisposedError):
            ListPageComments().execute(ctx, page=0)
