# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for Writer nested-table discovery, guards, and host-cell paragraph read/write."""
from types import SimpleNamespace
from typing import Any

import pytest

from plugin.framework.errors import ToolExecutionError
from plugin.writer.table_nesting import (
    _cell_hosted_table_names,
    cell_plain_siblings,
    raise_if_range_hosts_nested_table,
    set_host_paragraphs,
)


class _Enum:
    def __init__(self, items: list[Any]):
        self._items = list(items)
        self._index = 0

    def hasMoreElements(self) -> bool:
        return self._index < len(self._items)

    def nextElement(self) -> Any:
        if self._index >= len(self._items):
            raise IndexError("enumeration exhausted")
        item = self._items[self._index]
        self._index += 1
        return item


class _Portion:
    def __init__(self, **props: Any):
        self._props = props

    def getPropertyValue(self, key: str) -> Any:
        if key not in self._props:
            raise RuntimeError(key)
        return self._props[key]

    def getString(self) -> str:
        return str(self._props.get("Text", "") or "")


class _Paragraph:
    def __init__(self, text: str = "", portions: list[Any] | None = None):
        self._text = text
        self._portions = list(portions) if portions is not None else None

    def supportsService(self, name: str) -> bool:
        return name == "com.sun.star.text.Paragraph"

    def getString(self) -> str:
        return self._text

    def setString(self, text: str) -> None:
        self._text = text
        self._portions = None

    def createEnumeration(self) -> _Enum:
        if self._portions is not None:
            return _Enum(self._portions)
        if self._text:
            return _Enum([_Portion(TextPortionType="Text", Text=self._text)])
        return _Enum([])


class _Table:
    def __init__(self, name: str = ""):
        self._name = name

    def supportsService(self, name: str) -> bool:
        return name == "com.sun.star.text.TextTable"

    def getName(self) -> str:
        return self._name


class _Frame:
    def __init__(self, elements: list[Any]):
        self._elements = list(elements)

    def supportsService(self, name: str) -> bool:
        return name == "com.sun.star.text.TextFrame"

    def getText(self) -> Any:
        return SimpleNamespace(createEnumeration=lambda: _Enum(self._elements))


class _Cell:
    def __init__(self, elements: list[Any] | None = None):
        self._elements = list(elements or [])

    def createEnumeration(self) -> _Enum:
        return _Enum(self._elements)

    def getStart(self) -> Any:
        return object()

    def insertString(self, _cursor: Any, text: str, _absorb: bool = False) -> None:
        self._elements.insert(0, _Paragraph(text))


def test_host_read_hides_pending_tracked_deletion() -> None:
    para = _Paragraph(
        "novavelha",
        portions=[
            _Portion(TextPortionType="Text", Text="nova"),
            _Portion(TextPortionType="Redline", RedlineType="Delete"),
            _Portion(TextPortionType="Text", Text="velha"),
            _Portion(TextPortionType="Redline", RedlineType="Delete"),
        ],
    )
    cell = _Cell([para])
    assert para.getString() == "novavelha"
    assert cell_plain_siblings(cell) == "nova"
    assert cell_plain_siblings(cell) != "novavelha"


def test_host_read_skips_frame_text() -> None:
    cell = _Cell([_Paragraph("CAPTION"), _Table("Child"), _Frame([_Paragraph("FRAME")])])
    res = cell_plain_siblings(cell)
    assert res == "CAPTION"
    assert "FRAME" not in res


def test_set_host_paragraphs_writes_only_direct_paragraphs() -> None:
    p1 = _Paragraph("first")
    p2 = _Paragraph("second")
    frame_p = _Paragraph("frame text")
    frame = _Frame([frame_p])
    table = _Table("Nested")
    cell = _Cell([p1, p2, frame, table])

    set_host_paragraphs(cell, "x")

    assert p1.getString() == "x"
    assert p2.getString() == ""
    assert frame_p.getString() == "frame text"
    assert table in cell._elements


def test_host_set_then_get_round_trip_ignores_frame_text() -> None:
    frame_p = _Paragraph("frame text")
    frame = _Frame([frame_p])
    table = _Table("Nested")
    cell = _Cell([frame, table])

    assert cell_plain_siblings(cell) == ""
    set_host_paragraphs(cell, "cap")
    assert cell_plain_siblings(cell) == "cap"
    assert frame_p.getString() == "frame text"


def test_host_read_drops_blank_paragraphs() -> None:
    cell = _Cell([_Paragraph(""), _Paragraph("A"), _Paragraph("")])
    assert cell_plain_siblings(cell) == "A"


def test_nested_table_discovery_follows_frame_portions() -> None:
    frame = _Frame([_Table("Child")])
    cell = _Cell([
        _Table("Direct"),
        _Paragraph("", portions=[_Portion(TextContent=None, TextFrame=frame)]),
    ])
    assert _cell_hosted_table_names(cell) == ["Direct", "Child"]


def test_guard_message_does_not_name_a_tool_that_may_not_call_it() -> None:
    child_tbl = _Table("Child")
    cell = _Cell([child_tbl])
    setattr(
        cell,
        "createTextCursorByRange",
        lambda _start: SimpleNamespace(
            getPropertyValue=lambda k: object() if k == "TextTable" else None
        ),
    )
    range_fake = SimpleNamespace(getText=lambda: cell, getStart=lambda: object())

    with pytest.raises(ToolExecutionError) as exc_info:
        raise_if_range_hosts_nested_table(range_fake)

    msg = str(exc_info.value)
    assert "Child" in msg
    assert "table_set_cell" in msg
    assert "apply_document_content" not in msg
