# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2024 John Balis
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""XHTML/FODT export and image stripping for Writer documents.

Public entries: ``document_to_content`` and ``xtext_to_content``
(also re-exported from ``plugin.writer.format``).

CJK ruby: the XHTML filter concatenates ``text:ruby`` children and range copy
drops ``RubyText``. ``inject_ruby_into_html`` restores ``<ruby><rt>`` after
semantic post-process. Apply strips ``<rt>`` then sets ``RubyText`` on the
base run (``html_import.extract_and_strip_ruby`` / ``_apply_ruby_spans``).
"""

from __future__ import annotations

import logging
import re
import time
from html import escape as html_escape
from typing import Any

from plugin.doc.text_helpers import (
    get_string_without_tracked_deletions,
    _visible_portions as _shared_visible_portions,
)
from plugin.framework.uno_context import get_desktop
from . import xhtml_style_postprocess as xhtml_post
from . import format as format_mod

log = logging.getLogger("writeragent.writer")


class _RubySpan:
    """One CJK ruby (furigana) pair collected from Writer portions.

    ``start`` / ``end`` are offsets in the paragraph's visible text (base only;
    ruby marks have empty ``getString()``). Used to keep range export from
    wrapping a base that was trimmed out of the copied window.
    """

    __slots__ = ("base", "reading", "start", "end")

    def __init__(self, base: str, reading: str, start: int = 0, end: int = 0) -> None:
        self.base = base
        self.reading = reading
        self.start = start
        self.end = end

# com.sun.star.text.ControlCharacter.PARAGRAPH_BREAK
_PARAGRAPH_BREAK = 0

_DATA_URI_IMAGE_RE = re.compile(
    r"data:image/[^\"'\s);>]+;base64,[A-Za-z0-9+/=\s]+",
    re.IGNORECASE,
)
# The XHTML filter rewrites a fragment such as ``#2.3.4.Title.|outline`` into
# an HTML-safe id. The raw HyperLinkURL is put back on each exported anchor,
# in the order the source portions were copied.
_ANCHOR_HREF_RE = re.compile(r'(<a\b[^>]*?\bhref=")([^"]*)(")', re.IGNORECASE)


def strip_embedded_image_data(html: str) -> str:
    """Remove inline ``data:image`` base64 payloads from exported HTML; external URLs unchanged."""
    if not html:
        return html
    return _DATA_URI_IMAGE_RE.sub("", html)



def _apply_image_export_options(content: str, *, include_images: bool) -> str:
    if include_images or not content:
        return content
    return strip_embedded_image_data(content)


def _ruby_parts(span: Any):
    """``(base, reading)`` from a ``_RubySpan`` or a 2-tuple (unit tests)."""
    if isinstance(span, (tuple, list)):
        return span[0], span[1]
    return span.base, span.reading


def _inside_ruby_element(html: str, index: int):
    """True when *index* sits inside an existing ``<ruby>…</ruby>``."""
    last_open = html.rfind("<ruby", 0, index)
    if last_open < 0:
        return False
    last_close = html.rfind("</ruby>", 0, index)
    return last_close < last_open


def _index_in_tag(html: str, index: int):
    """True when *index* is inside ``<...>`` (attribute text is not body text)."""
    last_lt = html.rfind("<", 0, index)
    if last_lt < 0:
        return False
    last_gt = html.rfind(">", 0, index)
    return last_gt < last_lt


def _find_contiguous(html: str, needle: str, start: int = 0):
    """Offsets of *needle* as a raw substring that is not inside a tag."""
    if not html or not needle:
        return None
    idx = start
    while True:
        found = html.find(needle, idx)
        if found < 0:
            return None
        if not _index_in_tag(html, found):
            return found, found + len(needle)
        idx = found + 1


def _only_tags_or_ws(fragment: str):
    """True when *fragment* has no body text (tags and whitespace only)."""
    i = 0
    n = len(fragment)
    while i < n:
        ch = fragment[i]
        if ch.isspace():
            i += 1
            continue
        if ch != "<":
            return False
        close = fragment.find(">", i)
        if close < 0:
            return False
        i = close + 1
    return True


def _ruby_markup(base: str, reading: str):
    return "<ruby>%s<rt>%s</rt></ruby>" % (html_escape(base), html_escape(reading))


def inject_ruby_into_html(html: str, spans: list[Any]):
    """Rewrite glued or dropped ruby encodings to semantic ``<ruby><rt>``.

    The XHTML Writer filter has no ``text:ruby`` rule, so full export concatenates
    base + reading (``漢字かんじです``). Range/portion-copy never paints ``RubyText``
    — that property lives on the empty Ruby *start* mark, not the Text run —
    so range export used to drop the reading. Both become
    ``<ruby>漢字<rt>かんじ</rt></ruby>です``. Apply recreates portions in
    ``html_import`` (strip ``<rt>``, then ``RubyText`` on the base run).

    Replacements stay inside a single text node (or wrap the base node and
    delete a following reading node). Crossing a tag with a wrapper used to
    emit ``<span><ruby>…</span>…`` — browsers recover, but it is not HTML we
    want the agent to copy.
    """
    if not html or not spans:
        return html
    pos = 0
    parts = []
    for span in spans:
        base, reading = _ruby_parts(span)
        if not base or not reading:
            continue
        glued = _find_contiguous(html, base + reading, pos)
        if glued is not None and not _inside_ruby_element(html, glued[0]):
            start, end = glued
            parts.append(html[pos:start])
            parts.append(_ruby_markup(base, reading))
            pos = end
            continue
        base_at = _find_contiguous(html, base, pos)
        if base_at is None or _inside_ruby_element(html, base_at[0]):
            continue
        start, end = base_at
        reading_at = _find_contiguous(html, reading, end)
        if (
            reading_at is not None
            and _only_tags_or_ws(html[end:reading_at[0]])
            and not _inside_ruby_element(html, reading_at[0])
        ):
            # Bold/span around the base: keep those tags, drop the glued reading.
            parts.append(html[pos:start])
            parts.append(_ruby_markup(base, reading))
            parts.append(html[end:reading_at[0]])
            pos = reading_at[1]
            continue
        parts.append(html[pos:start])
        parts.append(_ruby_markup(base, reading))
        pos = end
    parts.append(html[pos:])
    return "".join(parts)


def _iter_xtext_paragraphs(text_obj: Any):
    """Yield paragraphs under *text_obj*, descending into table cells."""
    try:
        enum = text_obj.createEnumeration()
    except Exception:
        return
    while enum.hasMoreElements() is True:
        try:
            el = enum.nextElement()
        except Exception:
            break
        if _supports_service(el, "com.sun.star.text.TextTable"):
            try:
                names = el.getCellNames() or ()
            except Exception:
                continue
            for name in names:
                try:
                    cell = el.getCellByName(name)
                except Exception:
                    continue
                yield from _iter_xtext_paragraphs(cell)
            continue
        if hasattr(el, "createEnumeration"):
            yield el


def _iter_ruby_spans_in_paragraph(para: Any):
    """Yield ``_RubySpan`` for each Ruby start/end pair in *para*.

    Writer stores ruby as ``TextPortionType="Ruby"`` marks: the start has
    ``RubyText`` / ``RubyIsAbove`` / ``RubyAdjust``; the base is a normal
    ``Text`` run; the end mark is empty. ``getString()`` on the paragraph is
    base only — reading is not a text field.
    """
    try:
        portion_enum = para.createEnumeration()
    except Exception:
        return
    in_delete = False
    in_ruby = False
    reading = ""
    base_parts = []
    base_start = 0
    offset = 0
    while portion_enum.hasMoreElements() is True:
        try:
            portion = portion_enum.nextElement()
            kind = portion.getPropertyValue("TextPortionType")
        except Exception:
            break
        if kind == "Redline":
            try:
                if str(portion.getPropertyValue("RedlineType")) == "Delete":
                    in_delete = not in_delete
            except Exception:
                pass
            continue
        if in_delete:
            continue
        if kind == "Ruby":
            if not in_ruby:
                try:
                    reading = str(portion.getPropertyValue("RubyText") or "")
                except Exception:
                    reading = ""
                in_ruby = True
                base_parts = []
                base_start = offset
            else:
                base = "".join(base_parts)
                if base and reading:
                    yield _RubySpan(base, reading, base_start, base_start + len(base))
                in_ruby = False
                reading = ""
                base_parts = []
            continue
        try:
            chunk = portion.getString() or ""
        except Exception:
            chunk = ""
        if not chunk:
            continue
        if in_ruby:
            base_parts.append(chunk)
        offset += len(chunk)


def iter_ruby_spans(text_obj: Any):
    """Document-order ruby pairs under *text_obj* (body or cell), including tables."""
    for para in _iter_xtext_paragraphs(text_obj):
        yield from _iter_ruby_spans_in_paragraph(para)


def _ruby_spans_in_window(para: Any, trim_start: int, trim_end: int):
    """Ruby pairs whose entire base lies inside the copied visible-text window."""
    out = []
    for span in _iter_ruby_spans_in_paragraph(para):
        if span.start >= trim_start and span.end <= trim_end:
            out.append(span)
    return out


def _inject_ruby_from_model(model: Any, content: str, fodt_has_ruby: bool | None = None):
    """Walk the live model and rewrite *content* when ruby is (or may be) present.

    *fodt_has_ruby* is True/False from the paired FODT sidecar, or None when
    that export failed. False skips the portion walk on the common no-ruby path.
    """
    if not content:
        return content
    if fodt_has_ruby is False:
        return content
    try:
        spans = list(iter_ruby_spans(model.getText()))
    except Exception:
        log.debug("_inject_ruby_from_model: portion walk failed", exc_info=True)
        return content
    return inject_ruby_into_html(content, spans)


def _inject_exported_math_tex(model: Any, ctx: Any, content: str) -> str:
    """Replace formula OLE holes with delimited TeX for the model/chat.

    Failures stay in the HTML as a visible fallback; never drop formulas.
    """
    if not content or model is None or ctx is None:
        return content
    try:
        from plugin.writer.math.math_mml_export import inject_math_tex_into_html

        return inject_math_tex_into_html(model, ctx, content)
    except Exception:
        log.debug("_inject_exported_math_tex failed", exc_info=True)
        return content



def _export_xhtml(doc: Any, config_svc: Any):
    """Export *doc* via the XHTML Writer File filter; return the raw XHTML string."""
    with format_mod._with_temp_buffer(None, config_svc, ext=format_mod.XHTML_EXTENSION) as (path, file_url):
        props = (format_mod.create_property_value("FilterName", format_mod.XHTML_FILTER),)
        doc.storeToURL(file_url, props)
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return f.read()



def _autostyle_maps(doc: Any, config_svc: Any):
    """Export *doc* as flat ODF once and return ``(parents, overrides, has_ruby)``.

    ``parents`` (Pn -> base style name) lets the read path recover an autostyle paragraph's real
    style name when the XHTML CSS fingerprint matches nothing. ``overrides`` (Pn -> CSS text) is
    the paragraph's DIRECT formatting, which the flattened XHTML cannot distinguish from inherited
    values. ``has_ruby`` is True when the sidecar contains ``<text:ruby`` (ODF keeps ruby;
    the XHTML filter has no ``text:ruby`` rule and concatenates children). None means the
    export failed — the caller should walk portions rather than assume there is no ruby.

    Both come from the same export. Returns ``({}, {}, None)`` on any failure (the read still
    works, just without autostyle-name recovery and without the direct-formatting report).

    Both scopes report overrides: the range path copies the source paragraphs' direct formatting
    onto the temp document first (see _paint_direct_formatting)."""
    try:
        with format_mod._with_temp_buffer(None, config_svc, ext=format_mod.FODT_EXTENSION) as (path, file_url):
            props = (format_mod.create_property_value("FilterName", format_mod.FLAT_ODF_FILTER),)
            doc.storeToURL(file_url, props)
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                fodt = f.read()
        return (xhtml_post.extract_autostyle_parents_from_fodt(fodt),
                xhtml_post.extract_autostyle_overrides_from_fodt(fodt),
                "<text:ruby" in fodt)
    except Exception:
        log.debug("_autostyle_maps: flat-ODF export failed", exc_info=True)
        return ({}, {}, None)



# Direct formatting the range copy carries into the temp document. Whitelists rather than "every
# property": a blanket copy drags UNO structs and page/section properties along, which either fail
# to set or change the temp document's layout. Char* is painted per text portion so a bold run
# inside a sentence survives; Para* is set once per paragraph.
# HyperLink* is not a Char* style. The temp copy is setString, which drops the
# source portion's URL, so the XHTML filter would export the TOC line with no
# href. Full-document export does not use this list; it filters the real model.
# RubyText / RubyIsAbove / RubyAdjust are omitted on purpose: they live on the
# empty Ruby *start* mark, not the visible Text run (_visible_portions skips
# empty marks). Painting them from the Text run copies None. Recreating live
# ruby on the temp doc would still glue in XHTML (no text:ruby rule). Range
# export instead rewrites HTML from source spans (inject_ruby_into_html).
_COPIED_CHAR_PROPERTIES = (
    "CharStyleName", "CharFontName", "CharHeight", "CharWeight", "CharPosture",
    "CharUnderline", "CharStrikeout", "CharColor", "CharBackColor", "CharCaseMap",
    "CharEscapement", "CharEscapementHeight",
    "HyperLinkURL", "HyperLinkName", "HyperLinkTarget",
)
_COPIED_PARA_PROPERTIES = (
    "ParaLeftMargin", "ParaRightMargin", "ParaTopMargin", "ParaBottomMargin",
    "ParaFirstLineIndent", "ParaAdjust", "ParaBackColor",
)

_COPY_PORTION_LIMIT = 50000


def _copy_properties(src: Any, dst: Any, names: tuple[str, ...], style: Any = None):
    """Copy *names* from one range to another, but only where they differ from *style*.

    Copying a value equal to the style's turns an inherited value into a hand-set one on the copy,
    and the read would then report it as a direct override — margin-right:0cm and text-align:left
    on every paragraph, drowning the one indent that was actually set. Detection is by VALUE for
    the same reason as apply_paragraph_style_preserving_direct_char: getPropertyState is not
    dependable at the text-portion level.

    Per-property rather than all-or-nothing: the temp document is a plain Writer doc and does not
    necessarily offer every property the source paragraph carries.
    """
    for name in names:
        try:
            value = src.getPropertyValue(name)
        except Exception:
            continue
        if style is not None:
            try:
                if value == style.getPropertyValue(name):
                    continue
            except Exception:
                pass
        try:
            dst.setPropertyValue(name, value)
        except Exception:
            continue


def _source_style(model: Any, style_name: str, cache: dict[str, Any]):
    """The source document's paragraph-style object for *style_name*, or None. Cached per read."""
    if style_name in cache:
        return cache[style_name]
    style = None
    if style_name:
        try:
            style = model.getStyleFamilies().getByName("ParagraphStyles").getByName(style_name)
        except Exception:
            style = None
    cache[style_name] = style
    return style


def _visible_portions(para: Any, limit: int = _COPY_PORTION_LIMIT, truncated_out: list[str] | None = None):
    """Visible portions for offset paint. Same walk as the text helper.

    Aborts on portion enum / type failure so later runs are not painted at a
    drifted offset. ``get_string_without_tracked_deletions`` continues past a
    bad portion instead — that is the only intentional divergence.

    Hitting *limit* used to stop silently, so a range read could omit later
    runs' Char* with no signal. When the shared walk stops because of the cap,
    log and append ``walk_cap_warning`` to *truncated_out*.
    """
    hit: list[int] = []
    yield from _shared_visible_portions(
        para, abort_on_portion_error=True, limit=limit, truncated_out=hit
    )
    if hit:
        msg = format_mod.walk_cap_warning("text portions", hit[0], limit)
        log.warning("%s", msg)
        if truncated_out is not None:
            truncated_out.append(msg)


def _paint_direct_formatting(
    para: Any,
    portions: list[tuple[Any, str]],
    temp_text: Any,
    trim_start: int,
    trim_end: int,
    style: Any = None,
) -> None:
    """Re-apply the source paragraph's direct formatting to the copy just written.

    The copy is made with setString, which carries plain text and nothing else — so a range read
    used to hand the caller a flattened slice where a block quote was indistinguishable from body
    text. Done as a second pass over character OFFSETS rather than by inserting run by run: the
    text is already in place, so there is no insertion-point bookkeeping to get wrong, and a
    failure here degrades to the old plain-text result instead of corrupting the copy.
    """
    try:
        # The copy always appends, so the paragraph just written is the one at the document end.
        para_cursor = temp_text.createTextCursor()
        para_cursor.gotoEnd(False)
        para_cursor.gotoStartOfParagraph(False)
        para_start = para_cursor.getStart()
    except Exception:
        log.debug("_paint_direct_formatting: paragraph start skipped", exc_info=True)
        return

    try:
        para_cursor.gotoEndOfParagraph(True)
        _copy_properties(para, para_cursor, _COPIED_PARA_PROPERTIES, style)
    except Exception:
        # A refused Para* used to return here and skip the Char* loop, dropping bold/colour
        # for the whole range — the reason the temp-doc path exists. Log and keep painting.
        log.debug("_paint_direct_formatting: paragraph properties skipped", exc_info=True)

    offset = 0  # position within the visible (tracked-deletions removed) paragraph text
    for portion, chunk in portions:
        chunk_start, chunk_end = offset, offset + len(chunk)
        offset = chunk_end
        lo, hi = max(chunk_start, trim_start), min(chunk_end, trim_end)
        if lo >= hi:
            continue  # portion lies outside the requested range
        try:
            run = temp_text.createTextCursorByRange(para_start)
            run.goRight(lo - trim_start, False)
            run.goRight(hi - lo, True)
            _copy_properties(portion, run, _COPIED_CHAR_PROPERTIES, style)
        except Exception:
            log.debug("_paint_direct_formatting: portion skipped", exc_info=True)
            continue


def _hyperlink_urls(portions: list[tuple[Any, str]], trim_start: int, trim_end: int):
    """One URL per hyperlink run inside the copied window, in order.

    The XHTML filter emits one ``<a>`` per run, not per text portion, and it
    rewrites the fragment. A bold split or a preserve-format replace leaves two
    portions with the same URL and one anchor; writing every portion would put
    the extra URL on the next anchor. A portion with no URL separates runs, so
    two copies of the same URL with plain text between them stay two anchors.
    """
    urls = []
    previous = None
    offset = 0
    for portion, chunk in portions:
        chunk_start, chunk_end = offset, offset + len(chunk)
        offset = chunk_end
        if max(chunk_start, trim_start) >= min(chunk_end, trim_end):
            continue
        try:
            url = str(portion.getPropertyValue("HyperLinkURL") or "")
        except Exception:
            url = ""
        if not url:
            previous = None
            continue
        if url == previous:
            continue
        urls.append(url)
        previous = url
    return urls


def _restore_anchor_hrefs(content: str, urls: list[str]):
    """Write *urls* back onto exported anchors. The filter does not keep ``|outline``."""
    if not content or not urls:
        return content
    index = 0

    def _replace(match: re.Match[str]):
        nonlocal index
        if index >= len(urls):
            return match.group(0)
        url = html_escape(urls[index], quote=True)
        index += 1
        return match.group(1) + url + match.group(3)

    return _ANCHOR_HREF_RE.sub(_replace, content)


def _starts_before(text: Any, left: Any, right: Any) -> bool:
    """True when *left* is strictly before *right* (``compareRegionStarts`` == 1)."""
    try:
        return int(text.compareRegionStarts(left, right)) == 1
    except Exception:
        return False


def _starts_at_or_after(text: Any, left: Any, right: Any) -> bool:
    """True when *left* is at or after *right*. A failed compare does not stop the walk."""
    try:
        return int(text.compareRegionStarts(left, right)) != 1
    except Exception:
        return False


def _ends_after(text: Any, left: Any, right: Any) -> bool:
    """True when *left* ends strictly after *right* (``compareRegionEnds`` == -1)."""
    try:
        return int(text.compareRegionEnds(left, right)) == -1
    except Exception:
        return False


def _element_overlaps(text: Any, element: Any, source: Any) -> bool:
    """True when *element* and *source* share a character."""
    try:
        return (
            _starts_before(text, element.getStart(), source.getEnd())
            and _ends_after(text, element.getEnd(), source.getStart())
        )
    except Exception:
        return False


def _selection_cursor(model: Any):
    """The current selection as a cursor in its own text, or None.

    Scope ``selection`` used to turn this into character offsets and walk the
    body from the start. Those offsets and the paragraph walk disagree across
    an index section, so a TOC selection could export a later body paragraph.
    """
    from plugin.doc.text_helpers import _get_writer_selection_positions

    found = _get_writer_selection_positions(model)
    if not found:
        return None
    text, start, end = found
    try:
        cursor = text.createTextCursorByRange(start)
        cursor.gotoRange(end, True)
        return cursor
    except Exception:
        log.debug("_selection_cursor failed", exc_info=True)
        return None


def _trim_to_source(text: Any, element: Any, para_text: str, source: Any):
    """Visible-text window of *element* that lies inside *source*."""
    try:
        start_inside = _starts_before(text, element.getStart(), source.getStart())
        end_inside = _ends_after(text, element.getEnd(), source.getEnd())
    except Exception:
        return 0, len(para_text)
    if not start_inside and not end_inside:
        return 0, len(para_text)
    trim_start = 0
    trim_end = len(para_text)
    if start_inside:
        cur = text.createTextCursorByRange(element.getStart())
        cur.gotoRange(source.getStart(), True)
        trim_start = len(get_string_without_tracked_deletions(cur))
    if end_inside:
        cur = text.createTextCursorByRange(element.getStart())
        cur.gotoRange(source.getEnd(), True)
        trim_end = len(get_string_without_tracked_deletions(cur))
    return trim_start, trim_end


def _range_to_content_via_temp_doc(
    model: Any,
    ctx: Any,
    start: int,
    end: int,
    max_chars: int | None,
    config_svc: Any,
    *,
    include_images: bool = False,
    walk_warnings: list[str] | None = None,
    source_range: Any = None,
):
    """Export a character range, or *source_range* itself, via a hidden temp document.

    *source_range* is the selection. It is copied from that cursor's text, not
    re-found by character offset.
    """
    temp_doc = None
    try:
        ctx.getServiceManager()
        desktop = get_desktop(ctx)
        load_props = (format_mod.create_property_value("Hidden", True),)
        temp_doc = desktop.loadComponentFromURL("private:factory/swriter", "_default", 0, load_props)
        if not temp_doc or not hasattr(temp_doc, "getText"):
            return ""

        temp_text = temp_doc.getText()
        temp_cursor = temp_text.createTextCursor()
        style_cache = {}
        text = source_range.getText() if source_range is not None else model.getText()
        enum = text.createEnumeration()
        first_para = True
        added_any = False
        copied_urls = []
        copied_ruby = []
        # One cursor walking forward. Selecting back to the document start for
        # every paragraph copied a growing prefix (quadratic — the selection
        # read that hung on a long file). The gap since the previous element
        # is the same offset the old prefix measurement produced.
        walker = text.createTextCursor()
        walker.gotoStart(False)
        running = 0

        while enum.hasMoreElements():
            el = enum.nextElement()
            if not hasattr(el, "getString"):
                continue
            if source_range is not None and _starts_at_or_after(text, el.getStart(), source_range.getEnd()):
                break
            try:
                style = el.getPropertyValue("ParaStyleName")
            except Exception:
                style = ""
            # Same _visible_portions walk as get_string_without_tracked_deletions so
            # paint offsets match the helper string. Still walk here (not just the
            # helper) because we need the portion objects to copy Char* properties.
            portions = list(_visible_portions(el, truncated_out=walk_warnings))
            para_text = "".join(chunk for _unused, chunk in portions)
            style = style or ""
            if source_range is not None:
                if not _element_overlaps(text, el, source_range):
                    continue
                trim_start, trim_end = _trim_to_source(text, el, para_text, source_range)
                para_text = para_text[trim_start:trim_end]
            else:
                gap = text.createTextCursorByRange(walker.getStart())
                try:
                    gap.gotoRange(el.getStart(), True)
                    running += len(get_string_without_tracked_deletions(gap))
                except Exception:
                    log.debug("_range_to_content_via_temp_doc: paragraph offset gap skipped", exc_info=True)
                try:
                    walker.gotoRange(el.getStart(), False)
                except Exception:
                    log.debug("_range_to_content_via_temp_doc: offset walker skipped", exc_info=True)
                para_start = running
                para_end = para_start + len(para_text)
                if para_end <= start or para_start >= end:
                    continue
                # The window in the paragraph's own (tracked-deletions removed) coordinates. Kept even
                # when nothing is trimmed: _paint_direct_formatting indexes portions with it.
                trim_start, trim_end = 0, len(para_text)
                if para_start < start or para_end > end:
                    trim_start = max(0, start - para_start)
                    trim_end = len(para_text) - max(0, para_end - end)
                    para_text = para_text[trim_start:trim_end]

            if first_para:
                temp_cursor.gotoStart(False)
                temp_cursor.setString(para_text)
                temp_cursor.setPropertyValue("ParaStyleName", style)
                first_para = False
            else:
                temp_cursor.gotoEnd(False)
                temp_text.insertControlCharacter(temp_cursor, _PARAGRAPH_BREAK, False)
                # After insertControlCharacter the cursor is still before the break, not in the
                # new paragraph. Move into it before setting style/content, otherwise setString
                # clobbers the previous paragraph instead of filling the new one.
                temp_cursor.gotoNextParagraph(False)
                temp_cursor.gotoEndOfParagraph(True)
                temp_cursor.setPropertyValue("ParaStyleName", style)
                temp_cursor.setString(para_text)
            _paint_direct_formatting(el, portions, temp_text, trim_start, trim_end,
                                      _source_style(model, style, style_cache))
            copied_urls.extend(_hyperlink_urls(portions, trim_start, trim_end))
            # RubyText lives on the empty Ruby start mark, not the Text run, so
            # _COPIED_CHAR_PROPERTIES cannot recreate ruby on the temp doc.
            # Collect source spans and rewrite the exported HTML after XHTML.
            copied_ruby.extend(_ruby_spans_in_window(el, trim_start, trim_end))
            added_any = True

        if not added_any:
            return ""

        try:
            xhtml = _export_xhtml(temp_doc, config_svc)
            parents, overrides, _unused_ruby = _autostyle_maps(temp_doc, config_svc)
            content = xhtml_post.xhtml_to_semantic_html(xhtml, parents, overrides)
        except Exception:
            log.exception("_range_to_content_via_temp_doc (XHTML) failed; falling back to StarWriter")
            filter_name, _unused = format_mod._get_format_props(config_svc)
            with format_mod._with_temp_buffer(None, config_svc) as (path, file_url):
                props = (format_mod.create_property_value("FilterName", filter_name),)
                temp_doc.storeToURL(file_url, props)
                with open(path, "r", encoding="utf-8", errors="replace") as f:
                    content = f.read()
            content = format_mod._strip_html_boilerplate(content)
        content = _restore_anchor_hrefs(content, copied_urls)
        content = _apply_image_export_options(content, include_images=include_images)
        content = _inject_exported_math_tex(model, ctx, content)
        content = inject_ruby_into_html(content, copied_ruby)
        if max_chars and len(content) > max_chars:
            content = content[:max_chars] + "\n\n[... truncated ...]"
        return content
    except Exception:
        log.exception("_range_to_content_via_temp_doc failed")
        return ""
    finally:
        if temp_doc is not None:
            try:
                temp_doc.close(True)
            except Exception:
                pass



def document_to_content(
    model: Any,
    ctx: Any,
    services: Any,
    max_chars: int | None = None,
    scope: str = "full",
    range_start: int | None = None,
    range_end: int | None = None,
    *,
    include_images: bool = False,
    walk_warnings: list[str] | None = None,
):
    """Export a Writer document (or part of it) as HTML.

    Args:
        model: UNO document model.
        ctx: UNO component context.
        services: ServiceRegistry.
        max_chars: Truncate result to this length.
        scope: ``'full'``, ``'selection'``, or ``'range'``.
        range_start: Character offset start (for scope ``'range'``).
        range_end: Character offset end (for scope ``'range'``).
        include_images: When False (default), strip ``data:image`` base64 from export; external img URLs kept.

    Returns:
        Content string.
    """
    t0 = time.perf_counter()
    log.debug("document_to_content: start scope=%r max_chars=%r include_images=%s", scope, max_chars, include_images)
    config_svc = services.get("config") if services else None

    def _done(content: str, path: str) -> str:
        # Hang diagnosis: if chat stuck on get_document_content, these phase logs name the slow step.
        log.debug(
            "document_to_content: done path=%s scope=%r content_len=%d total_ms=%.1f",
            path,
            scope,
            len(content) if isinstance(content, str) else -1,
            (time.perf_counter() - t0) * 1000.0,
        )
        return content

    if scope == "selection":
        # Copy the selection's own paragraphs. Character offsets from
        # get_selection_range and this walk disagree across an index section,
        # and measuring each paragraph from the document start hung on a long file.
        source = _selection_cursor(model)
        if source is None:
            return _done("", "selection")
        return _done(
            _range_to_content_via_temp_doc(
                model, ctx, 0, 0, max_chars, config_svc,
                include_images=include_images, walk_warnings=walk_warnings,
                source_range=source),
            "selection",
        )

    if scope == "range":
        start = int(range_start) if range_start is not None else 0
        end = int(range_end) if range_end is not None else 0
        doc_len = services.document.get_document_length(model) if services else 0
        start = max(0, min(start, doc_len))
        end = min(end, doc_len)
        return _done(
            _range_to_content_via_temp_doc(
                model, ctx, start, end, max_chars, config_svc,
                include_images=include_images, walk_warnings=walk_warnings),
            "range",
        )

    # scope == "full" — preferred: XHTML (+ flat-ODF parent map) -> semantic data-lo-style.
    try:
        t_phase = time.perf_counter()
        xhtml = _export_xhtml(model, config_svc)
        log.debug(
            "document_to_content: phase=_export_xhtml elapsed_ms=%.1f xhtml_len=%d",
            (time.perf_counter() - t_phase) * 1000.0,
            len(xhtml) if isinstance(xhtml, str) else -1,
        )
        t_phase = time.perf_counter()
        parents, overrides, fodt_has_ruby = _autostyle_maps(model, config_svc)
        log.debug(
            "document_to_content: phase=_autostyle_maps elapsed_ms=%.1f parents=%d overrides=%d ruby=%s",
            (time.perf_counter() - t_phase) * 1000.0,
            len(parents) if isinstance(parents, dict) else -1,
            len(overrides) if isinstance(overrides, dict) else -1,
            fodt_has_ruby,
        )
        t_phase = time.perf_counter()
        content = xhtml_post.xhtml_to_semantic_html(xhtml, parents, overrides)
        content = _apply_image_export_options(content, include_images=include_images)
        content = _inject_exported_math_tex(model, ctx, content)
        content = _inject_ruby_from_model(model, content, fodt_has_ruby)
        if max_chars and len(content) > max_chars:
            content = content[:max_chars] + "\n\n[... truncated ...]"
        log.debug(
            "document_to_content: phase=postprocess elapsed_ms=%.1f content_len=%d",
            (time.perf_counter() - t_phase) * 1000.0,
            len(content),
        )
        return _done(content, "xhtml")
    except Exception:
        log.exception("document_to_content (full, XHTML) failed; falling back to StarWriter")

    # Fallback: legacy StarWriter export (so reads never hard-fail).
    try:
        t_phase = time.perf_counter()
        filter_name, _unused = format_mod._get_format_props(config_svc)
        with format_mod._with_temp_buffer(None, config_svc) as (path, file_url):
            props = (format_mod.create_property_value("FilterName", filter_name),)
            model.storeToURL(file_url, props)
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                content = f.read()
            content = format_mod._strip_html_boilerplate(content)
            content = _apply_image_export_options(content, include_images=include_images)
            content = _inject_exported_math_tex(model, ctx, content)
            content = _inject_ruby_from_model(model, content, None)
            if max_chars and len(content) > max_chars:
                content = content[:max_chars] + "\n\n[... truncated ...]"
            log.debug(
                "document_to_content: phase=starwriter_fallback elapsed_ms=%.1f content_len=%d",
                (time.perf_counter() - t_phase) * 1000.0,
                len(content),
            )
            return _done(content, "starwriter")
    except Exception:
        log.exception("document_to_content (full) failed")
        return _done("", "failed")


def _supports_service(obj: Any, name: str):
    try:
        return bool(obj.supportsService(name))
    except Exception:
        return False


def _xtext_has_tables(text_obj: Any):
    try:
        enum = text_obj.createEnumeration()
    except Exception:
        return False
    while enum.hasMoreElements() is True:
        try:
            el = enum.nextElement()
        except Exception:
            break
        if _supports_service(el, "com.sun.star.text.TextTable"):
            return True
    return False


def _whole_xtext_range(text_obj: Any):
    cursor = text_obj.createTextCursor()
    cursor.gotoStart(False)
    cursor.gotoEnd(True)
    return cursor


def _goto_doc_end(doc: Any) -> None:
    try:
        doc.getCurrentController().getViewCursor().gotoEnd(False)
    except Exception:
        pass


def _paste_range(src_doc: Any, rng: Any, dest_doc: Any):
    """Copy a range via the document transferable (fields, images, char format).

    Selecting a header/footer *table* and pasting this way drops the table
    (probed: ``insertTransferable`` yields an empty dest). Paragraphs,
    page-number fields, and AS_CHARACTER images survive.
    """
    try:
        src_ctrl = src_doc.getCurrentController()
        src_ctrl.select(rng)
        xfer = src_ctrl.getTransferable()
        dest_ctrl = dest_doc.getCurrentController()
        _goto_doc_end(dest_doc)
        dest_ctrl.insertTransferable(xfer)
        return True
    except Exception:
        log.debug("_paste_range failed", exc_info=True)
        return False


def _copy_field_into(dest_doc: Any, dest_text: Any, dest_cursor: Any, src_field: Any):
    """Recreate *src_field* in *dest_doc* (used for table-cell fields)."""
    services = []
    try:
        services = [
            s for s in src_field.getSupportedServiceNames()
            if str(s).startswith("com.sun.star.text.textfield.")
        ]
    except Exception:
        return False
    if not services:
        return False
    svc = sorted(services, key=len)[-1]
    try:
        new_field = dest_doc.createInstance(svc)
    except Exception:
        return False
    for prop in ("NumberingType", "PageNumberType", "IsDate", "IsFixed", "Format"):
        try:
            new_field.setPropertyValue(prop, src_field.getPropertyValue(prop))
        except Exception:
            pass
    try:
        dest_text.insertTextContent(dest_cursor, new_field, False)
        return True
    except Exception:
        log.debug("_copy_field_into failed", exc_info=True)
        return False


def _copy_cell_xtext(src_doc: Any, src_cell: Any, dest_doc: Any, dest_cell: Any) -> None:
    """Copy one table cell: text + fields. Images in cells are not copied here."""
    dest_text = dest_cell
    try:
        dest_cell.setString("")
    except Exception:
        pass
    dest_cursor = dest_text.createTextCursor()
    dest_cursor.gotoStart(False)
    try:
        para_enum = src_cell.createEnumeration()
    except Exception:
        dest_cell.setString(src_cell.getString())
        return
    first_para = True
    while para_enum.hasMoreElements() is True:
        try:
            para = para_enum.nextElement()
        except Exception:
            break
        if _supports_service(para, "com.sun.star.text.TextTable"):
            # Recreate the nested table inside dest_cell (same walk as body _copy_table).
            _copy_table(src_doc, para, dest_doc, dest_cell)
            dest_cursor = dest_text.createTextCursor()
            dest_cursor.gotoEnd(False)
            first_para = False
            continue
        if not first_para:
            try:
                dest_text.insertControlCharacter(dest_cursor, _PARAGRAPH_BREAK, False)
                dest_cursor.gotoNextParagraph(False)
            except Exception:
                pass
        first_para = False
        try:
            portions = para.createEnumeration()
        except Exception:
            try:
                dest_text.insertString(dest_cursor, para.getString(), False)
            except Exception:
                pass
            continue
        while portions.hasMoreElements() is True:
            try:
                portion = portions.nextElement()
                kind = portion.getPropertyValue("TextPortionType")
            except Exception:
                break
            if kind == "TextField":
                try:
                    field = portion.getPropertyValue("TextField")
                except Exception:
                    continue
                _copy_field_into(dest_doc, dest_text, dest_cursor, field)
            else:
                try:
                    chunk = portion.getString()
                except Exception:
                    chunk = ""
                if chunk:
                    dest_text.insertString(dest_cursor, chunk, False)


def _writer_cell_position(name: str):
    """Parse a Writer cell name the way ``SwXTextTable::GetCellPosition`` does.

    Writer letters are base 52 (A–Z, a–z, then AA…). Spreadsheet ``parse_a1``
    uppercases and treats AA as column 26; do not use it here. This parser is
    for the delete-guard / HTML-copy band only — not to rebuild a read matrix.

    LibreOffice: ``sw/source/core/unocore/unotbl.cxx`` ``GetCellPosition``.
    Row is ``o3tl::toInt32`` on the substring from the first digit — that
    parse stops at the first non-digit, so split-cell ``A1.1.1`` is row 0
    (same band as ``A1``). ``int()`` on the whole tail would raise.

    Lives here because LibrePy ships ``html_export`` and not
    ``specialized.tables``.
    """
    if not name:
        return None
    n_len = len(name)
    n_row_pos = 0
    while n_row_pos < n_len:
        ch = name[n_row_pos]
        if "0" <= ch <= "9":
            break
        n_row_pos += 1
    if n_row_pos <= 0 or n_row_pos >= n_len:
        return None
    n_col_idx = 0
    for i in range(n_row_pos):
        n_col_idx *= 52
        if i < n_row_pos - 1:
            n_col_idx += 1
        c_char = name[i]
        if "A" <= c_char <= "Z":
            n_col_idx += ord(c_char) - ord("A")
        elif "a" <= c_char <= "z":
            n_col_idx += 26 + ord(c_char) - ord("a")
        else:
            return None
    n_digits_end = n_row_pos
    while n_digits_end < n_len and "0" <= name[n_digits_end] <= "9":
        n_digits_end += 1
    if n_digits_end == n_row_pos:
        return None
    n_row = int(name[n_row_pos:n_digits_end]) - 1
    if n_row < 0 or n_col_idx < 0:
        return None
    return n_col_idx, n_row


def _writer_table_copy_layout(table: Any):
    """Dest initialize size and named cells so HTML copy does not drop D2.

    Dest rows/cols are the max of ``getRows()``/``getColumns()`` and the Writer
    name coordinates. Split-cell suffixes (``A1.1.1``) map to the parent box
    and must not inflate dest size. The parser is not used to rebuild a
    read-path matrix.
    """
    try:
        rows = int(table.getRows().getCount())
        cols = int(table.getColumns().getCount())
    except Exception:
        rows, cols = 0, 0
    try:
        names = list(table.getCellNames() or ())
    except Exception:
        names = []
    max_row = max(rows - 1, 0)
    max_col = max(cols - 1, 0)
    for cell_name in names:
        pos = _writer_cell_position(cell_name)
        if pos is None:
            continue
        col_idx, row_idx = pos
        if col_idx > max_col:
            max_col = col_idx
        if row_idx > max_row:
            max_row = row_idx
    return max_row + 1, max_col + 1, names


def _copy_table(src_doc: Any, src_table: Any, dest_doc: Any, dest_text: Any = None) -> None:
    """Recreate *src_table* in *dest_text* (document body if omitted).

    *dest_text* is a cell when copying a nested TextTable; recursion through
    ``_copy_cell_xtext`` then copies inner cells (including further nests).

    Copy by ``getCellNames()`` so a merged banner (``getColumns()`` is the
    first-row box count and can be 1) still copies D2. Dest is sized to the
    max Writer name coordinate so those names exist; merges are not recreated.
    """
    try:
        dest_rows, dest_cols, names = _writer_table_copy_layout(src_table)
    except Exception:
        return
    if dest_rows < 1 or dest_cols < 1:
        return
    dest_table = dest_doc.createInstance("com.sun.star.text.TextTable")
    dest_table.initialize(dest_rows, dest_cols)
    dest_xtext = dest_text if dest_text is not None else dest_doc.getText()
    dest_xtext.insertTextContent(dest_xtext.getEnd(), dest_table, False)
    if names:
        for cell_name in names:
            try:
                src_cell = src_table.getCellByName(cell_name)
                dest_cell = dest_table.getCellByName(cell_name)
            except Exception:
                continue
            _copy_cell_xtext(src_doc, src_cell, dest_doc, dest_cell)
    else:
        # No name list: last-resort position walk (same first-row bound as before).
        for row in range(dest_rows):
            for col in range(dest_cols):
                try:
                    src_cell = src_table.getCellByPosition(col, row)
                    dest_cell = dest_table.getCellByPosition(col, row)
                except Exception:
                    continue
                _copy_cell_xtext(src_doc, src_cell, dest_doc, dest_cell)
    _goto_doc_end(dest_doc)


def _copy_xtext_by_portions(src_doc: Any, src_text: Any, dest_doc: Any) -> None:
    """Copy paragraphs/fields without the view transferable.

    Needed when ``select()`` on ``HeaderText`` pastes nothing because the
    view is on the first page (``FirstIsShared=False``).
    """
    dest_text = dest_doc.getText()
    dest_text.setString("")
    dest_cursor = dest_text.createTextCursor()
    dest_cursor.gotoStart(False)
    try:
        enum = src_text.createEnumeration()
    except Exception:
        dest_text.setString(src_text.getString() if src_text else "")
        return
    first_para = True
    while enum.hasMoreElements() is True:
        try:
            el = enum.nextElement()
        except Exception:
            break
        if _supports_service(el, "com.sun.star.text.TextTable"):
            _copy_table(src_doc, el, dest_doc)
            _goto_doc_end(dest_doc)
            dest_cursor = dest_text.createTextCursor()
            dest_cursor.gotoEnd(False)
            first_para = False
            continue
        if not first_para:
            try:
                dest_text.insertControlCharacter(dest_cursor, _PARAGRAPH_BREAK, False)
                dest_cursor.gotoNextParagraph(False)
            except Exception:
                pass
        first_para = False
        try:
            portions = el.createEnumeration()
        except Exception:
            try:
                dest_text.insertString(dest_cursor, el.getString(), False)
            except Exception:
                pass
            continue
        while portions.hasMoreElements() is True:
            try:
                portion = portions.nextElement()
                kind = portion.getPropertyValue("TextPortionType")
            except Exception:
                break
            if kind == "TextField":
                try:
                    field = portion.getPropertyValue("TextField")
                except Exception:
                    continue
                _copy_field_into(dest_doc, dest_text, dest_cursor, field)
            else:
                try:
                    chunk = portion.getString()
                except Exception:
                    chunk = ""
                if chunk:
                    dest_text.insertString(dest_cursor, chunk, False)


def _copy_xtext_into_doc(src_doc: Any, src_text: Any, dest_doc: Any) -> None:
    """Copy *src_text* (body, header, footer, cell) into *dest_doc*'s body.

    Paragraphs use the transferable so fields and AS_CHARACTER images survive.
    Tables are recreated — LO's transferable drops a header table (probed).
    When the view is on the first page, ``select(HeaderText)`` pastes empty;
    fall back to a portion walk so shared vs first-page regions still export.
    """
    dest_doc.getText().setString("")
    src_plain = (src_text.getString() if src_text else "") or ""
    if not _xtext_has_tables(src_text):
        pasted = _paste_range(src_doc, _whole_xtext_range(src_text), dest_doc)
        dest_plain = dest_doc.getText().getString() or ""
        if pasted and dest_plain.strip():
            return
        if src_plain.strip():
            _copy_xtext_by_portions(src_doc, src_text, dest_doc)
        return
    try:
        enum = src_text.createEnumeration()
    except Exception:
        dest_doc.getText().setString(src_plain)
        return
    while enum.hasMoreElements() is True:
        try:
            el = enum.nextElement()
        except Exception:
            break
        if _supports_service(el, "com.sun.star.text.TextTable"):
            _copy_table(src_doc, el, dest_doc)
        else:
            if not _paste_range(src_doc, el, dest_doc):
                try:
                    dest_doc.getText().insertString(dest_doc.getText().getEnd(), el.getString(), False)
                except Exception:
                    pass
    dest_plain = dest_doc.getText().getString() or ""
    if not dest_plain.strip() and src_plain.strip():
        _copy_xtext_by_portions(src_doc, src_text, dest_doc)


def _open_hidden_writer(ctx: Any):
    desktop = get_desktop(ctx)
    load_props = (format_mod.create_property_value("Hidden", True),)
    return desktop.loadComponentFromURL("private:factory/swriter", "_blank", 0, load_props)


def xtext_to_content(
    text_obj: Any,
    model: Any,
    ctx: Any,
    services: Any = None,
    *,
    include_images: bool = True,
    max_chars: int | None = None,
):
    """Export an ``XText`` (header, footer, body, cell) via ``document_to_content``.

    Full-document XHTML omits page-style headers/footers, so the region is
    copied into a hidden Writer body's text and run through the same
    XHTML + postprocess stack as ``get_document_content``.
    """
    if text_obj is None:
        return ""
    temp_doc = None
    try:
        temp_doc = _open_hidden_writer(ctx)
        if not temp_doc or not hasattr(temp_doc, "getText"):
            return ""
        _copy_xtext_into_doc(model, text_obj, temp_doc)
        return document_to_content(
            temp_doc,
            ctx,
            services,
            max_chars=max_chars,
            scope="full",
            include_images=include_images,
        )
    except Exception:
        log.exception("xtext_to_content failed")
        return ""
    finally:
        if temp_doc is not None:
            try:
                temp_doc.close(True)
            except Exception:
                pass


