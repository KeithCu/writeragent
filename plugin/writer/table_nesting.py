# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Nested-table discovery, mutation guards, and host-cell paragraph read/write helpers.

Host-cell contract: read and write use the same definition — the cell's direct
paragraph children only. Tracked deletions are hidden on read
(get_string_without_tracked_deletions), and frame text is not host text because
the write cannot clear it. Nested tables are still discovered through frame
portions. Callers run on the UI thread: the read helper is @main_thread_only.
"""
from typing import Any, Iterator

from plugin.doc.text_helpers import get_string_without_tracked_deletions
from plugin.framework.errors import ToolExecutionError, uno_probe

# uno_probe default for sites where None is a real UNO result.
_FAILED: Any = object()


def _service_named(obj: Any, name: str) -> bool:
    """True if *obj* supports *name*. Prefer supportsService; fall back to names."""

    def via_supports_service() -> bool | None:
        ss = getattr(obj, "supportsService", None)
        return bool(ss(name)) if callable(ss) else None

    found = uno_probe(via_supports_service, None)
    if found is not None:
        return found
    return uno_probe(lambda: name in (obj.getSupportedServiceNames() or ()), False)


def _is_text_table(obj: Any) -> bool:
    """True for a Writer TextTable."""
    return _service_named(obj, "com.sun.star.text.TextTable")


def _container_xtext(obj: Any) -> Any | None:
    """Inner XText of an as-character TextFrame, else None.

    A table inside a frame still lives in the host cell — setString on the
    cell would destroy it — so discovery walks into the frame. A TextSection
    is a range of the same XText, not a separate one, so it is not a container.
    """
    if not _service_named(obj, "com.sun.star.text.TextFrame"):
        return None
    return uno_probe(lambda: obj.getText(), None)


def _safe_enumerate(obj: Any) -> Iterator[Any]:
    """Yield elements from an object's XEnumeration, re-raising disposal."""
    enum = uno_probe(lambda: obj.createEnumeration(), None)
    if enum is None:
        return
    # Portion walks must use hasMoreElements() is True; bare "if not enum.hasMoreElements()"
    # never stops on MagicMock (AGENTS.md). A failed read ends the walk like the end does.
    while (element := uno_probe(lambda: enum.nextElement() if enum.hasMoreElements() is True else _FAILED, _FAILED)) is not _FAILED:
        yield element


def _iter_text_frames_in_para(para: Any) -> Iterator[Any]:
    """As-character TextFrames in *para* (portion property ``TextFrame``).

    Probed: a frame in a cell is not an XEnumeration sibling — the cell enum
    is only Paragraph; the frame hangs off a portion.
    """
    for portion in _safe_enumerate(para):
        frame = uno_probe(lambda: portion.getPropertyValue("TextFrame"), None)
        if frame is not None:
            yield frame


def _iter_nested_tables(xtext: Any, *, into_containers: bool = True) -> Iterator[Any]:
    """Yield TextTables in *xtext*, following frame containers when asked.

    When *into_containers* is true (cell / body), follow TextFrame siblings and
    as-character TextFrame portions. Once inside a container, leave it false so
    the frame's own portions do not re-enter the same frame (UNO hands back a
    new proxy each time).
    """
    for element in _safe_enumerate(xtext):
        if _is_text_table(element):
            yield element
            continue
        if not into_containers:
            continue
        inner = _container_xtext(element)
        if inner is not None:
            yield from _iter_nested_tables(inner, into_containers=False)
            continue
        for frame in _iter_text_frames_in_para(element):
            nested = _container_xtext(frame)
            if nested is not None:
                yield from _iter_nested_tables(nested, into_containers=False)


def _host_paragraphs(cell: Any) -> Iterator[Any]:
    """Direct paragraph children of a host cell (frame text is a different XText)."""
    for element in _safe_enumerate(cell):
        if not _is_text_table(element) and _service_named(element, "com.sun.star.text.Paragraph"):
            yield element


def _cell_hosted_table_names(cell: Any) -> list[str]:
    """Names of TextTables hosted in *cell* (direct or via frame/section).

    Writer nests a TextTable as a sibling of the cell's paragraphs — not via
    anchors or geometry. Empty if the cell cannot be enumerated (plain fakes,
    covered/merged cells). Used by set/delete guards so they scan one cell,
    not the whole document.
    """
    names: list[str] = []
    for table in _iter_nested_tables(cell):
        nested_name = uno_probe(lambda: str(table.getName() or ""), "")
        if nested_name:
            names.append(nested_name)
    return names


def range_hosted_nested_tables(text_range: Any) -> list[str]:
    """Nested table names that setString on *text_range* would destroy.

    Only when the range lives inside a table cell (cursor ``TextTable`` is
    set). Body XText tables are top-level — a body search-replace must not
    be treated as a host-cell wipe.
    """

    def host_text() -> Any:
        text_obj = text_range.getText()
        cur = text_obj.createTextCursorByRange(text_range.getStart())
        return _FAILED if cur.getPropertyValue("TextTable") is None else text_obj

    text_obj = uno_probe(host_text, _FAILED)
    return [] if text_obj is _FAILED else _cell_hosted_table_names(text_obj)


def raise_if_range_hosts_nested_table(text_range: Any) -> None:
    """Refuse a rewrite that would setString a host cell (wipes nested tables)."""
    hosted = range_hosted_nested_tables(text_range)
    if not hosted:
        return

    raise ToolExecutionError(
        "This range is in a table cell that contains nested table(s) %s. "
        "Use table_set_cell for the host cell's own paragraphs, or "
        "edit those tables by name."
        % ", ".join(hosted)
    )


def cell_plain_siblings(cell: Any) -> str:
    """Host-cell paragraph text; pending tracked deletions stay hidden."""
    parts = [get_string_without_tracked_deletions(para) for para in _host_paragraphs(cell)]
    return "\n".join(part for part in parts if part)


def set_host_paragraphs(cell: Any, text: str) -> None:
    """Rewrite the cell's own paragraph siblings; leave tables and frames.

    Direct paragraphs only — frame text is a different XText and is not host
    text, so it is neither read nor written. Blank paragraphs are dropped on
    read, so they are not preserved. No paragraphs: insert at getStart() so a
    caption lands before a table that table_insert placed at getEnd().
    """
    paras = list(_host_paragraphs(cell))
    if not paras:
        cell.insertString(cell.getStart(), text, False)
        return
    paras[0].setString(text)
    for extra in paras[1:]:
        extra.setString("")
