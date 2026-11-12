# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""TOC outline fixture with 40 entries for eval-1 benchmark tasks."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable


TOC_FIXTURE_ID = "toc_outline_40"


@dataclass(frozen=True)
class FixtureRow:
    title: str
    level: int
    url: str
    page: int


def _generate_fixture_rows() -> list[FixtureRow]:
    rows: list[FixtureRow] = []
    for ch in range(1, 9):
        ch_title = f"Chapter {ch} Title"
        rows.append(FixtureRow(
            title=ch_title,
            level=1,
            url=f"#{ch}.{ch_title}|outline",
            page=1 if ch == 1 else 2,
        ))
        for s in range(1, 5):
            sec_title = f"Section {ch}.{s} Topic"
            rows.append(FixtureRow(
                title=sec_title,
                level=2,
                url=f"#{ch}.{s}.{sec_title}|outline",
                page=2,
            ))
    return rows


FIXTURE_ROWS: list[FixtureRow] = _generate_fixture_rows()

EDIT_RENAMES: list[tuple[str, str]] = [
    ("Chapter 5 Title", "Chapter 6 Title"),
    ("Section 5.1 Topic", "Section 6.1 Topic"),
    ("Section 5.2 Topic", "Section 6.2 Topic"),
    ("Section 5.3 Topic", "Section 6.3 Topic"),
    ("Section 5.4 Topic", "Section 6.4 Topic"),
    ("Chapter 4 Title", "Chapter 5 Title"),
    ("Section 4.1 Topic", "Section 5.1 Topic"),
    ("Section 4.2 Topic", "Section 5.2 Topic"),
    ("Section 4.3 Topic", "Section 5.3 Topic"),
    ("Section 4.4 Topic", "Section 5.4 Topic"),
]

EDIT_INSERT: tuple[str, str] = ("Chapter 4 Interim Review", "Section 3.4 Topic")


def expected_rows_after_edit() -> list[str]:
    """Expected 41 visible TOC entry titles after the renumber and insert edits."""
    titles: list[str] = []
    # Chapters 1..3 untouched (15 rows)
    for ch in range(1, 4):
        titles.append(f"Chapter {ch} Title")
        for s in range(1, 5):
            titles.append(f"Section {ch}.{s} Topic")
    # Inserted row right after Section 3.4 Topic
    titles.append(EDIT_INSERT[0])
    # Old Chapter 4 renumbered to Chapter 5 (5 rows)
    titles.append("Chapter 5 Title")
    for s in range(1, 5):
        titles.append(f"Section 5.{s} Topic")
    # Old Chapter 5 renumbered to Chapter 6 (5 rows)
    titles.append("Chapter 6 Title")
    for s in range(1, 5):
        titles.append(f"Section 6.{s} Topic")
    # Chapters 6..8 untouched (15 rows)
    for ch in range(6, 9):
        titles.append(f"Chapter {ch} Title")
        for s in range(1, 5):
            titles.append(f"Section {ch}.{s} Topic")
    return titles


def _add_heading(text: Any, title: str, first: bool, level: int) -> None:
    cursor = text.createTextCursor()
    cursor.gotoEnd(False)
    if not first:
        text.insertControlCharacter(cursor, 0, False)
        cursor.gotoEnd(False)
    text.insertString(cursor, title, False)
    sel = text.createTextCursorByRange(cursor.getEnd())
    sel.goLeft(len(title), True)
    sel.setPropertyValue("ParaStyleName", f"Heading {level}")

    cursor = text.createTextCursor()
    cursor.gotoEnd(False)
    text.insertControlCharacter(cursor, 0, False)
    body_text = f"Body text under {title}."
    text.insertString(cursor, body_text, False)
    sel = text.createTextCursorByRange(cursor.getEnd())
    sel.goLeft(len(body_text), True)
    sel.setPropertyValue("ParaStyleName", "Standard")


def build_toc_fixture(doc: Any, execute_tool: Callable[[str, dict[str, Any]], Any]) -> None:
    """Build the 40-heading document and painted TOC on the LO thread."""
    text = doc.getText()
    text.setString("")
    first = True
    for ch in range(1, 9):
        _add_heading(text, f"Chapter {ch} Title", first, 1)
        first = False
        for s in range(1, 5):
            _add_heading(text, f"Section {ch}.{s} Topic", False, 2)

    execute_tool("indexes_create", {"kind": "toc", "title": "Contents", "target": "beginning"})

    indexes = doc.getDocumentIndexes()
    toc = indexes.getByIndex(0)
    was_protected = bool(toc.getPropertyValue("IsProtected"))
    toc.setPropertyValue("IsProtected", False)

    enum = text.createEnumeration()
    toc_paras: list[Any] = []
    while enum.hasMoreElements():
        p = enum.nextElement()
        try:
            style = p.getPropertyValue("ParaStyleName") or ""
        except Exception:
            continue
        if style.startswith("Contents ") and style != "Contents Heading":
            toc_paras.append(p)

    assert len(toc_paras) == len(FIXTURE_ROWS), (
        f"Expected {len(FIXTURE_ROWS)} TOC paragraphs, got {len(toc_paras)}"
    )

    painted = 0
    for para, row in zip(toc_paras, FIXTURE_ROWS):
        cur = text.createTextCursorByRange(para.getStart())
        cur.gotoRange(para.getEnd(), True)
        cur.setPropertyValue("HyperLinkURL", row.url)
        painted += 1

    toc.setPropertyValue("IsProtected", was_protected)
    assert painted == 40, f"Expected 40 painted rows, got {painted}"
