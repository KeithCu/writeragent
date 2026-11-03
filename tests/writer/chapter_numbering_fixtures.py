# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Reusable Chapter Numbering fixtures for Writer UNO tests (discussion #876).

Not product API. Use from ``test_*_uno.py`` to turn Tools→Chapter Numbering
ON/OFF and seed Heading 1/2/3 trees with stable ``ListLabelString`` values.

Authoritative visible label on outline headings: paragraph property
``ListLabelString`` (same property list tests use for ``<ol>`` markers).
``getString()`` / tree ``text`` never include the label.

Enable/disable MUST use ``uno.invoke(... replaceByIndex ...,
uno.Any("[]com.sun.star.beans.PropertyValue", level))`` — bare
``rules.replaceByIndex`` raises ``IllegalArgumentException`` (classic pyuno
NumberingRules typing issue).

Do **not** use these helpers inside ``test_numbering_lists_uno.py`` — that
suite deliberately keeps Chapter Numbering OFF (see
``docs/writer/list-numbering.md``).
"""

from __future__ import annotations

import uno

# com.sun.star.style.NumberingType
NUMBERING_ARABIC = 4
NUMBERING_NONE = 5

# Stable in-process fixture (no committed .fodt). After enable_chapter_numbering
# with suffix="", ListLabelString values are:
#   Chapter One -> "1"
#   Section Alpha -> "1.1"
#   Subsection One -> "1.1.1"
#   Section Beta -> "1.2"
#   Chapter Two -> "2"
#   Section Gamma -> "2.1"
CHAPTER_HEADING_FIXTURE = (
    ("Heading 1", "Chapter One"),
    ("Heading 2", "Section Alpha"),
    ("Heading 3", "Subsection One"),
    ("Heading 2", "Section Beta"),
    ("Heading 1", "Chapter Two"),
    ("Heading 2", "Section Gamma"),
)

# Expected labels when Chapter Numbering is ON with Suffix="" (discussion #876
# style: "3.1" not "3.1."). With Suffix="." every label gains a trailing ".".
EXPECTED_LABELS_SUFFIX_EMPTY = {
    "Chapter One": "1",
    "Section Alpha": "1.1",
    "Subsection One": "1.1.1",
    "Section Beta": "1.2",
    "Chapter Two": "2",
    "Section Gamma": "2.1",
}


def replace_chapter_numbering_level(rules, index: int, **updates) -> None:
    """Write one ChapterNumberingRules level (typed Any; see module doc)."""
    level = rules.getByIndex(index)
    for prop in level:
        if prop.Name in updates:
            prop.Value = updates[prop.Name]
    uno.invoke(
        rules,
        "replaceByIndex",
        (index, uno.Any("[]com.sun.star.beans.PropertyValue", level)),
    )


def enable_chapter_numbering(
    doc, levels: int = 3, suffix: str = "", start_with: int | None = None
) -> None:
    """Turn Tools→Chapter Numbering ON for Heading 1..N.

    Keeps LO default ``ParentNumbering=10`` (include all upper levels) so
    labels are hierarchical. ``suffix=""`` → ``1`` / ``1.1`` / ``1.1.1``;
    ``suffix="."`` → ``1.`` / ``1.1.`` / ``1.1.1.``.

    ``start_with`` (optional) sets level-0 ``StartWith`` so the first Heading 1
    is e.g. ``3`` and its child is ``3.1`` (discussion #876 example shape).
    """
    rules = doc.getChapterNumberingRules()
    for i in range(levels):
        updates = {
            "NumberingType": NUMBERING_ARABIC,
            "Suffix": suffix,
            "Prefix": "",
        }
        # Always write StartWith — leftover Writer reuse keeps the last
        # value (e.g. 3) across tests / sequential enable() calls.
        if i == 0:
            updates["StartWith"] = int(start_with) if start_with is not None else 1
        replace_chapter_numbering_level(rules, i, **updates)


def disable_chapter_numbering(doc) -> None:
    """Turn Tools→Chapter Numbering OFF for all 10 outline levels.

    Clears Suffix/Prefix as well — leaving Suffix="." with NumberingType=NONE
    makes ``ListLabelString`` return ``"."`` instead of empty.
    """
    rules = doc.getChapterNumberingRules()
    for i in range(rules.getCount()):
        replace_chapter_numbering_level(
            rules, i, NumberingType=NUMBERING_NONE, Suffix="", Prefix=""
        )


def insert_chapter_heading_fixture(doc, rows=CHAPTER_HEADING_FIXTURE) -> None:
    """Replace body with Heading 1/2/3 rows (ParaStyleName). Clears doc first."""
    text = doc.getText()
    text.setString("")
    cursor = text.createTextCursor()
    for style, title in rows:
        text.insertString(cursor, title, False)
        cursor.setPropertyValue("ParaStyleName", style)
        text.insertControlCharacter(cursor, 0, False)  # PARAGRAPH_BREAK


def iter_heading_label_snaps(doc):
    """Yield dicts for non-empty paragraphs (text + outline/list label props)."""
    enum = doc.getText().createEnumeration()
    while enum.hasMoreElements():
        el = enum.nextElement()
        try:
            if not el.supportsService("com.sun.star.text.Paragraph"):
                continue
        except Exception:
            continue
        text = str(el.getString() or "")
        if not text.strip():
            continue
        yield {
            "text": text,
            "ParaStyleName": str(el.getPropertyValue("ParaStyleName") or ""),
            "OutlineLevel": el.getPropertyValue("OutlineLevel"),
            "NumberingStyleName": str(el.getPropertyValue("NumberingStyleName") or ""),
            "NumberingLevel": el.getPropertyValue("NumberingLevel"),
            "ListLabelString": str(el.getPropertyValue("ListLabelString") or ""),
            "NumberingIsNumber": bool(el.getPropertyValue("NumberingIsNumber")),
        }


def chapter_number_from_para(el) -> str | None:
    """Read-only v1 helper: return label or None when Chapter Numbering is off.

    Product field name planned: ``chapter_number``. Omit key when this returns
    None (empty ``ListLabelString``).
    """
    try:
        label = str(el.getPropertyValue("ListLabelString") or "")
    except Exception:
        return None
    return label if label else None
