# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Build and inspect live Writer documents in UNO tests.

Every function takes a real Writer document (``@with_native_doc("writer")``). The document
pool, tool contexts and fakes live in ``tests/testing_utils.py``.
"""

from __future__ import annotations

from typing import Any

from plugin.framework.uno_context import enum_has_more, execute_dispatch

# com.sun.star.text.ControlCharacter.PARAGRAPH_BREAK
_PARAGRAPH_BREAK = 0


def reset_body(doc: Any, ctx: Any, *paragraphs: str) -> None:
    """Stop recording, replace the body with *paragraphs*, then accept the redlines still listed."""
    text = doc.getText()
    doc.setPropertyValue("RecordChanges", False)
    cursor = text.createTextCursor()
    cursor.gotoStart(False)
    cursor.gotoEnd(True)
    cursor.setString("")
    cursor.gotoStart(False)
    for i, paragraph in enumerate(paragraphs):
        if i:
            text.insertControlCharacter(cursor, _PARAGRAPH_BREAK, False)
        text.insertString(cursor, paragraph, False)
    if doc.getRedlines().getCount():
        accept_all_changes(doc, ctx)


def insert_named_table(
    doc: Any, name: str, rows: int, cols: int, cells: dict[str, str] | None = None
) -> Any:
    """Append a text table to the body and return it. *cells* maps cell name to text."""
    text = doc.getText()
    table = doc.createInstance("com.sun.star.text.TextTable")
    table.initialize(rows, cols)
    text.insertTextContent(text.getEnd(), table, False)
    table.setName(name)
    for cell_name, value in (cells or {}).items():
        table.getCellByName(cell_name).setString(value)
    return table


def accept_all_changes(doc: Any, ctx: Any) -> None:
    """Accept every tracked change through the command the user runs."""
    execute_dispatch(ctx, doc.getCurrentController(), ".uno:AcceptAllTrackedChanges")


def reject_all_changes(doc: Any, ctx: Any) -> None:
    """Reject every tracked change through the command the user runs."""
    execute_dispatch(ctx, doc.getCurrentController(), ".uno:RejectAllTrackedChanges")


def list_redlines(doc: Any) -> list[dict[str, str]]:
    """Return ``type``, ``RedlineComment`` and ``RedlineAuthor`` per redline, in enumeration order."""
    out: list[dict[str, str]] = []
    enum = doc.getRedlines().createEnumeration()
    while enum_has_more(enum):
        redline = enum.nextElement()
        entry = {"type": str(redline.getPropertyValue("RedlineType"))}
        for prop in ("RedlineComment", "RedlineAuthor"):
            entry[prop] = str(redline.getPropertyValue(prop))
        out.append(entry)
    return out


def list_redline_types(doc: Any) -> list[str]:
    """Return the ``RedlineType`` of each redline, in enumeration order."""
    return [redline["type"] for redline in list_redlines(doc)]
