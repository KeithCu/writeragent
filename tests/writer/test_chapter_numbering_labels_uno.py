# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Chapter Numbering → ListLabelString lock (discussion #876 R&D).

Confirms the authoritative visible heading label and the enable/disable UNO
sequence. Does **not** implement ``chapter_number`` on ``writer_tree``.
"""

from __future__ import annotations

import uno  # noqa: F401

from plugin.testing_runner import native_test
from plugin.tests.testing_utils import with_native_doc
from tests.writer.chapter_numbering_fixtures import (
    EXPECTED_LABELS_SUFFIX_EMPTY,
    chapter_number_from_para,
    disable_chapter_numbering,
    enable_chapter_numbering,
    insert_chapter_heading_fixture,
    iter_heading_label_snaps,
)


@native_test
@with_native_doc("writer")
def test_chapter_numbering_listlabel_on_off_uno(ctx, doc):
    insert_chapter_heading_fixture(doc)

    disable_chapter_numbering(doc)
    off = list(iter_heading_label_snaps(doc))
    headings = [s for s in off if s["OutlineLevel"] > 0]
    assert headings, off
    for snap in headings:
        assert snap["ListLabelString"] == "", snap
        assert snap["NumberingStyleName"] == "Outline", snap  # style linked; label empty
        assert snap["OutlineLevel"] >= 1

    enable_chapter_numbering(doc, suffix="")
    on = {s["text"]: s for s in iter_heading_label_snaps(doc)}
    for title, expected in EXPECTED_LABELS_SUFFIX_EMPTY.items():
        assert on[title]["ListLabelString"] == expected, (title, on[title])

    enable_chapter_numbering(doc, suffix=".")
    on_dot = {s["text"]: s["ListLabelString"] for s in iter_heading_label_snaps(doc)}
    assert on_dot["Section Alpha"] == "1.1.", on_dot

    disable_chapter_numbering(doc)
    off2 = list(iter_heading_label_snaps(doc))
    assert all(s["ListLabelString"] == "" for s in off2 if s["OutlineLevel"] > 0), off2


@native_test
@with_native_doc("writer")
def test_chapter_number_from_para_omit_when_off_uno(ctx, doc):
    insert_chapter_heading_fixture(doc)
    disable_chapter_numbering(doc)
    enum = doc.getText().createEnumeration()
    el = enum.nextElement()
    assert chapter_number_from_para(el) is None

    enable_chapter_numbering(doc, suffix="")
    enum = doc.getText().createEnumeration()
    el = enum.nextElement()
    assert chapter_number_from_para(el) == "1"


@native_test
@with_native_doc("writer")
def test_chapter_numbering_start_with_three_uno(ctx, doc):
    """start_with=3 yields discussion-shaped 3 / 3.1 / 3.1.1 labels."""
    insert_chapter_heading_fixture(doc)
    enable_chapter_numbering(doc, suffix="", start_with=3)
    on = {s["text"]: s["ListLabelString"] for s in iter_heading_label_snaps(doc)}
    assert on["Chapter One"] == "3", on
    assert on["Section Alpha"] == "3.1", on
    assert on["Subsection One"] == "3.1.1", on
    assert on["Section Beta"] == "3.2", on
    assert on["Chapter Two"] == "4", on
