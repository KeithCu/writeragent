# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2024 John Balis
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.
"""Format helpers — document format conversions (markdown/HTML <-> UNO)."""

from __future__ import annotations

import logging
import re
from typing import Any

from plugin.framework.uno_context import create_property_value as create_property_value
from .math.html_math_segment import html_fragment_contains_mixed_math  # noqa: F401  # pyright: ignore[reportUnusedImport]

log = logging.getLogger("writeragent.writer")


# ---------------------------------------------------------------------------
# Format configuration
# ---------------------------------------------------------------------------

HTML_FILTER = "HTML (StarWriter)"

# Read-path export filter. The XHTML filter preserves the paragraph style model as CSS
# classes + a <style> block (vs HTML (StarWriter), which flattens everything to inline
# CSS). We post-process it into semantic data-lo-style attributes. The WRITE/import path
# keeps HTML (StarWriter).
XHTML_FILTER = "XHTML Writer File"
XHTML_EXTENSION = ".xhtml"
# Flat ODF: keeps each autostyle's parent named style (style:parent-style-name), which the
# XHTML export flattens away — so autostyle paragraphs (common after a StarWriter import) can
# recover their real style name. Paired with the XHTML export on the read path.
FLAT_ODF_FILTER = "OpenDocument Text Flat XML"
FODT_EXTENSION = ".fodt"

# NumberingType mapping for PageNumber text fields preserved across HTML/XHTML round-trips.
# Corresponds to com.sun.star.style.NumberingType constants.
PAGE_NUMBER_NUMBERING_TYPE_NAMES: dict[int, str] = {
    0: "CHARS_UPPER_LETTER",
    1: "CHARS_LOWER_LETTER",
    2: "ROMAN_UPPER",
    3: "ROMAN_LOWER",
    4: "ARABIC",
    5: "NUMBER_NONE",
    6: "CHAR_SPECIAL",
    7: "PAGE_DESCRIPTOR",
    8: "BITMAP",
    9: "CHARS_UPPER_LETTER_N",
    10: "CHARS_LOWER_LETTER_N",
    11: "TRANSLITERATION",
    12: "NATIVE_NUMBERING",
    13: "FULLWIDTH_ARABIC",
    14: "CIRCLE_NUMBER",
    64: "ARABIC_ZERO",
}

PAGE_NUMBER_NUMBERING_TYPE_VALS: dict[str, int] = {
    name: val for val, name in PAGE_NUMBER_NUMBERING_TYPE_NAMES.items()
}

# ---------------------------------------------------------------------------
# HTML helpers
# ---------------------------------------------------------------------------


def _strip_html_boilerplate(html_string: str) -> str:  # pyright: ignore[reportUnusedFunction]
    """Extract content between ``<body>`` tags if present."""
    if not html_string or not isinstance(html_string, str):
        return html_string
    match = re.search(r"<body[^>]*>(.*?)</body>", html_string, re.DOTALL | re.IGNORECASE)
    if match:
        return match.group(1).strip()
    return html_string


def walk_cap_warning(kind: str, seen: int, limit: int) -> str:
    """Agent-facing note when a text walk stopped at a hard cap.

    The walk still returns what it collected; anything after the cap was not read, so
    formatting (or a header scan) may be incomplete. DO: treat the result as partial and
    re-read a smaller range rather than assuming the rest is unformatted.
    """
    return (
        "Walk stopped after %d %s (cap %d). Later content was not read, so formatting "
        "may be incomplete. Read a smaller range (or split the target) rather than "
        "treating this result as the whole picture." % (seen, kind, limit)
    )


def record_walk_cap(enum: Any, seen: int, limit: int, kind: str, dest: list[str] | None = None) -> bool:
    """Log (and optionally collect) a walk-cap warning. Returns True when the cap was hit."""
    if seen < limit or enum is None or enum.hasMoreElements() is not True:
        return False
    msg = walk_cap_warning(kind, seen, limit)
    log.warning("%s", msg)
    if dest is not None:
        dest.append(msg)
    return True


def run_writer_mutation_with_optional_review(doc: Any, ctx: Any, apply_fn: Any) -> Any:
    """Run a Writer document mutation, optionally wrapped in EditReviewSession.

    LibrePy omits ``plugin.writer.edit_review``; when that module is absent we apply
    directly. Full WriterAgent (or co-install) imports it and honors review mode.
    """
    try:
        from plugin.writer.edit_review import EditReviewSession, review_recording_enabled
    except ImportError:
        return apply_fn()
    review = EditReviewSession(doc, ctx, enabled=review_recording_enabled())
    try:
        with review:
            return review.record_mutation(apply_fn)
    finally:
        review.cleanup()


# ---------------------------------------------------------------------------
# Re-exports from html_export and html_import.
# ---------------------------------------------------------------------------

from .html_export import (
    document_to_content as document_to_content,
)
from .html_import import (
    _content_has_block_markup as _content_has_block_markup,
    _ensure_html_linebreaks as _ensure_html_linebreaks,
    _insert_starwriter_html_at_cursor as _insert_starwriter_html_at_cursor,
    content_has_markup as content_has_markup,
    plain_text_if_markup as plain_text_if_markup,
    insert_content_at_position as insert_content_at_position,
    insert_html_at_cursor as insert_html_at_cursor,
    insert_inline_at_cursor as insert_inline_at_cursor,
    replace_full_document as replace_full_document,
    replace_preserving_format as replace_preserving_format,
    replace_single_range_with_content as replace_single_range_with_content,
)


