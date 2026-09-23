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
from typing import Any, cast

import uno
from plugin.doc.text_helpers import get_string_without_tracked_deletions as _get_str  # noqa: F401  # pyright: ignore[reportUnusedImport]
from plugin.framework.url_utils import path_to_file_url
from .math.html_math_segment import html_fragment_contains_mixed_math  # noqa: F401  # pyright: ignore[reportUnusedImport]
from .math.math_mml_convert import convert_latex_to_starmath as convert_latex_to_starmath  # noqa: F401  # pyright: ignore[reportUnusedImport]
from .math.math_mml_convert import convert_mathml_to_starmath as convert_mathml_to_starmath  # noqa: F401  # pyright: ignore[reportUnusedImport]
from .math.math_mml_convert import insert_writer_math_formula as insert_writer_math_formula  # noqa: F401  # pyright: ignore[reportUnusedImport]
from . import xhtml_style_postprocess as xhtml_post  # noqa: F401  # pyright: ignore[reportUnusedImport]

log = logging.getLogger("writeragent.writer")


def _selection_range_for_export(model: Any):  # pyright: ignore[reportUnusedFunction]
    """Resolve selection range for export (lazy import so html_export never names text_helpers directly or document_helpers)."""
    from plugin.doc.text_helpers import get_selection_range

    return get_selection_range(model)


def _deletion_author():  # pyright: ignore[reportUnusedFunction]
    """WriterAgent split-author coloring; no-op when ``review_authors`` is omitted (LibrePy)."""
    try:
        from .review_authors import deletion_author

        return deletion_author()
    except ImportError:
        return contextlib.nullcontext()


def _resolve_style_name(model: Any, style_name: str):  # pyright: ignore[reportUnusedFunction]
    """Resolve a style name case-insensitively against document ParagraphStyles."""
    try:
        families = model.getStyleFamilies()
        para_styles = families.getByName("ParagraphStyles")
        if para_styles.hasByName(style_name):
            return style_name
        lower = style_name.lower()
        for name in para_styles.getElementNames():
            if name.lower() == lower:
                return name
    except Exception:
        pass
    return style_name


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


def _get_format_props(config_svc: Any = None):
    """Return ``(filter_name, file_extension)`` for HTML format."""
    return HTML_FILTER, HTML_EXTENSION


# ---------------------------------------------------------------------------
# UNO helpers (import inside functions to avoid import-time dependency)
# ---------------------------------------------------------------------------


def create_property_value(name: str, value: Any):
    """Create a ``com.sun.star.beans.PropertyValue``."""
    p = cast("Any", uno.createUnoStruct("com.sun.star.beans.PropertyValue"))
    p.Name = name
    p.Value = value
    return p


@contextlib.contextmanager
def _with_temp_buffer(content: Any = None, config_svc: Any = None, ext: str | None = None):  # pyright: ignore[reportUnusedFunction]
    """Context manager that yields ``(path, file_url)`` for a temp file
    with the correct format extension.

    If *content* is not ``None`` it is written to the file.
    *ext* overrides the file extension (e.g. ``".xhtml"`` for the read filter).
    The file is deleted on exit.
    """
    if ext is None:
        _unused, ext = _get_format_props(config_svc)
    fd, path = tempfile.mkstemp(suffix=ext, dir=TEMP_DIR)
    try:
        if content is not None:
            if isinstance(content, list):
                content = "\n".join(str(x) for x in content)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(content)
        else:
            os.close(fd)
        yield (path, path_to_file_url(path))
    finally:
        try:
            os.unlink(path)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# HTML helpers
# ---------------------------------------------------------------------------


def _strip_html_boilerplate(html_string: str):  # pyright: ignore[reportUnusedFunction]
    """Extract content between ``<body>`` tags if present."""
    if not html_string or not isinstance(html_string, str):
        return html_string
    match = re.search(r"<body[^>]*>(.*?)</body>", html_string, re.DOTALL | re.IGNORECASE)
    if match:
        return match.group(1).strip()
    return html_string


# XHTML export embeds graphics as data:image/...;base64,... — strip payload only, keep external src URLs.


# ---------------------------------------------------------------------------
# Semantic style model (read path): XHTML filter -> semantic data-lo-style HTML.
# Pure string/CSS pipeline: xhtml_style_postprocess (no UNO). This module owns UNO export
# (XHTML + optional flat ODF for Pn->parent autostyle recovery). v1 cost: two storeToURL per
# full read; v1 gaps (partial-edit style apply, table cells, whole-para overrides): see
# docs/writer/html-style-model-plan.md#v1-limitations-shipped.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Semantic style model (write path): honor data-lo-style on incoming HTML.
# Named styles applied only on replace_full_document (apply_styles=True). Partial inserts
# strip the attribute but do not apply — see docs/writer/html-style-model-plan.md#v1-limitations-shipped.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Document -> content
# ---------------------------------------------------------------------------

# com.sun.star.text.ControlCharacter.PARAGRAPH_BREAK


# ---------------------------------------------------------------------------
# Content -> Document
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Paragraph style apply (preserve direct Char* formatting)
# ---------------------------------------------------------------------------


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


def enum_hit_walk_cap(seen: int, limit: int, enum: Any):
    """True when a walk stopped because of *limit*, not because *enum* was exhausted."""
    if seen < limit or enum is None:
        return False
    try:
        return enum.hasMoreElements() is True
    except Exception:
        return False


def walk_cap_warning(kind: str, seen: int, limit: int):
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


def record_walk_cap(enum: Any, seen: int, limit: int, kind: str, dest: list[str] | None = None):
    """Log (and optionally collect) a walk-cap warning. Returns True when the cap was hit."""
    if not enum_hit_walk_cap(seen, limit, enum):
        return False
    msg = walk_cap_warning(kind, seen, limit)
    log.warning("%s", msg)
    if dest is not None:
        dest.append(msg)
    return True


def _reset_properties_to_default(cursor: Any, names: Any):
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
    except Exception:
        pass  # no XMultiPropertyStates on this range — fall back to one at a time
    cleared = []
    for name in names:
        try:
            cursor.setPropertyToDefault(name)
            cleared.append(name)
        except Exception:
            continue
    return cleared


def _summarize_char_overrides(overrides: Any):
    """Flat, JSON-safe {prop: value} of the captured direct Char* overrides (first portion wins)."""
    summary = {}
    for _pc, props in overrides:
        for name in REPORTED_CHAR_PROPERTIES:
            if name in props and name not in summary:
                val = props[name]
                if isinstance(val, (str, int, float, bool)):
                    summary[name] = val
    return summary


def apply_paragraph_style_preserving_direct_char(doc: Any, cursor: Any, style_name: str, clear_direct: str = "none"):
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

    Returns a report dict so callers can tell the agent what actually happened.

    KNOWN LIMITATION: a direct override whose value equals the old style default is not
    captured; applying a style with a different default can change that property visibly.
    """

    walk_notes: list[str] = []

    def _expand_to_full_paragraphs(cur: Any):
        try:
            text = cur.getText()
            start = text.createTextCursorByRange(cur.getStart())
            end = text.createTextCursorByRange(cur.getEnd())
            start.gotoStartOfParagraph(False)
            end.gotoEndOfParagraph(True)
            expanded = text.createTextCursorByRange(start.getStart())
            expanded.gotoRange(end.getEnd(), True)
            return expanded
        except Exception:
            return None

    def _capture_direct_char_overrides(capture_cursor: Any):
        overrides: list[Any] = []
        try:
            para_styles = doc.getStyleFamilies().getByName("ParagraphStyles")
        except Exception:
            para_styles = None
        try:
            READONLY = uno.getConstantByName("com.sun.star.beans.PropertyAttribute.READONLY")
        except Exception:
            READONLY = 0
        try:
            para_enum = capture_cursor.createEnumeration()
        except Exception:
            return overrides
        _paras = 0
        while para_enum.hasMoreElements() is True and _paras < _CAPTURE_PARA_LIMIT:
            _paras += 1
            try:
                para = para_enum.nextElement()
            except Exception:
                break
            if not (hasattr(para, "supportsService") and para.supportsService("com.sun.star.text.Paragraph")):
                continue
            old_style = None
            if para_styles is not None:
                try:
                    old_style = para_styles.getByName(para.getPropertyValue("ParaStyleName"))
                except Exception:
                    old_style = None
            if old_style is None:
                continue
            try:
                portion_enum = para.createEnumeration()
            except Exception:
                continue
            _portions = 0
            while portion_enum.hasMoreElements() is True and _portions < _CAPTURE_PORTION_LIMIT:
                _portions += 1
                try:
                    portion = portion_enum.nextElement()
                    if portion.getPropertyValue("TextPortionType") != "Text":
                        continue
                    portion_props = portion.getPropertySetInfo().getProperties()
                except Exception:
                    continue
                props = {}
                for p in portion_props:
                    name = p.Name
                    if not name.startswith("Char"):
                        continue
                    if READONLY and (p.Attributes & READONLY):
                        continue
                    try:
                        val = portion.getPropertyValue(name)
                    except Exception:
                        continue
                    try:
                        if val == old_style.getPropertyValue(name):
                            continue
                    except Exception:
                        continue
                    props[name] = val
                if props:
                    try:
                        pc = portion.getText().createTextCursorByRange(portion.getStart())
                        pc.gotoRange(portion.getEnd(), True)
                        overrides.append((pc, props))
                    except Exception:
                        continue
            record_walk_cap(portion_enum, _portions, _CAPTURE_PORTION_LIMIT, "text portions", walk_notes)
        record_walk_cap(para_enum, _paras, _CAPTURE_PARA_LIMIT, "paragraphs", walk_notes)
        return overrides

    capture_cursor = _expand_to_full_paragraphs(cursor) or cursor
    overrides = _capture_direct_char_overrides(capture_cursor)
    found = _summarize_char_overrides(overrides)
    cursor.setPropertyValue("ParaStyleName", style_name)

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
                except Exception:
                    pass

    report: dict[str, Any] = {"direct_formatting": "preserved" if clear_direct == "none" else "cleared",
                              "clear_direct": clear_direct}
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
        # Clear explicitly rather than leaning on the ParaStyleName side effect, which leaves
        # direct Char* on the text portions standing. "all" targets exactly the overrides that
        # were found, so nothing unrelated is touched.
        char_targets = (STYLE_GOVERNED_CHAR_PROPERTIES if clear_direct == "style_props"
                        else sorted({n for _pc, props in overrides for n in props}))
        report["cleared_char_properties"] = _reset_properties_to_default(capture_cursor, char_targets)
        report["cleared_paragraph_properties"] = _reset_properties_to_default(capture_cursor, CLEARABLE_PARA_PROPERTIES)
    if walk_notes:
        report["warning"] = " ".join(walk_notes)
    return report


# ---------------------------------------------------------------------------
# Text search (forwarded to plugin.writer.search)
# ---------------------------------------------------------------------------


def find_text_ranges(model: Any, ctx: Any, search: str, start: int = 0, limit: int | None = None, case_sensitive: bool = True):
    from .search import find_text_ranges as impl
    return impl(model, ctx, search, start=start, limit=limit, case_sensitive=case_sensitive)


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
# Pipeline implementations live in html_export / html_import. Wrappers keep
# plugin.writer.format as the public import path without a circular
# format <-> html_* import at module load.
# ---------------------------------------------------------------------------


def strip_embedded_image_data(html: str) -> str:
    from .html_export import strip_embedded_image_data as impl
    return impl(html)


def _apply_image_export_options(content: str, *, include_images: bool) -> str:  # pyright: ignore[reportUnusedFunction]
    from .html_export import _apply_image_export_options as impl
    return impl(content, include_images=include_images)


def document_to_content(model: Any, ctx: Any, services: Any, max_chars: int | None = None, scope: str = "full", range_start: int | None = None, range_end: int | None = None, *, include_images: bool = False, walk_warnings: list[str] | None = None):
    from .html_export import document_to_content as impl
    return impl(model, ctx, services, max_chars, scope, range_start, range_end, include_images=include_images, walk_warnings=walk_warnings)


def xtext_to_content(text_obj: Any, model: Any, ctx: Any, services: Any = None, *, include_images: bool = True, max_chars: int | None = None):
    from .html_export import xtext_to_content as impl
    return impl(text_obj, model, ctx, services, include_images=include_images, max_chars=max_chars)


def _ensure_html_linebreaks(content: str):  # pyright: ignore[reportUnusedFunction]
    from .html_import import _ensure_html_linebreaks as impl
    return impl(content)


def html_to_plain_text(html_string: str, ctx: Any, config_svc: Any = None):
    from .html_import import html_to_plain_text as impl
    return impl(html_string, ctx, config_svc)


def insert_html_fragment_at_cursor(cursor: Any, html_fragment: str, *, extra_css: str | None = None, wrap: bool = True, config_svc: Any = None, model: Any = None):
    from .html_import import insert_html_fragment_at_cursor as impl
    return impl(cursor, html_fragment, extra_css=extra_css, wrap=wrap, config_svc=config_svc, model=model)


def _insert_starwriter_html_at_cursor(model: Any, cursor: Any, prepared_html: str, config_svc: Any = None):  # pyright: ignore[reportUnusedFunction]
    from .html_import import _insert_starwriter_html_at_cursor as impl
    return impl(model, cursor, prepared_html, config_svc)


def _insert_mixed_html_and_math_at_cursor(model: Any, ctx: Any, cursor: Any, unescaped: str, config_svc: Any = None):  # pyright: ignore[reportUnusedFunction]
    from .html_import import _insert_mixed_html_and_math_at_cursor as impl
    return impl(model, ctx, cursor, unescaped, config_svc)


def _insert_mixed_or_plain_html(model: Any, ctx: Any, cursor: Any, unescaped_content: str, config_svc: Any = None, apply_styles: bool = True):  # pyright: ignore[reportUnusedFunction]
    from .html_import import _insert_mixed_or_plain_html as impl
    return impl(model, ctx, cursor, unescaped_content, config_svc, apply_styles)


def insert_html_at_cursor(model: Any, ctx: Any, cursor: Any, unescaped_content: str, config_svc: Any = None, apply_styles: bool = True):
    from .html_import import insert_html_at_cursor as impl
    return impl(model, ctx, cursor, unescaped_content, config_svc, apply_styles)


def insert_content_at_position(model: Any, ctx: Any, content: str, position: str, config_svc: Any = None):
    from .html_import import insert_content_at_position as impl
    return impl(model, ctx, content, position, config_svc)


def replace_full_document(model: Any, ctx: Any, content: str, config_svc: Any = None):
    from .html_import import replace_full_document as impl
    return impl(model, ctx, content, config_svc)


def replace_single_range_with_content(model: Any, text_range: Any, content: str, ctx: Any, config_svc: Any = None):
    from .html_import import replace_single_range_with_content as impl
    return impl(model, text_range, content, ctx, config_svc)


def replace_xtext_with_html(text_obj: Any, html: str, config_svc: Any = None, model: Any = None):
    from .html_import import replace_xtext_with_html as impl
    return impl(text_obj, html, config_svc, model)


def rewrite_exported_field_spans(html: str):
    from .html_import import rewrite_exported_field_spans as impl
    return impl(html)


def content_has_markup(content: str):
    from .html_import import content_has_markup as impl
    return impl(content)


def _content_has_block_markup(content: str):  # pyright: ignore[reportUnusedFunction]
    from .html_import import _content_has_block_markup as impl
    return impl(content)


def replace_preserving_format(model: Any, target_range: Any, new_text: str, ctx: Any = None, in_undo_context: bool = False, split_author: bool = True):
    from .html_import import replace_preserving_format as impl
    return impl(model, target_range, new_text, ctx, in_undo_context, split_author)

