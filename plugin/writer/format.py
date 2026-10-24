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

import contextlib
import logging
import os
import re
import sys
import tempfile
from typing import Any, Iterable, Iterator, Literal, TypedDict

import uno
from plugin.framework.errors import best_effort, reraise_if_disposed
from plugin.framework.uno_context import create_property_value as create_property_value
from plugin.framework.url_utils import path_to_file_url
from .math.html_math_segment import html_fragment_contains_mixed_math  # noqa: F401  # pyright: ignore[reportUnusedImport]
from . import xhtml_style_postprocess as xhtml_post  # noqa: F401  # pyright: ignore[reportUnusedImport]

log = logging.getLogger("writeragent.writer")

__all__ = [
    "_with_temp_buffer",
    "_strip_html_boilerplate",
]


# ---------------------------------------------------------------------------
# Format configuration
# ---------------------------------------------------------------------------

HTML_FILTER = "HTML (StarWriter)"
HTML_EXTENSION = ".html"

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

# System temp directory (cross-platform). Under CrossHair, gettempdir() probes
# candidate dirs with open() and trips auditwall SideEffectDetected on import —
# honor tempfile's env vars (and a cwd fallback) without that probe.
def _resolve_temp_dir() -> str:
    if "crosshair" in sys.modules:
        return (
            os.environ.get("TMPDIR")
            or os.environ.get("TEMP")
            or os.environ.get("TMP")
            or tempfile.tempdir
            or os.curdir
        )
    return tempfile.gettempdir()


TEMP_DIR = _resolve_temp_dir()


@contextlib.contextmanager
def _with_temp_buffer(content: Any = None, ext: str | None = None) -> Iterator[tuple[str, str]]:
    """Context manager that yields ``(path, file_url)`` for a temp file
    with the correct format extension.

    If *content* is not ``None`` it is written to the file.
    *ext* overrides the file extension (e.g. ``".xhtml"`` for the read filter).
    The file is deleted on exit.
    """
    if ext is None:
        ext = HTML_EXTENSION
    fd, path = tempfile.mkstemp(suffix=ext, dir=TEMP_DIR)
    try:
        if content is not None:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(content)
        else:
            os.close(fd)
        yield (path, path_to_file_url(path))
    finally:
        best_effort(lambda: os.unlink(path), "temp buffer unlink failed")


# ---------------------------------------------------------------------------
# HTML helpers
# ---------------------------------------------------------------------------


def _strip_html_boilerplate(html_string: str) -> str:
    """Extract content between ``<body>`` tags if present."""
    if not html_string or not isinstance(html_string, str):
        return html_string
    match = re.search(r"<body[^>]*>(.*?)</body>", html_string, re.DOTALL | re.IGNORECASE)
    if match:
        return match.group(1).strip()
    return html_string


# Other code paths that set ParaStyleName and may need apply_paragraph_style_preserving_direct_char
# (audit each call site before adopting — semantics differ):
#   plugin/writer/structural.py — CloneHeadingBlock style re-apply
#   plugin/writer/format.py   — replace_single_range_with_content style restore (~595)
#   plugin/notebook/writer_importer.py — resolved paragraph style on import (~621)
#   plugin/notebook/notebook_runner.py — output cell paragraph style (~177)
# Char properties a paragraph style is expected to govern. apply_style defaults to
# clear_direct="style_props": these are the ONLY captured overrides not restored after the style
# is applied, so the house font and size show while hand-set bold/italic/colour survive.
# See CLEARABLE_PARA_PROPERTIES for the paragraph half (indents/alignment).
#
# Future idea — not now: clear whatever the *target style* actually defines (PropertyState /
# inheritance), instead of this fixed list. Hard because a Normal weight or an inherited default
# looks the same as "the style does not set this", so it is easy to wipe emphasis the style
# never claimed.
STYLE_GOVERNED_CHAR_PROPERTIES = (
    "CharFontName", "CharFontNameAsian", "CharFontNameComplex",
    "CharHeight", "CharHeightAsian", "CharHeightComplex",
)

# Whole-paragraph direct overrides cleared by clear_direct. This module owns the name list;
# styles.py imports it for the inspect/schema set so the two cannot drift. (styles already
# imports from format — there is no cycle to avoid.) Deliberately NOT setAllPropertiesToDefault
# — that also wipes numbering, borders and language, which no caller asked to lose.
CLEARABLE_PARA_PROPERTIES = (
    "ParaTopMargin", "ParaBottomMargin", "ParaLeftMargin", "ParaRightMargin",
    "ParaFirstLineIndent", "ParaAdjust", "ParaBackColor", "ParaKeepTogether", "ParaSplit",
)

# Scalar, JSON-safe subset reported back to the agent. Includes the Asian/Complex font/size
# slots because style_props clears those too — omitting them made a CJK house-font apply look
# like it only touched the Latin slot. The capture itself still covers every Char*; only the
# report is narrowed (CharLocale and friends are UNO structs).
REPORTED_CHAR_PROPERTIES = (
    "CharFontName", "CharFontNameAsian", "CharFontNameComplex",
    "CharHeight", "CharHeightAsian", "CharHeightComplex",
    "CharWeight", "CharPosture", "CharColor",
)

# Walk caps for the capture in apply_paragraph_style_preserving_direct_char. A UNO enumeration
# that misbehaves otherwise spins forever; hitting the cap means later runs were not read.
_CAPTURE_PARA_LIMIT = 200000
_CAPTURE_PORTION_LIMIT = 50000


def enum_hit_walk_cap(seen: int, limit: int, enum: Any) -> bool:
    """True when a walk stopped because of *limit*, not because *enum* was exhausted."""
    if seen < limit or enum is None:
        return False
    return enum.hasMoreElements() is True


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
    if not enum_hit_walk_cap(seen, limit, enum):
        return False
    msg = walk_cap_warning(kind, seen, limit)
    log.warning("%s", msg)
    if dest is not None:
        dest.append(msg)
    return True


_STYLE_PROP_FAILED = object()
_CHAR_PROPS_KEY = "__writable_char_props__"
FONT_SLANT_TO_INT: dict[str, int] = {
    "none": 0,
    "oblique": 1,
    "italic": 2,
}


def _reset_properties_to_default(cursor: Any, names: Iterable[str]) -> list[str]:
    """Drop *names* as direct attributes on *cursor* so they fall back to the paragraph style.

    This is Format > Clear Direct Formatting, narrowed to the names the caller asks for. It is the
    only reliable way to remove an override: re-applying a paragraph's existing style leaves its
    direct Char* standing (LibreOffice 26.2), so imposing a house font on a document whose
    paragraphs are all already 'Standard' silently keeps the old font without this.

    Returns the names actually cleared.
    """
    names = tuple(names)
    if not names:
        return []
    try:
        cursor.setPropertiesToDefault(names)
        return list(names)
    except Exception as exc:
        reraise_if_disposed(exc)
        pass  # no XMultiPropertyStates on this range — fall back to one at a time
    cleared = []
    for name in names:
        try:
            cursor.setPropertyToDefault(name)
            cleared.append(name)
        except Exception as exc:
            reraise_if_disposed(exc)
            continue
    return cleared


def _summarize_char_overrides(overrides: Any) -> dict[str, Any]:
    """Flat, JSON-safe {prop: value} of the captured direct Char* overrides (first portion wins)."""
    summary = {}
    for _pc, props in overrides:
        for name in REPORTED_CHAR_PROPERTIES:
            if name in props and name not in summary:
                val = props[name]
                if getattr(type(val), "__module__", None) in ("uno", "pyuno"):
                    inner = getattr(val, "value", None)
                    if inner is not None:
                        val = inner
                if name == "CharPosture" and isinstance(val, str):
                    low = val.strip().lower()
                    if low in FONT_SLANT_TO_INT:
                        val = FONT_SLANT_TO_INT[low]
                if isinstance(val, (str, int, float, bool)):
                    summary[name] = val
    return summary


class ParagraphStyleApplyReport(TypedDict, total=False):
    direct_formatting: str
    clear_direct: str
    preserved_char_overrides: dict[str, Any]
    removed_char_overrides: dict[str, Any]
    cleared_char_properties: list[str]
    cleared_paragraph_properties: list[str]
    warning: str
    capture_incomplete: bool


def apply_paragraph_style_preserving_direct_char(
    doc: Any,
    cursor: Any,
    style_name: str,
    clear_direct: Literal["none", "style_props", "all"] = "none",
    style_cache: dict[Any, Any] | None = None,
) -> ParagraphStyleApplyReport:
    """Set *cursor*'s ParaStyleName to *style_name*, deciding what happens to direct formatting.

    Setting ParaStyleName to a DIFFERENT style resets hand-set Char* properties to that style's
    defaults — across the WHOLE paragraph, even for a sub-range cursor — which is what the capture
    below exists to undo. Re-applying the style a paragraph already has does not reset Char*
    (LibreOffice 26.2). It *does* drop the paragraph's direct Para* (margins, alignment, first-line
    indent) — so re-applying Standard does not keep a hand-set quote indent. Clearing a Char*
    override is never a side effect to rely on; see _reset_properties_to_default.
    ``getPropertyState`` is unreliable at the text-portion level, so overrides are
    detected by VALUE (Char* differs from the paragraph's current style default).

    *clear_direct* decides what happens to those captured overrides. The helper default is
    ``"none"`` so HTML import / style-restore callers keep inline fonts; ``apply_style``
    defaults to ``"style_props"`` so a house-font update is visible without a retry:
      ``"style_props"`` restores everything except STYLE_GOVERNED_CHAR_PROPERTIES and clears
      CLEARABLE_PARA_PROPERTIES, so font/size/indent obey the style and bold/italic/colour survive;
      ``"none"`` restores all captured Char* — keeps a hand-set font (the style can look like a
      no-op). Does not restore Para*; LibreOffice already dropped those when ParaStyleName was set;
      ``"all"`` restores nothing and clears the paragraph properties too (Ctrl+M semantics).

    A result with a ``warning`` and ``capture_incomplete=True`` means later runs were not read
    back, so the override dict may be short.

    Returns a report dict so callers can tell the agent what actually happened.

    KNOWN LIMITATION: a direct override whose value equals the old style default is not
    captured; applying a style with a different default can change that property visibly.
    """

    walk_notes: list[str] = []
    capture_failures = 0
    if style_cache is None:
        style_cache = {}

    def _expand_to_full_paragraphs(cur: Any) -> Any:
        text = cur.getText()
        start = text.createTextCursorByRange(cur.getStart())
        end = text.createTextCursorByRange(cur.getEnd())
        start.gotoStartOfParagraph(False)
        end.gotoEndOfParagraph(True)
        expanded = text.createTextCursorByRange(start.getStart())
        expanded.gotoRange(end.getEnd(), True)
        return expanded

    def _capture_direct_char_overrides(capture_cursor: Any) -> list[Any]:
        nonlocal capture_failures
        overrides: list[Any] = []
        try:
            para_styles = doc.getStyleFamilies().getByName("ParagraphStyles")
        except Exception as exc:
            reraise_if_disposed(exc)
            capture_failures += 1
            return overrides

        READONLY = uno.getConstantByName("com.sun.star.beans.PropertyAttribute.READONLY")
        if not isinstance(READONLY, int):
            READONLY = 16
        try:
            para_enum = capture_cursor.createEnumeration()
        except Exception as exc:
            reraise_if_disposed(exc)
            capture_failures += 1
            return overrides

        _paras = 0
        while para_enum.hasMoreElements() is True and _paras < _CAPTURE_PARA_LIMIT:
            _paras += 1
            try:
                para = para_enum.nextElement()
            except Exception as exc:
                reraise_if_disposed(exc)
                capture_failures += 1
                break
            if not (hasattr(para, "supportsService") and para.supportsService("com.sun.star.text.Paragraph")):
                continue

            try:
                cur_style_name = para.getPropertyValue("ParaStyleName")
            except Exception as exc:
                reraise_if_disposed(exc)
                capture_failures += 1
                continue

            if cur_style_name in style_cache:
                old_style = style_cache[cur_style_name]
            else:
                try:
                    old_style = para_styles.getByName(cur_style_name)
                except Exception as exc:
                    reraise_if_disposed(exc)
                    capture_failures += 1
                    old_style = None
                style_cache[cur_style_name] = old_style

            if old_style is None:
                continue

            try:
                portion_enum = para.createEnumeration()
            except Exception as exc:
                reraise_if_disposed(exc)
                capture_failures += 1
                continue

            _portions = 0
            while portion_enum.hasMoreElements() is True and _portions < _CAPTURE_PORTION_LIMIT:
                _portions += 1
                try:
                    portion = portion_enum.nextElement()
                except Exception as exc:
                    reraise_if_disposed(exc)
                    capture_failures += 1
                    break

                try:
                    portion_type = portion.getPropertyValue("TextPortionType")
                except Exception as exc:
                    reraise_if_disposed(exc)
                    capture_failures += 1
                    continue
                if portion_type != "Text":
                    continue

                char_names = style_cache.get(_CHAR_PROPS_KEY)
                if char_names is None:
                    try:
                        info = portion.getPropertySetInfo()
                        if info is not None:
                            portion_props = info.getProperties()
                            names = [
                                p.Name for p in portion_props
                                if p.Name.startswith("Char") and not (getattr(p, "Attributes", 0) & READONLY)
                            ]
                            if names:
                                style_cache[_CHAR_PROPS_KEY] = names
                                char_names = names
                    except Exception as exc:
                        reraise_if_disposed(exc)
                        capture_failures += 1

                if not char_names:
                    continue

                props = {}
                for name in char_names:
                    try:
                        val = portion.getPropertyValue(name)
                    except Exception as exc:
                        reraise_if_disposed(exc)
                        capture_failures += 1
                        continue

                    cache_key = (cur_style_name, name)
                    if cache_key in style_cache:
                        style_val = style_cache[cache_key]
                    else:
                        try:
                            style_val = old_style.getPropertyValue(name)
                        except Exception as exc:
                            reraise_if_disposed(exc)
                            capture_failures += 1
                            style_val = _STYLE_PROP_FAILED
                        style_cache[cache_key] = style_val

                    if style_val is _STYLE_PROP_FAILED:
                        continue
                    if val == style_val:
                        continue
                    props[name] = val

                if props:
                    try:
                        pc = portion.getText().createTextCursorByRange(portion.getStart())
                        pc.gotoRange(portion.getEnd(), True)
                        overrides.append((pc, props))
                    except Exception as exc:
                        reraise_if_disposed(exc)
                        capture_failures += 1
                        continue

            record_walk_cap(portion_enum, _portions, _CAPTURE_PORTION_LIMIT, "text portions", walk_notes)
        record_walk_cap(para_enum, _paras, _CAPTURE_PARA_LIMIT, "paragraphs", walk_notes)
        return overrides

    capture_cursor = _expand_to_full_paragraphs(cursor)
    overrides = _capture_direct_char_overrides(capture_cursor)
    found = _summarize_char_overrides(overrides)
    try:
        cursor.setPropertyValue("ParaStyleName", style_name)
    except Exception as exc:
        reraise_if_disposed(exc)
        raise

    if clear_direct == "all":
        skip = None  # restore nothing
    elif clear_direct == "style_props":
        skip = set(STYLE_GOVERNED_CHAR_PROPERTIES)
    else:
        skip = set()  # historical behaviour: restore everything

    if skip is not None:
        for pc, props in overrides:
            for name, val in props.items():
                if name in skip:
                    continue
                try:
                    pc.setPropertyValue(name, val)
                except Exception as exc:
                    reraise_if_disposed(exc)
                    capture_failures += 1

    report: ParagraphStyleApplyReport = {
        "direct_formatting": "preserved" if clear_direct == "none" else "cleared",
        "clear_direct": clear_direct,
    }
    if clear_direct == "none":
        # What the caller can act on: these overrode the style and are the reason it looks like
        # the apply did nothing.
        if found:
            report["preserved_char_overrides"] = found
    else:
        # Only what was really dropped — under "style_props" bold and italic were restored, and
        # listing them as removed would send the caller chasing a change that never happened.
        removed = found if skip is None else {k: v for k, v in found.items() if k in skip}
        if removed:
            report["removed_char_overrides"] = removed
        # Clear Char* explicitly: the ParaStyleName side effect leaves direct overrides on portions.
        char_targets = (
            STYLE_GOVERNED_CHAR_PROPERTIES if clear_direct == "style_props"
            else sorted({n for _pc, props in overrides for n in props})
        )
        report["cleared_char_properties"] = _reset_properties_to_default(capture_cursor, char_targets)
        report["cleared_paragraph_properties"] = _reset_properties_to_default(capture_cursor, CLEARABLE_PARA_PROPERTIES)

    if capture_failures > 0:
        walk_notes.append("Some direct formatting overrides could not be read; the capture may be incomplete.")
    if walk_notes:
        report["warning"] = " ".join(walk_notes)
    if capture_failures > 0:
        report["capture_incomplete"] = True
    return report


# ---------------------------------------------------------------------------
# Markup detection & format-preserving replacement
# ---------------------------------------------------------------------------


# Block-level HTML tags: their presence means the content defines its own
# paragraph structure, so we must NOT force the original paragraph style onto it.


def run_writer_mutation_with_optional_review(doc: Any, ctx: Any, apply_fn: Any) -> None:
    """Run a Writer document mutation, optionally wrapped in EditReviewSession.

    LibrePy omits ``plugin.writer.edit_review``; when that module is absent we apply
    directly. Full WriterAgent (or co-install) imports it and honors review mode.
    """
    try:
        from plugin.writer.edit_review import EditReviewSession, review_recording_enabled
    except ImportError:
        apply_fn()
        return
    review = EditReviewSession(doc, ctx, enabled=review_recording_enabled(ctx))
    try:
        with review:
            review.record_mutation(apply_fn)
    finally:
        review.cleanup()


# ---------------------------------------------------------------------------
# Re-exports from html_export and html_import.
# ---------------------------------------------------------------------------

from .html_export import (
    _apply_image_export_options as _apply_image_export_options,
    document_to_content as document_to_content,
    strip_embedded_image_data as strip_embedded_image_data,
    xtext_to_content as xtext_to_content,
)
from .html_import import (
    _content_has_block_markup as _content_has_block_markup,
    _ensure_html_linebreaks as _ensure_html_linebreaks,
    _insert_mixed_html_and_math_at_cursor as _insert_mixed_html_and_math_at_cursor,
    _insert_mixed_or_plain_html as _insert_mixed_or_plain_html,
    _insert_starwriter_html_at_cursor as _insert_starwriter_html_at_cursor,
    content_has_markup as content_has_markup,
    html_to_plain_text as html_to_plain_text,
    plain_text_if_markup as plain_text_if_markup,
    insert_content_at_position as insert_content_at_position,
    insert_html_at_cursor as insert_html_at_cursor,
    insert_html_fragment_at_cursor as insert_html_fragment_at_cursor,
    insert_inline_at_cursor as insert_inline_at_cursor,
    replace_full_document as replace_full_document,
    replace_preserving_format as replace_preserving_format,
    replace_single_range_with_content as replace_single_range_with_content,
    replace_xtext_with_html as replace_xtext_with_html,
)
from .field_restore import (
    rewrite_exported_field_spans as rewrite_exported_field_spans,
)

content_has_block_markup = _content_has_block_markup


