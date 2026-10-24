# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Nested-table discovery, mutation guards, and host-cell paragraph read/write helpers for Writer documents."""
from typing import Any, Iterator

from plugin.framework.errors import uno_probe

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
    """Inner XText of a TextFrame or TextSection, else None.

    A table inside one of these still lives in the host cell — setString on
    the cell would destroy it — so discovery walks through the container.
    """
    if not (
        _service_named(obj, "com.sun.star.text.TextFrame")
        or _service_named(obj, "com.sun.star.text.TextSection")
    ):
        return None
    return uno_probe(lambda: obj.getText() if hasattr(obj, "getText") else obj, obj)


def _safe_enumerate(obj: Any) -> Iterator[Any]:
    """Yield elements from an object's XEnumeration, re-raising disposal."""
    enum = uno_probe(lambda: obj.createEnumeration(), None)
    if enum is None:
        return
    # Portion walks must use hasMoreElements() is True; bare "if not enum.hasMoreElements()"
    # never stops on MagicMock (AGENTS.md). A failed read ends the walk like the end does.
    while (element := uno_probe(lambda: enum.nextElement() if enum.hasMoreElements() is True else _FAILED, _FAILED)) is not _FAILED:
        yield element


def _iter_direct_children(xtext: Any) -> Iterator[Any]:
    """Yield each element of *xtext*'s XEnumeration (one level, no recurse)."""
    return _safe_enumerate(xtext)


def _iter_text_frames_in_para(para: Any) -> Iterator[Any]:
    """As-character TextFrames in *para* (portion property ``TextFrame``).

    Probed: a frame in a cell is not an XEnumeration sibling — the cell enum
    is only Paragraph; the frame hangs off a portion.
    """
    for portion in _safe_enumerate(para):
        frame = uno_probe(lambda: portion.getPropertyValue("TextFrame"), None)
        if frame is not None:
            yield frame


def _walk_xtext_siblings(xtext: Any, *, into_containers: bool = True) -> Iterator[tuple[str, Any]]:
    """Yield ``('table', obj)`` or ``('para', obj)`` from *xtext*.

    When *into_containers* is true (cell / body), follow TextFrame /
    TextSection siblings and as-character ``TextFrame`` portions. Once
    inside a container, leave it false so the frame's own portions do not
    re-enter the same frame (UNO hands back a new proxy each time).
    """
    for element in _iter_direct_children(xtext):
        if _is_text_table(element):
            yield ("table", element)
            continue
        if into_containers:
            inner = _container_xtext(element)
            if inner is not None:
                yield from _walk_xtext_siblings(inner, into_containers=False)
                continue
            for frame in _iter_text_frames_in_para(element):
                nested = _container_xtext(frame)
                if nested is not None:
                    yield from _walk_xtext_siblings(nested, into_containers=False)
        yield ("para", element)


def _cell_hosted_table_names(cell: Any) -> list[str]:
    """Names of TextTables hosted in *cell* (direct or via frame/section).

    Writer nests a TextTable as a sibling of the cell's paragraphs — not via
    anchors or geometry. Empty if the cell cannot be enumerated (plain fakes,
    covered/merged cells). Used by set/delete guards so they scan one cell,
    not the whole document.
    """
    names: list[str] = []
    for kind, obj in _walk_xtext_siblings(cell):
        if kind != "table":
            continue
        nested_name = uno_probe(lambda: str(obj.getName() or ""), "")
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
    from plugin.framework.errors import ToolExecutionError

    raise ToolExecutionError(
        "This range is in a table cell that contains nested table(s) %s. "
        "Use table_set_cell for the host cell's own paragraphs, or "
        "edit those tables by name — apply_document_content would delete them."
        % ", ".join(hosted)
    )


def cell_plain_siblings(cell: Any) -> str:
    """Host-cell paragraph text only — skips nested tables' getString() dump."""
    parts: list[str] = []
    for kind, obj in _walk_xtext_siblings(cell):
        if kind != "para":
            continue
        part = uno_probe(lambda: obj.getString(), "")
        if part:
            parts.append(str(part))
    return "\n".join(parts)


def set_host_paragraphs(cell: Any, text: str) -> None:
    """Rewrite the cell's own paragraph siblings; leave tables and frames.

    Direct children only — do not rewrite text inside a frame/section (that
    would be a different XText). No paragraphs: insert at getStart() so a
    caption lands before a table that table_insert placed at getEnd().
    """
    paras: list[Any] = []
    for element in _iter_direct_children(cell):
        if _is_text_table(element) or _container_xtext(element) is not None:
            continue
        paras.append(element)
    if not paras:
        cell.insertString(cell.getStart(), text, False)
        return
    paras[0].setString(text)
    for extra in paras[1:]:
        extra.setString("")
