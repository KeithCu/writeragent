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
import zlib
from html import escape as html_escape
from typing import Any, ClassVar, Iterator

from plugin.framework.errors import best_effort
from plugin.doc.text_helpers import (
    get_string_without_tracked_deletions,
    _visible_portions as _shared_visible_portions,
)
from plugin.framework.errors import ToolExecutionError, reraise_if_disposed, uno_read
from plugin.framework.uno_context import new_blank_writer, uno_same
from . import xhtml_style_postprocess as xhtml_post
from . import format as format_mod

log = logging.getLogger("writeragent.writer")
# uno_read default where None is a real UNO result.
_FAILED: Any = object()


class _RubySpan:
    """One CJK ruby (furigana) pair collected from Writer portions.

    ``start`` / ``end`` are offsets in the paragraph's visible text (base only;
    ruby marks have empty ``getString()``). Used to keep range export from
    wrapping a base that was trimmed out of the copied window.
    """

    base: str
    reading: str
    start: int
    end: int
    __slots__: ClassVar[tuple[str, ...]] = ("base", "reading", "start", "end")

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



# LibreOffice's XHTML export gives every heading an empty outline anchor, ``<a id="a__…">``,
# named after the heading's text. A picture anchored inside the heading puts its whole base64
# data into that name, so a heading holding one embedded screenshot came back with a
# 40-65 KB id (twice the picture itself, once in the id and once in src), even with
# include_images=false. Nothing in WriterAgent, nor the agent, uses these ids.
# shorten_outline_anchor_ids gives a long id a short, stable name, in the id and in any
# ``href="#…"`` that points to it.
_MAX_OUTLINE_ANCHOR_ID = 200
_LONG_OUTLINE_ANCHOR_RE = re.compile(r'(\b(?:id|href)="#?)(a__[^"]{%d,})(")' % _MAX_OUTLINE_ANCHOR_ID)


def shorten_outline_anchor_ids(html: str) -> str:
    """Replace outline anchor ids longer than ``_MAX_OUTLINE_ANCHOR_ID`` with ``a__<crc32>``."""
    if not html or "a__" not in html:
        return html
    return _LONG_OUTLINE_ANCHOR_RE.sub(lambda m: "%sa__%08x%s" % (m.group(1), zlib.crc32(m.group(2).encode("utf-8")), m.group(3)), html)


def _apply_image_export_options(content: str, *, include_images: bool) -> str:
    if not content:
        return content
    content = shorten_outline_anchor_ids(content)
    return content if include_images else strip_embedded_image_data(content)


def _ruby_parts(span: Any) -> tuple[str, str]:
    """``(base, reading)`` from a ``_RubySpan`` or a 2-tuple (unit tests)."""
    if isinstance(span, (tuple, list)):
        return span[0], span[1]
    return span.base, span.reading


def _inside_ruby_element(html: str, index: int) -> bool:
    """True when *index* sits inside an existing ``<ruby>…</ruby>``."""
    last_open = html.rfind("<ruby", 0, index)
    if last_open < 0:
        return False
    last_close = html.rfind("</ruby>", 0, index)
    return last_close < last_open


def _index_in_tag(html: str, index: int) -> bool:
    """True when *index* is inside ``<...>`` (attribute text is not body text)."""
    last_lt = html.rfind("<", 0, index)
    if last_lt < 0:
        return False
    last_gt = html.rfind(">", 0, index)
    return last_gt < last_lt


def _find_contiguous(html: str, needle: str, start: int = 0) -> tuple[int, int] | None:
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


def _only_tags_or_ws(fragment: str) -> bool:
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


def _ruby_markup(base: str, reading: str) -> str:
    return "<ruby>%s<rt>%s</rt></ruby>" % (html_escape(base), html_escape(reading))


def inject_ruby_into_html(html: str, spans: list[Any]) -> str:
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


def _iter_xtext_paragraphs(text_obj: Any) -> Iterator[Any]:
    """Yield paragraphs under *text_obj*, descending into table cells."""
    enum = uno_read(lambda: text_obj.createEnumeration(), _FAILED)
    if enum is _FAILED:
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


def _iter_ruby_spans_in_paragraph(para: Any) -> Iterator[_RubySpan]:
    """Yield ``_RubySpan`` for each Ruby start/end pair in *para*.

    Writer stores ruby as ``TextPortionType="Ruby"`` marks: the start has
    ``RubyText`` / ``RubyIsAbove`` / ``RubyAdjust``; the base is a normal
    ``Text`` run; the end mark is empty. ``getString()`` on the paragraph is
    base only — reading is not a text field.
    """
    portion_enum = uno_read(lambda: para.createEnumeration(), _FAILED)
    if portion_enum is _FAILED:
        return
    in_delete = False
    in_ruby = False
    reading = ""
    base_parts: list[str] = []
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
                reading = uno_read(lambda: str(portion.getPropertyValue("RubyText") or ""), "")
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
        chunk = uno_read(lambda: portion.getString() or "", "")
        if not chunk:
            continue
        if in_ruby:
            base_parts.append(chunk)
        offset += len(chunk)


def iter_ruby_spans(text_obj: Any) -> Iterator[_RubySpan]:
    """Document-order ruby pairs under *text_obj* (body or cell), including tables."""
    for para in _iter_xtext_paragraphs(text_obj):
        yield from _iter_ruby_spans_in_paragraph(para)


def _ruby_spans_in_window(para: Any, trim_start: int, trim_end: int) -> list[_RubySpan]:
    """Ruby pairs whose entire base lies inside the copied visible-text window."""
    out = []
    for span in _iter_ruby_spans_in_paragraph(para):
        if span.start >= trim_start and span.end <= trim_end:
            out.append(span)
    return out


def _inject_ruby_from_model(model: Any, content: str, fodt_has_ruby: bool | None = None) -> str:
    """Walk the live model and rewrite *content* when ruby is (or may be) present.

    *fodt_has_ruby* is True/False from the paired FODT sidecar, or None when
    that export failed. False skips the portion walk on the common no-ruby path.
    """
    if not content:
        return content
    if fodt_has_ruby is False:
        return content
    spans = best_effort(lambda: list(iter_ruby_spans(model.getText())), "_inject_ruby_from_model: portion walk failed")
    if spans is None:
        return content
    return inject_ruby_into_html(content, spans)


def _inject_exported_math_tex(model: Any, ctx: Any, content: str) -> str:
    """Replace formula OLE holes with delimited TeX for the model/chat.

    Failures stay in the HTML as a visible fallback; never drop formulas.
    """
    if not content or model is None or ctx is None:
        return content
    def inject() -> str:
        from plugin.writer.math.math_mml_export import inject_math_tex_into_html

        return inject_math_tex_into_html(model, ctx, content)

    return best_effort(inject, "_inject_exported_math_tex failed", content)



def _truncate_html(content: str, max_chars: int) -> str:
    """Cut *content* to *max_chars* without ending inside a tag or an entity.

    The old slice ``content[:max_chars]`` handed the model an unclosed
    ``<a href="…`` or a trailing ``&nb``. The marker stays so
    get_document_content can set ``truncated``.
    """
    window = content[:max_chars]
    last_lt = window.rfind("<")
    last_gt = window.rfind(">")
    if last_lt > last_gt:
        window = window[:last_lt]
    amp = window.rfind("&")
    if amp >= 0 and ";" not in window[amp:]:
        window = window[:amp]
    return window + "\n\n[... truncated ...]"



def _export_xhtml(doc: Any, config_svc: Any) -> str:
    """Export *doc* via the XHTML Writer File filter; return the raw XHTML string."""
    with format_mod._with_temp_buffer(None, config_svc, ext=format_mod.XHTML_EXTENSION) as (path, file_url):
        props = (format_mod.create_property_value("FilterName", format_mod.XHTML_FILTER),)
        doc.storeToURL(file_url, props)
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return f.read()



def _autostyle_maps(doc: Any, config_svc: Any) -> tuple[dict[str, str], dict[str, str], bool | None]:
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
    def read_maps() -> tuple[dict[str, str], dict[str, str], bool | None]:
        with format_mod._with_temp_buffer(None, config_svc, ext=format_mod.FODT_EXTENSION) as (path, file_url):
            props = (format_mod.create_property_value("FilterName", format_mod.FLAT_ODF_FILTER),)
            doc.storeToURL(file_url, props)
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                fodt = f.read()
        return (xhtml_post.extract_autostyle_parents_from_fodt(fodt),
                xhtml_post.extract_autostyle_overrides_from_fodt(fodt),
                "<text:ruby" in fodt)

    failed: tuple[dict[str, str], dict[str, str], bool | None] = ({}, {}, None)
    return best_effort(read_maps, "_autostyle_maps: flat-ODF export failed", failed)



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


def _copy_properties(src: Any, dst: Any, names: tuple[str, ...], style: Any = None) -> None:
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


def _source_style(model: Any, style_name: str, cache: dict[str, Any]) -> Any:
    """The source document's paragraph-style object for *style_name*, or None. Cached per read."""
    if style_name in cache:
        return cache[style_name]
    style = None
    if style_name:
        style = uno_read(lambda: model.getStyleFamilies().getByName("ParagraphStyles").getByName(style_name), None)
    cache[style_name] = style
    return style


def _visible_portions(para: Any, limit: int = _COPY_PORTION_LIMIT, truncated_out: list[str] | None = None) -> Iterator[Any]:
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
    def paragraph_start() -> tuple[Any, Any]:
        # The copy always appends, so the paragraph just written is the one at the document end.
        para_cursor = temp_text.createTextCursor()
        para_cursor.gotoEnd(False)
        para_cursor.gotoStartOfParagraph(False)
        return para_cursor, para_cursor.getStart()

    got = best_effort(paragraph_start, "_paint_direct_formatting: paragraph start skipped")
    if got is None:
        return
    para_cursor, para_start = got

    def paint_paragraph() -> None:
        para_cursor.gotoEndOfParagraph(True)
        _copy_properties(para, para_cursor, _COPIED_PARA_PROPERTIES, style)

    # A refused Para* used to return here and skip the Char* loop, dropping bold/colour
    # for the whole range — the reason the temp-doc path exists. Log and keep painting.
    best_effort(paint_paragraph, "_paint_direct_formatting: paragraph properties skipped")

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


def _hyperlink_urls(portions: list[tuple[Any, str]], trim_start: int, trim_end: int) -> list[str]:
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
        url = uno_read(lambda: str(portion.getPropertyValue("HyperLinkURL") or ""), "")
        if not url:
            previous = None
            continue
        if url == previous:
            continue
        urls.append(url)
        previous = url
    return urls


def _restore_anchor_hrefs(content: str, urls: list[str]) -> str:
    """Write *urls* back onto exported anchors. The filter does not keep ``|outline``."""
    if not content or not urls:
        return content
    index = 0

    def _replace(match: re.Match[str]) -> str:
        nonlocal index
        if index >= len(urls):
            return match.group(0)
        url = html_escape(urls[index], quote=True)
        index += 1
        return match.group(1) + url + match.group(3)

    return _ANCHOR_HREF_RE.sub(_replace, content)


def _starts_before(text: Any, left: Any, right: Any) -> bool:
    """True when *left* is strictly before *right* (``compareRegionStarts`` == 1)."""
    return uno_read(lambda: int(text.compareRegionStarts(left, right)) == 1, False)


def _starts_at_or_after(text: Any, left: Any, right: Any) -> bool:
    """True when *left* is at or after *right*. A failed compare does not stop the walk."""
    try:
        return int(text.compareRegionStarts(left, right)) != 1
    except Exception:
        return False


def _ends_after(text: Any, left: Any, right: Any) -> bool:
    """True when *left* ends strictly after *right* (``compareRegionEnds`` == -1)."""
    return uno_read(lambda: int(text.compareRegionEnds(left, right)) == -1, False)


def _element_overlaps(text: Any, element: Any, source: Any) -> bool:
    """True when *element* and *source* share a character."""
    try:
        return (
            _starts_before(text, element.getStart(), source.getEnd())
            and _ends_after(text, element.getEnd(), source.getStart())
        )
    except Exception:
        return False


def _selection_cursor(model: Any) -> Any | None:
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
    def select() -> Any:
        cursor = text.createTextCursorByRange(start)
        cursor.gotoRange(end, True)
        return cursor

    return best_effort(select, "_selection_cursor failed")


def _apply_temp_para_style(temp_doc: Any, temp_cursor: Any, style: str) -> None:
    """Apply *style* on the scratch paragraph when that style exists there.

    Selection and range export copy each paragraph into a blank
    scratch document and set ``ParaStyleName`` with no check. A custom
    or foreign style is not in that document, so ``setPropertyValue``
    raises and the broad handler returns ``""``, discarding the whole
    selection. Skip the missing style (the scratch default stays) and
    keep copying. One style miss must not discard the range.
    """
    if not style:
        return
    try:
        families = temp_doc.getStyleFamilies()
        para_styles = families.getByName("ParagraphStyles")
        if not para_styles.hasByName(style):
            log.debug(
                "range export: paragraph style %r is not in the scratch document; keeping the default",
                style,
            )
            return
        temp_cursor.setPropertyValue("ParaStyleName", style)
    except Exception:
        log.debug(
            "range export: skipped paragraph style %r on the scratch document",
            style,
            exc_info=True,
        )


def _trim_to_source(text: Any, element: Any, para_text: str, source: Any) -> tuple[int, int]:
    """Visible-text window of *element* that lies inside *source*."""
    # getStart/getEnd do not fail on a live range. Compare helpers already
    # catch. Swallowing a failure here exported the whole paragraph.
    start_inside = _starts_before(text, element.getStart(), source.getStart())
    end_inside = _ends_after(text, element.getEnd(), source.getEnd())
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
) -> str:
    """Export a character range, or *source_range* itself, via a hidden temp document.

    *source_range* is the selection. It is copied from that cursor's text, not
    re-found by character offset.
    """
    temp_doc = None
    try:
        # ctx.getServiceManager() was an unused probe; new_blank_writer is the check.
        # Blank scratch doc: the factory URL honours the user's default template,
        # and this function reads the whole body back (see new_blank_writer).
        temp_doc = new_blank_writer(ctx, target="_default")
        if not temp_doc or not hasattr(temp_doc, "getText"):
            # Bugfix: returning "" made scope=range/selection report ok with
            # empty content when the scratch Writer could not be created
            # (relato #63).
            raise ToolExecutionError(
                "Could not read that part of the document (no scratch document)."
            )

        temp_text = temp_doc.getText()
        temp_cursor = temp_text.createTextCursor()
        style_cache: dict[str, Any] = {}
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
            if _supports_service(el, "com.sun.star.text.TextTable"):
                # Tables are in this enumeration and have no ParaStyleName.
                style = ""
            else:
                # Paragraphs always have ParaStyleName.
                style = el.getPropertyValue("ParaStyleName") or ""
            # Same _visible_portions walk as get_string_without_tracked_deletions so
            # paint offsets match the helper string. Still walk here (not just the
            # helper) because we need the portion objects to copy Char* properties.
            portions = list(_visible_portions(el, truncated_out=walk_warnings))
            para_text = "".join(chunk for _unused, chunk in portions)
            if source_range is not None:
                if not _element_overlaps(text, el, source_range):
                    continue
                trim_start, trim_end = _trim_to_source(text, el, para_text, source_range)
                para_text = para_text[trim_start:trim_end]
            else:
                gap = text.createTextCursorByRange(walker.getStart())
                # An offset failure must not shift the window. Swallowing it
                # mapped every later paragraph to the wrong range and returned
                # that slice as ok.
                gap.gotoRange(el.getStart(), True)
                running += len(get_string_without_tracked_deletions(gap))
                walker.gotoRange(el.getStart(), False)
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
                _apply_temp_para_style(temp_doc, temp_cursor, style)
                first_para = False
            else:
                temp_cursor.gotoEnd(False)
                temp_text.insertControlCharacter(temp_cursor, _PARAGRAPH_BREAK, False)
                # After insertControlCharacter the cursor is still before the break, not in the
                # new paragraph. Move into it before setting style/content, otherwise setString
                # clobbers the previous paragraph instead of filling the new one.
                temp_cursor.gotoNextParagraph(False)
                temp_cursor.gotoEndOfParagraph(True)
                _apply_temp_para_style(temp_doc, temp_cursor, style)
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
        except Exception as e:
            # A closed document must not start a second export.
            reraise_if_disposed(e)
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
            content = _truncate_html(content, max_chars)
        return content
    except Exception as e:
        # Disposal is not "could not read that part". A ToolExecutionError
        # raised above (missing scratch doc) must not be wrapped again.
        reraise_if_disposed(e)
        if isinstance(e, ToolExecutionError):
            raise
        # Same silent "ok with empty content" as the full read (relato #63): say why instead.
        raise ToolExecutionError("Could not read that part of the document (%s)." % _reason(e)) from e
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
) -> str:
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
        # No clamp to the document length: the walk below skips paragraphs past the end on its
        # own. Clamping to CharacterCount cut a read at the end short ("o dan"), and measuring
        # the real length here would walk the whole body a second time per range read.
        start = max(0, start)
        return _done(
            _range_to_content_via_temp_doc(
                model, ctx, start, end, max_chars, config_svc,
                include_images=include_images, walk_warnings=walk_warnings),
            "range",
        )

    # scope == "full" — preferred: XHTML (+ flat-ODF parent map) -> semantic data-lo-style.
    xhtml_error: Exception | None = None
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
        _raise_if_empty_export(model, content, "the XHTML export came back empty")
        if max_chars and len(content) > max_chars:
            content = _truncate_html(content, max_chars)
        log.debug(
            "document_to_content: phase=postprocess elapsed_ms=%.1f content_len=%d",
            (time.perf_counter() - t_phase) * 1000.0,
            len(content),
        )
        return _done(content, "xhtml")
    except Exception as e:
        # A closed document must not start a second export.
        reraise_if_disposed(e)
        xhtml_error = e
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
            _raise_if_empty_export(model, content, "the HTML export came back empty")
            if max_chars and len(content) > max_chars:
                content = _truncate_html(content, max_chars)
            log.debug(
                "document_to_content: phase=starwriter_fallback elapsed_ms=%.1f content_len=%d",
                (time.perf_counter() - t_phase) * 1000.0,
                len(content),
            )
            return _done(content, "starwriter")
    except Exception as e:
        # Disposal is not a double export failure.
        reraise_if_disposed(e)
        # When both exports fail, returning "" makes get_document_content
        # answer status ok with empty content (relato #63: document_length
        # 57003, content ""), so neither the agent nor the log reader can
        # tell why. The failure surfaces as a tool error that names both
        # reasons (a full /tmp, a filter error, ...).
        raise ToolExecutionError(
            "Could not read the document: the XHTML export failed (%s) and the HTML export "
            "failed too (%s)." % (_reason(xhtml_error), _reason(e))) from e


def _reason(error: Exception | None) -> str:
    """Short text for an export failure: the message, or the exception type when it has none
    (UNO exceptions often have an empty message)."""
    if error is None:
        return "unknown error"
    return str(error).strip() or type(error).__name__


def _raise_if_empty_export(model: Any, content: str, why: str) -> None:
    """Raise when an export produced nothing although the document has text."""
    if content and content.strip():
        return
    has_text = uno_read(lambda: bool(model.getText().getString().strip()), True)
    if has_text:
        raise ToolExecutionError(why)


def _supports_service(obj: Any, name: str) -> bool:
    return uno_read(lambda: bool(obj.supportsService(name)), False)


def _goto_doc_end(doc: Any) -> None:
    try:
        doc.getCurrentController().getViewCursor().gotoEnd(False)
    except Exception:
        pass


def _copy_field_into(dest_doc: Any, dest_text: Any, dest_cursor: Any, src_field: Any) -> bool:
    """Recreate *src_field* in *dest_doc* (used for table-cell fields)."""
    services = uno_read(lambda: [s for s in src_field.getSupportedServiceNames() if str(s).startswith("com.sun.star.text.textfield.")], _FAILED)
    if services is _FAILED:
        return False
    if not services:
        return False
    svc = sorted(services, key=len)[-1]
    new_field = uno_read(lambda: dest_doc.createInstance(svc), _FAILED)
    if new_field is _FAILED:
        return False
    # Page-style clone copies header/footer fields through this helper
    # (test_copy_page_style_clones_next_page_number_and_roman). The old XHTML
    # path re-injected data-offset; this tuple omitted Offset, so a NEXT field
    # with Offset 1 was cloned as Offset 0. Properties the service does not
    # have still fail the try/except below and are skipped.
    for prop in ("NumberingType", "SubType", "Offset", "IsDate", "IsFixed", "Format"):
        try:
            new_field.setPropertyValue(prop, src_field.getPropertyValue(prop))
        except Exception as e:
            log.debug("_copy_field_into failed to set property %r: %s", prop, e)
    def insert() -> bool:
        dest_text.insertTextContent(dest_cursor, new_field, False)
        return True

    return best_effort(insert, "_copy_field_into failed", False)


def _copy_cell_xtext(src_doc: Any, src_cell: Any, dest_doc: Any, dest_cell: Any) -> None:
    """Copy one table cell: text + fields. Images in cells are not copied here."""
    dest_text = dest_cell
    # Fresh cell; setString cannot fail here.
    dest_cell.setString("")
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
                chunk = uno_read(lambda: portion.getString(), "")
                if chunk:
                    dest_text.insertString(dest_cursor, chunk, False)


def _writer_cell_position(name: str) -> tuple[int, int] | None:
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


def _writer_table_copy_layout(table: Any) -> tuple[int, int, list[str]]:
    """Dest initialize size and named cells so HTML copy does not drop D2.

    Dest rows/cols are the max of ``getRows()``/``getColumns()`` and the Writer
    name coordinates. Split-cell suffixes (``A1.1.1``) map to the parent box
    and must not inflate dest size. The parser is not used to rebuild a
    read-path matrix.
    """
    rows, cols = uno_read(lambda: (int(table.getRows().getCount()), int(table.getColumns().getCount())), (0, 0))
    names: list[str] = uno_read(lambda: list(table.getCellNames() or []), [])
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
    # _writer_table_copy_layout already returns a size when a metric read fails.
    dest_rows, dest_cols, names = _writer_table_copy_layout(src_table)
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
            elif kind == "Text":
                chunk = uno_read(lambda: portion.getString(), "")
                if not chunk:
                    continue
                start = dest_cursor.getStart()
                dest_text.insertString(dest_cursor, chunk, False)
                run = dest_text.createTextCursorByRange(start)
                run.gotoRange(dest_cursor.getEnd(), True)
                _copy_properties(portion, run, _COPIED_CHAR_PROPERTIES)
                dest_cursor.gotoRange(run.getEnd(), False)
            else:
                chunk = uno_read(lambda: portion.getString(), "")
                if chunk:
                    dest_text.insertString(dest_cursor, chunk, False)
        para_cursor = dest_text.createTextCursor()
        para_cursor.gotoEnd(False)
        para_cursor.gotoStartOfParagraph(False)
        para_cursor.gotoEndOfParagraph(True)
        _copy_properties(el, para_cursor, _COPIED_PARA_PROPERTIES)
        dest_cursor.gotoRange(para_cursor.getEnd(), False)


def _copy_flow_into_xtext(doc: Any, src_text: Any, dest_text: Any) -> int:  # pyright: ignore[reportUnusedFunction]
    """Copy paragraphs, tables, fields, and direct char/para props inside *doc*.

    Used by page-style clone. Does not select the source, does not use the
    clipboard, and does not open a scratch Writer or run XHTML / flat ODF.
    Returns how many fields were inserted.

    Bugfix: xtext_to_content used to controller.select the live header or
    footer, getTransferable / insertTransferable into a hidden Writer, export
    XHTML (in-process libxslt, stderr "XSL Vendor: 'libxslt'") and flat ODF,
    then temp_doc.close(True) while that selection and clipboard could still
    alias header nodes, fields, and titled draw frames. A titled AT_PARAGRAPH
    graphic makes the ODF export hit the default rule in LibreOffice
    filter/source/xslt/odf2xhtml/export/common/body.xsl (match="*"), which
    logs "Using default element rule for ODF element 'svg:title'.". glibc
    then reported "malloc(): unsorted double linked list corrupted", or
    soffice died SIGSEGV 139 / VCL SalAbort. Python usually saw that on a
    later pages.getByName ("Binary URP bridge disposed"), sometimes many
    tests later in the pooled soffice (GHA 38526445257, 38584920511,
    38584636315). This walk never selects and never exports.
    """
    cursor = dest_text.createTextCursor()
    cursor.gotoStart(False)
    cursor.gotoEnd(True)
    cursor.setString("")
    cursor.gotoStart(False)
    fields_copied = 0
    try:
        enum = src_text.createEnumeration()
    except Exception:
        try:
            dest_text.insertString(cursor, src_text.getString() if src_text else "", False)
        except Exception:
            pass
        return 0
    if not enum:
        return 0

    first_para = True
    while enum.hasMoreElements() is True:
        try:
            el = enum.nextElement()
        except Exception:
            break
        if _supports_service(el, "com.sun.star.text.TextTable"):
            _copy_table(doc, el, doc, dest_text)
            cursor = dest_text.createTextCursor()
            cursor.gotoEnd(False)
            first_para = False
            continue
        if not first_para:
            try:
                dest_text.insertControlCharacter(cursor, _PARAGRAPH_BREAK, False)
            except Exception:
                pass
            try:
                cursor.gotoNextParagraph(False)
            except Exception:
                pass
        first_para = False
        try:
            portions = el.createEnumeration()
        except Exception:
            try:
                dest_text.insertString(cursor, el.getString() if el else "", False)
            except Exception:
                pass
            para_cursor = dest_text.createTextCursor()
            para_cursor.gotoEnd(False)
            para_cursor.gotoStartOfParagraph(False)
            para_cursor.gotoEndOfParagraph(True)
            _copy_properties(el, para_cursor, _COPIED_PARA_PROPERTIES)
            cursor.gotoRange(para_cursor.getEnd(), False)
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
                if _copy_field_into(doc, dest_text, cursor, field):
                    fields_copied += 1
                cursor.gotoEnd(False)
            elif kind == "Text":
                chunk = uno_read(lambda: portion.getString(), "")
                if not chunk:
                    continue
                start = cursor.getStart()
                dest_text.insertString(cursor, chunk, False)
                run = dest_text.createTextCursorByRange(start)
                run.gotoRange(cursor.getEnd(), True)
                _copy_properties(portion, run, _COPIED_CHAR_PROPERTIES)
                cursor.gotoRange(run.getEnd(), False)
            else:
                continue
        para_cursor = dest_text.createTextCursor()
        para_cursor.gotoEnd(False)
        para_cursor.gotoStartOfParagraph(False)
        para_cursor.gotoEndOfParagraph(True)
        _copy_properties(el, para_cursor, _COPIED_PARA_PROPERTIES)
        cursor.gotoRange(para_cursor.getEnd(), False)

    return fields_copied


# Same list as page._PAGE_GRAPHIC_PROPS. The temp Writer does not offer every
# orient property; the per-property except is that miss, not a missing Graphic.
_ANCHORED_GRAPHIC_PROPS = (
    "AnchorType", "Graphic", "Width", "Height",
    "HoriOrient", "HoriOrientPosition", "HoriOrientRelation",
    "VertOrient", "VertOrientPosition", "VertOrientRelation",
    "HyperLinkURL", "HyperLinkTarget", "Title", "Description",
)


def _anchor_offset(src_text: Any, anchor: Any) -> int:
    """Visible-text offset of *anchor* in *src_text*."""
    measure = src_text.createTextCursor()
    measure.gotoStart(False)
    measure.gotoRange(anchor.getStart(), True)
    return len(measure.getString() or "")


def _copy_anchored_graphics(src_doc: Any, src_text: Any, dest_doc: Any) -> None:
    """Clone TextGraphicObjects anchored in *src_text* onto *dest_doc*.

    The portion walk does not carry Graphic. controller.select + transferable
    into the scratch Writer is the pattern _copy_flow_into_xtext removed
    (heap corruption / SIGSEGV on the following XHTML export). Shapes are
    found the way page._scan_region_content finds them: draw page, uno_same
    on the anchor's XText.
    """
    def page_and_count() -> tuple[Any, int]:
        draw_page = src_doc.getDrawPage()
        return draw_page, int(draw_page.getCount())

    got = best_effort(page_and_count, "_copy_anchored_graphics: no draw page")
    if got is None:
        return
    draw_page, count = got
    shapes: list[Any] = []
    for i in range(min(count, 5000)):
        try:
            shapes.append(draw_page.getByIndex(i))
        except Exception:
            break
    dest_text = dest_doc.getText()
    for shape in shapes:
        try:
            anchor = shape.getAnchor()
            if not uno_same(anchor.getText(), src_text):
                continue
            # `is True`: MagicMock supportsService() is truthy.
            if shape.supportsService("com.sun.star.text.TextGraphicObject") is not True:
                continue
        except Exception:
            continue
        def copy_one() -> None:
            clone = dest_doc.createInstance("com.sun.star.text.TextGraphicObject")
            if not clone:
                return
            for prop in _ANCHORED_GRAPHIC_PROPS:
                try:
                    clone.setPropertyValue(prop, shape.getPropertyValue(prop))
                except Exception:
                    log.debug("_copy_anchored_graphics: skip %s", prop)
            cursor = dest_text.createTextCursor()
            cursor.gotoStart(False)
            # A bad anchor still inserts the logo at the start. Dropping
            # it here would hide the letterhead.
            offset = uno_read(lambda: _anchor_offset(src_text, anchor), 0)
            if offset:
                cursor.goRight(offset, False)
            dest_text.insertTextContent(cursor, clone, False)

        best_effort(copy_one, "_copy_anchored_graphics: skipped a graphic")


def _copy_xtext_into_doc(src_doc: Any, src_text: Any, dest_doc: Any) -> None:
    """Copy *src_text* (body, header, footer, cell) into *dest_doc*'s body.

    Bugfix: this called controller.select on the user's document and
    insertTransferable into the scratch Writer, then xtext_to_content ran
    XHTML and closed that Writer. That moved the visible caret and is the
    select + clipboard + hidden-export pattern that corrupted the heap on
    header reads (see _copy_flow_into_xtext). Portion copy keeps fields,
    tables, and direct formatting. Anchored graphics are cloned afterwards
    because setString in the portion walk would drop a graphic inserted first.
    """
    _copy_xtext_by_portions(src_doc, src_text, dest_doc)
    _copy_anchored_graphics(src_doc, src_text, dest_doc)


def _open_hidden_writer(ctx: Any) -> Any:
    """Hidden, empty scratch Writer (see ``new_blank_writer``)."""
    return new_blank_writer(ctx)


_PAGE_NUMBER_SUBTYPE_NAMES: dict[int, str] = {
    0: "PREV",
    1: "CURRENT",
    2: "NEXT",
}

_PAGE_NUMBER_SPAN_RE = re.compile(
    r'<span\b(?=[^>]*\btitle=["\']page-number["\'])[^>]*>',
    re.IGNORECASE,
)


def _page_number_subtype_to_name(subtype: Any) -> str:
    """Return a readable SubType enum name ('PREV', 'CURRENT', 'NEXT')."""
    if hasattr(subtype, "value"):
        val = subtype.value
        if isinstance(val, str) and val in ("PREV", "CURRENT", "NEXT"):
            return val
    elif isinstance(subtype, int):
        if subtype in _PAGE_NUMBER_SUBTYPE_NAMES:
            return _PAGE_NUMBER_SUBTYPE_NAMES[subtype]
    elif isinstance(subtype, str):
        u = subtype.strip().upper()
        if u in ("PREV", "CURRENT", "NEXT"):
            return u
        if u.isdigit() and int(u) in _PAGE_NUMBER_SUBTYPE_NAMES:
            return _PAGE_NUMBER_SUBTYPE_NAMES[int(u)]
    log.warning("Unknown PageNumber SubType: %r, defaulting to CURRENT", subtype)
    return "CURRENT"


def _page_number_numbering_type_to_name(num_type: Any) -> str:
    """Return a readable NumberingType enum name (e.g. 'ROMAN_UPPER', 'ARABIC')."""
    if isinstance(num_type, int):
        if num_type in format_mod.PAGE_NUMBER_NUMBERING_TYPE_NAMES:
            return format_mod.PAGE_NUMBER_NUMBERING_TYPE_NAMES[num_type]
    elif isinstance(num_type, str):
        u = num_type.strip().upper()
        if u in format_mod.PAGE_NUMBER_NUMBERING_TYPE_VALS:
            return u
        if u.isdigit() and int(u) in format_mod.PAGE_NUMBER_NUMBERING_TYPE_NAMES:
            return format_mod.PAGE_NUMBER_NUMBERING_TYPE_NAMES[int(u)]
    log.warning("Unknown PageNumber NumberingType: %r, defaulting to ARABIC", num_type)
    return "ARABIC"


def extract_page_number_fields(text_obj: Any) -> list[dict[str, Any]]:
    """Walk *text_obj* in document order and collect PageNumber field properties."""
    fields: list[dict[str, Any]] = []
    if text_obj is None:
        return fields
    try:
        enum = text_obj.createEnumeration()
    except Exception as e:
        log.debug("extract_page_number_fields createEnumeration failed: %s", e)
        return fields

    def _walk_para(para: Any):
        try:
            portions = para.createEnumeration()
        except Exception as e:
            log.debug("extract_page_number_fields _walk_para failed: %s", e)
            return
        while portions.hasMoreElements() is True:
            try:
                portion = portions.nextElement()
                kind = portion.getPropertyValue("TextPortionType")
            except Exception as e:
                log.debug("extract_page_number_fields portion failed: %s", e)
                break
            if kind == "TextField":
                try:
                    field = portion.getPropertyValue("TextField")
                    if field.supportsService("com.sun.star.text.textfield.PageNumber"):
                        # A PageNumber field always has these three. A failed
                        # read skips the field instead of inventing CURRENT/0/ARABIC.
                        subtype = field.getPropertyValue("SubType")
                        offset = field.getPropertyValue("Offset")
                        numbering = field.getPropertyValue("NumberingType")
                        fields.append({
                            "SubType": subtype,
                            "Offset": offset,
                            "NumberingType": numbering,
                        })
                except Exception as e:
                    log.debug("extract_page_number_fields TextField processing failed: %s", e)
                    continue

    while enum.hasMoreElements() is True:
        try:
            el = enum.nextElement()
        except Exception as e:
            log.debug("extract_page_number_fields enum failed: %s", e)
            break
        if _supports_service(el, "com.sun.star.text.TextTable"):
            try:
                cell_names = el.getCellNames()
            except Exception as e:
                log.debug("extract_page_number_fields getCellNames failed: %s", e)
                continue
            for cell_name in cell_names:
                try:
                    cell = el.getCellByName(cell_name)
                    cell_enum = cell.createEnumeration()
                except Exception as e:
                    log.debug("extract_page_number_fields getCellByName failed: %s", e)
                    continue
                while cell_enum.hasMoreElements() is True:
                    try:
                        _walk_para(cell_enum.nextElement())
                    except Exception as e:
                        log.debug("extract_page_number_fields cell_enum failed: %s", e)
                        break
        else:
            _walk_para(el)
    return fields


def xtext_to_content(
    text_obj: Any,
    model: Any,
    ctx: Any,
    services: Any = None,
    *,
    include_images: bool = True,
    max_chars: int | None = None,
) -> str:
    """Export an ``XText`` (header, footer, body, cell) via ``document_to_content``.

    Full-document XHTML omits page-style headers/footers, so the region is
    copied into a hidden Writer body's text and run through the same
    XHTML + postprocess stack as ``get_document_content``.
    """
    if text_obj is None:
        return ""

    extracted_fields = extract_page_number_fields(text_obj)

    temp_doc = None
    try:
        temp_doc = _open_hidden_writer(ctx)
        if not temp_doc or not hasattr(temp_doc, "getText"):
            # Bugfix: returning "" made page_get_header_footer_text report
            # status ok with empty content (relato #63), including when
            # document_to_content had already raised ToolExecutionError.
            raise ToolExecutionError(
                "Could not read the document: no scratch document."
            )
        _copy_xtext_into_doc(model, text_obj, temp_doc)
        content = document_to_content(
            temp_doc,
            ctx,
            services,
            max_chars=max_chars,
            scope="full",
            include_images=include_images,
        )

        if extracted_fields:
            # Re-inject the collected properties into the resulting HTML string
            # Find all <span ... title="page-number" ...> elements in the exported HTML
            spans = list(_PAGE_NUMBER_SPAN_RE.finditer(content))
            if len(spans) == len(extracted_fields):
                new_content = []
                last_end = 0
                for match, field_props in zip(spans, extracted_fields):
                    new_content.append(content[last_end:match.start()])
                    tag = match.group(0)

                    # Only inject attributes if different from defaults:
                    # com.sun.star.text.PageNumberType.CURRENT = 1
                    # Offset = 0
                    # ARABIC = 4
                    attrs = []
                    st_name = _page_number_subtype_to_name(field_props.get("SubType"))
                    if st_name != "CURRENT":
                        attrs.append(f' data-subtype="{st_name}"')

                    # Offset is a UNO short; it is already an int.
                    offset_val = field_props.get("Offset", 0)
                    if offset_val != 0:
                        attrs.append(f' data-offset="{offset_val}"')

                    nt_name = _page_number_numbering_type_to_name(field_props.get("NumberingType"))
                    if nt_name != "ARABIC":
                        attrs.append(f' data-numbering-type="{nt_name}"')

                    if attrs:
                        attr_str = "".join(attrs)
                        if tag.endswith("/>"):
                            new_tag = tag[:-2] + attr_str + "/>"
                        elif tag.endswith(">"):
                            new_tag = tag[:-1] + attr_str + ">"
                        else:
                            new_tag = tag
                    else:
                        new_tag = tag

                    new_content.append(new_tag)
                    last_end = match.end()
                new_content.append(content[last_end:])
                content = "".join(new_content)
            else:
                log.warning(
                    "xtext_to_content: PageNumber fields count mismatch. Extracted %d, found %d spans. Skipping attribute injection.",
                    len(extracted_fields),
                    len(spans),
                )

        return content
    finally:
        if temp_doc is not None:
            try:
                temp_doc.close(True)
            except Exception:
                pass


