# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Heading snapshot and outline-link retargeting after Writer edits."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from plugin.doc.text_helpers import DeletionState, iter_elements, visible_portions
from plugin.framework.errors import uno_read
from plugin.writer import range_copy, search as search_mod
from plugin.writer.hyperlink_fixup import heading_renames, retarget_outline_links
from plugin.writer.review_scan import document_has_tracked_deletions

if TYPE_CHECKING:
    from plugin.writer.edit_review import EditReviewSession


def document_headings(doc: Any) -> list[tuple[int, str]]:
    """Collect ``(level, text)`` headings from the body, table cells, and text frames (Word text boxes included).

    ``SwNodes::UpdateOutlineNode`` (sw/source/core/docnode/ndnum.cxx) adds any text node
    with ``OutlineLevel > 0`` to the outline list, and ``SwDoc::GotoOutline``
    (sw/source/core/doc/docnum.cxx) searches that list. The body-only ``build_heading_tree``
    misses headings inside tables and text frames. When *tracked* deletions exist, every
    paragraph is walked so a deletion's end marker in a non-heading paragraph updates state.
    """
    texts: list[Any] = []
    body_text = uno_read(lambda: doc.getText(), None)
    if body_text is not None:
        texts.append(body_text)
    frames = uno_read(lambda: doc.getTextFrames(), None)
    if frames is not None:
        frame_names: tuple[Any, ...] = uno_read(lambda: tuple(search_mod.iter_text_frame_names(doc)), ())
        for name in frame_names:
            frame_text = uno_read(lambda: frames.getByName(name).getText(), None)
            if frame_text is not None:
                texts.append(frame_text)

    tracked = document_has_tracked_deletions(doc)
    headings: list[tuple[int, str]] = []
    for text_obj in texts:
        state = DeletionState()
        for para in range_copy.iter_xtext_paragraphs(text_obj):
            level = uno_read(lambda: para.getPropertyValue("OutlineLevel"), 0)
            is_heading = isinstance(level, int) and level > 0
            if not is_heading and not tracked:
                continue
            text = uno_read(
                lambda: "".join(chunk for _portion, chunk in visible_portions(para, state=state)),
                "",
            )
            if is_heading and text:
                headings.append((level, text))
    return headings


def _touches_heading(text_range: Any) -> bool:
    """True when any paragraph intersecting *text_range* has ``OutlineLevel > 0``."""
    enum = uno_read(
        lambda: text_range.getText().createTextCursorByRange(text_range).createEnumeration(),
        None,
    )
    if enum is None:
        return False
    for para in iter_elements(enum):
        level = uno_read(lambda: para.getPropertyValue("OutlineLevel"), 0)
        if isinstance(level, int) and level > 0:
            return True
    return False


def headings_if_touched(doc: Any, ranges: list[Any]) -> list[tuple[int, str]] | None:
    """Snapshot document headings only when at least one edited range touches a heading."""
    if any(_touches_heading(r) for r in ranges):
        return document_headings(doc)
    return None


def retarget_renamed_links(
    session: EditReviewSession,
    doc: Any,
    before: list[tuple[int, str]] | None,
) -> tuple[int, list[str]]:
    """Retarget outline links when *before* headings were renamed, returning ``(count, stale)``."""
    if not before:
        return 0, []
    renames = heading_renames(before, document_headings(doc))
    if not renames:
        return 0, []
    return session.tag_side_effects(lambda: retarget_outline_links(doc, renames))
