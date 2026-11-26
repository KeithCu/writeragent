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

from plugin.doc.text_helpers import iter_elements
from plugin.framework.uno_context import enum_has_more, execute_dispatch

# com.sun.star.text.ControlCharacter.PARAGRAPH_BREAK
_PARAGRAPH_BREAK = 0
_PARAGRAPH = "com.sun.star.text.Paragraph"
_TEXT_TABLE = "com.sun.star.text.TextTable"


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


def set_body_text(doc: Any, text_value: str) -> None:
    """Replace the whole Writer body with *text_value* (``\n`` starts a new paragraph)."""
    text = doc.getText()
    cur = text.createTextCursor()
    cur.gotoStart(False)
    cur.gotoEnd(True)
    cur.setString("")
    cur.gotoStart(False)
    text.insertString(cur, text_value, False)


def find_first(doc: Any, needle: str) -> Any:
    """Return the first range matching *needle* (plain search), or ``None``."""
    sd = doc.createSearchDescriptor()
    sd.setSearchString(needle)
    return doc.findFirst(sd)


def body_paragraphs(text: Any) -> list[Any]:
    """Return the paragraphs directly inside *text* (body, header or cell), skipping tables."""
    return [el for el in iter_elements(text.createEnumeration()) if el.supportsService(_PARAGRAPH)]


def body_texts(text: Any) -> list[str]:
    """Return the string of each paragraph directly inside *text*, skipping tables."""
    return [para.getString() for para in body_paragraphs(text)]


def body_shape(text: Any) -> list[tuple[str, str]]:
    """Return ``(ParaStyleName, text)`` per paragraph and ``("TABLE", "")`` per table in *text*."""
    out: list[tuple[str, str]] = []
    for el in iter_elements(text.createEnumeration()):
        if el.supportsService(_TEXT_TABLE):
            out.append(("TABLE", ""))
        else:
            out.append((str(el.getPropertyValue("ParaStyleName")), el.getString()))
    return out


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


def build_chain_deletion_doc(doc: Any, text: Any) -> None:
    """Fill *text* with ``Keep GONE Line 1`` / ``Line OLD 2 match More`` and track-delete ``GONE `` and ``OLD ``.

    *text* is the body or a cell. The visible text is ``Keep Line 1`` / ``Line 2 match More``, so a
    needle spanning both paragraphs (``Line 1`` then ``Line 2 match``) only matches if search skips struck text.
    """
    text.setString("")
    cur = text.createTextCursor()
    text.insertString(cur, "Keep GONE Line 1", False)
    text.insertControlCharacter(cur, _PARAGRAPH_BREAK, False)
    text.insertString(cur, "Line OLD 2 match More", False)
    paras = body_paragraphs(text)
    doc.setPropertyValue("RecordChanges", True)
    try:
        for para, keep, gone in ((paras[0], "Keep ", "GONE "), (paras[1], "Line ", "OLD ")):
            cursor = text.createTextCursorByRange(para.getStart())
            cursor.goRight(len(keep), False)
            cursor.goRight(len(gone), True)
            cursor.setString("")
    finally:
        doc.setPropertyValue("RecordChanges", False)


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


def strike_substring(
    doc: Any, xtext: Any, needle: str, *, ctx: Any = None, author: str | None = None
) -> None:
    """Strike the first occurrence of *needle* in *xtext* as a tracked deletion."""
    from plugin.writer import review_authors

    cur = xtext.createTextCursor()
    cur.gotoStart(False)
    idx = xtext.getString().index(needle)
    cur.goRight(idx, False)
    cur.goRight(len(needle), True)
    prior = None
    doc.setPropertyValue("RecordChanges", True)
    try:
        if author is not None:
            assert ctx is not None
            prior = review_authors.begin(ctx)
            assert review_authors.set_office_author(ctx, author) is True
        cur.setString("")
    finally:
        doc.setPropertyValue("RecordChanges", False)
        if author is not None:
            review_authors.end(ctx, prior)
