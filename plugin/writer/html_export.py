# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2024 John Balis
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""XHTML/FODT export and image stripping for Writer documents.

Public entries: ``document_to_content`` and ``xtext_to_content``.

CJK ruby: the XHTML filter concatenates ``text:ruby`` children and range copy
drops ``RubyText``. ``inject_ruby_into_html`` restores ``<ruby><rt>`` after
semantic post-process. Apply strips ``<rt>`` then sets ``RubyText`` on the
base run (``html_ruby.extract_and_strip_ruby`` / ``_apply_ruby_spans``).
"""

from __future__ import annotations

import logging
import re
import time
import zlib
from html import escape as html_escape
from typing import Any, Callable, Iterator

from plugin.doc.temp_files import with_temp_buffer
from plugin.doc.text_helpers import (
    get_string_without_tracked_deletions,
    get_writer_selection_positions,
    iter_elements,
    starts_at_or_after,
    visible_portions,
    walk_cap_warning,
)
from plugin.framework.errors import ToolExecutionError, best_effort, reraise_if_disposed, uno_read
from plugin.framework.scratch_writer import new_blank_writer
from plugin.framework.uno_context import create_property_value
from plugin.writer.math.math_mml_export import (
    OBJECT_REPLACEMENT,
    inject_exported_math_tex,
    portion_math_embed,
    substitute_math_placeholders,
)
from . import html_ruby
from . import range_copy
from . import xhtml_style_postprocess as xhtml_post

log = logging.getLogger("writeragent.writer")

HTML_FILTER = "HTML (StarWriter)"
XHTML_FILTER = "XHTML Writer File"
XHTML_EXTENSION = ".xhtml"
FLAT_ODF_FILTER = "OpenDocument Text Flat XML"
FODT_EXTENSION = ".fodt"

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


def strip_html_boilerplate(html_string: str) -> str:
    """Extract content between ``<body>`` tags if present."""
    if not html_string or not isinstance(html_string, str):
        return html_string
    match = re.search(r"<body[^>]*>(.*?)</body>", html_string, re.DOTALL | re.IGNORECASE)
    if match:
        return match.group(1).strip()
    return html_string

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
    content = shorten_outline_anchor_ids(content)
    return content if include_images else strip_embedded_image_data(content)


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


def _read_filter_text(doc: Any, filter_name: str, ext: str = ".html") -> str:
    with with_temp_buffer(ext=ext) as (path, file_url):
        props = (create_property_value("FilterName", filter_name),)
        doc.storeToURL(file_url, props)
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return f.read()


def _export_starwriter_html(doc: Any) -> str:
    return strip_html_boilerplate(_read_filter_text(doc, HTML_FILTER))


def _export_xhtml(doc: Any) -> str:
    """Export *doc* via the XHTML Writer File filter; return the raw XHTML string."""
    return _read_filter_text(doc, XHTML_FILTER, ext=XHTML_EXTENSION)



def _autostyle_maps(doc: Any) -> tuple[dict[str, str], dict[str, str], bool | None]:
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
    onto the temp document first (see paint_direct_formatting)."""
    def read_maps() -> tuple[dict[str, str], dict[str, str], bool | None]:
        fodt = _read_filter_text(doc, FLAT_ODF_FILTER, ext=FODT_EXTENSION)
        parents, overrides = xhtml_post.parse_fodt_autostyles(fodt)
        return (parents, overrides, "<text:ruby" in fodt)

    failed: tuple[dict[str, str], dict[str, str], bool | None] = ({}, {}, None)
    return best_effort(read_maps, "_autostyle_maps: flat-ODF export failed", failed)



_COPY_PORTION_LIMIT = 50000


def _visible_portions(para: Any, limit: int = _COPY_PORTION_LIMIT, truncated_out: list[str] | None = None) -> Iterator[Any]:
    """Visible portions for offset paint. Same walk as the text helper.

    Aborts on portion enum / type failure so later runs are not painted at a
    drifted offset. ``get_string_without_tracked_deletions`` continues past a
    bad portion instead — that is the only intentional divergence.

    When the portion walk hits *limit*, log and append ``walk_cap_warning`` to
    *truncated_out* so truncated range reads are visible instead of omitting later
    Char* runs without a signal.
    """
    hit: list[int] = []
    yield from visible_portions(
        para, abort_on_portion_error=True, limit=limit, truncated_out=hit
    )
    if hit:
        msg = walk_cap_warning("text portions", hit[0], limit)
        log.warning("%s", msg)
        if truncated_out is not None:
            truncated_out.append(msg)


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
    return int(text.compareRegionStarts(left, right)) == 1


def _ends_after(text: Any, left: Any, right: Any) -> bool:
    """True when *left* ends strictly after *right* (``compareRegionEnds`` == -1)."""
    return int(text.compareRegionEnds(left, right)) == -1


def _element_overlaps(text: Any, element: Any, source: Any) -> bool:
    """True when *element* and *source* share a character.

    SwXText::ComparePositions (sw/source/core/unocore/unotext.cxx) throws only
    when a range is not in this text. The caller's window comes from that same
    text, so a failure means the read cannot place the paragraph, and the
    range read raises instead of guessing.
    """
    return (
        _starts_before(text, element.getStart(), source.getEnd())
        and _ends_after(text, element.getEnd(), source.getStart())
    )


def _selection_cursor(model: Any) -> Any | None:
    """The current selection as a cursor in its own text, or None.

    Selection scope builds a cursor in the selection's own XText instead of body
    character offsets, so index/TOC selections do not export a later body paragraph.
    """
    found = get_writer_selection_positions(model)
    if not found:
        return None
    text, start, end = found
    def select() -> Any:
        cursor = text.createTextCursorByRange(start)
        cursor.gotoRange(end, True)
        return cursor

    return best_effort(select, "_selection_cursor failed")


def _trim_to_source(text: Any, element: Any, para_text: str, source: Any) -> tuple[int, int]:
    """Visible-text window of *element* that lies inside *source*."""
    # getStart/getEnd and region comparisons run on ranges from the same text;
    # any failure raises to the outer handler so the slice does not guess.
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


# (element, visible para text) -> (trim_start, trim_end) in visible coordinates
# (_visible_portions, tracked deletions removed), or None to skip the element.
# The pair is always returned, even when nothing is cut, because paint_direct_formatting
# and _hyperlink_urls index portions with it.
_Window = Callable[[Any, str], "tuple[int, int] | None"]


def _selection_window(text: Any, source: Any) -> tuple[Callable[[Any], bool], _Window]:
    """Window filter and stop predicate for selection-range export."""
    def past_end(el: Any) -> bool:
        return starts_at_or_after(text, el.getStart(), source.getEnd())

    def window(el: Any, para_text: str) -> tuple[int, int] | None:
        if not _element_overlaps(text, el, source):
            return None
        return _trim_to_source(text, el, para_text, source)

    return past_end, window


def _offset_window(text: Any, start: int, end: int) -> tuple[Callable[[Any], bool], _Window]:
    """Window filter for character-offset slice export.

    One cursor walks forward so each paragraph costs only its gap from the
    previous element. A failed gap measurement raises rather than shifting the
    window across subsequent paragraphs. Returns the window in the paragraph's
    own coordinates with tracked deletions removed, keeping (0, len) when uncut
    so portion indexing matches.
    """
    walker = text.createTextCursor()
    walker.gotoStart(False)
    running = 0

    def window(el: Any, para_text: str) -> tuple[int, int] | None:
        nonlocal running
        gap = text.createTextCursorByRange(walker.getStart())
        gap.gotoRange(el.getStart(), True)
        running += len(get_string_without_tracked_deletions(gap))
        walker.gotoRange(el.getStart(), False)
        para_start = running
        para_end = para_start + len(para_text)
        if para_end <= start or para_start >= end:
            return None
        return max(0, start - para_start), len(para_text) - max(0, para_end - end)

    return (lambda _el: False), window


def _copy_formula_runs(
    portions: list[tuple[Any, str]],
    trim_start: int,
    trim_end: int,
    temp_text: Any,
    temp_cursor: Any,
    math_records: list[tuple[str, str, bool]],
) -> None:
    """Copy formula and text runs into *temp_text*, recording placeholders."""
    # Standalone formula: paragraph has no other visible text.
    display_block = not any(
        chunk.strip() and chunk != OBJECT_REPLACEMENT and not portion_math_embed(p)
        for p, chunk in portions
    )
    cur_offset = 0
    for p, chunk in portions:
        run_len = len(chunk)
        run_start = cur_offset
        run_end = cur_offset + run_len
        cur_offset += run_len
        emb = portion_math_embed(p)
        if emb is not None:
            # A formula portion can be zero-length; keep it when it sits inside the window.
            if run_start < trim_start or run_start > trim_end:
                continue
            slice_text = range_copy.record_math_placeholder(math_records, emb, display_block=display_block)
        else:
            if run_end <= trim_start or run_start >= trim_end:
                continue
            s_start = max(0, trim_start - run_start)
            s_end = run_len - max(0, run_end - trim_end)
            slice_text = chunk[s_start:s_end]
        if not slice_text:
            continue
        range_copy.copy_text_run(p, temp_text, temp_cursor, slice_text)


def _copy_window_to_scratch(
    model: Any,
    text: Any,
    temp_doc: Any,
    past_end: Callable[[Any], bool],
    window: _Window,
    walk_warnings: list[str] | None,
) -> tuple[list[str], list[Any], list[tuple[str, str, bool]]] | None:
    """Copy elements matching *window* into *temp_doc*.

    Returns (copied_urls, copied_ruby, math_records), or None when no element was copied.
    """
    temp_text = temp_doc.getText()
    temp_cursor = temp_text.createTextCursor()
    style_cache: dict[str, Any] = {}
    enum = text.createEnumeration()
    added_any = False
    copied_urls = []
    copied_ruby = []
    math_records: list[tuple[str, str, bool]] = []

    for el in iter_elements(enum):
        if not hasattr(el, "getString"):
            continue
        if past_end(el):
            break
        if range_copy.supports_service(el, "com.sun.star.text.TextTable"):
            # Tables are in this enumeration and have no ParaStyleName.
            style = ""
        else:
            # Paragraphs always have ParaStyleName.
            style = el.getPropertyValue("ParaStyleName") or ""

        # Same _visible_portions walk as get_string_without_tracked_deletions so
        # paint offsets match the helper string. Still walk here (not just the
        # helper) because we need the portion objects to copy Char* properties.
        portions = list(_visible_portions(el, truncated_out=walk_warnings))
        has_formula = any(portion_math_embed(p) is not None for p, _unused in portions)
        para_text = "".join(chunk for _unused, chunk in portions)

        trim = window(el, para_text)
        if trim is None:
            continue
        trim_start, trim_end = trim
        para_text = para_text[trim_start:trim_end]

        applied = False
        if not added_any:
            temp_cursor.gotoStart(False)
            applied = range_copy.apply_temp_para_style(temp_doc, temp_cursor, style)
        else:
            temp_cursor.gotoEnd(False)
            temp_text.insertControlCharacter(temp_cursor, range_copy.PARAGRAPH_BREAK, False)
            # After insertControlCharacter the cursor is still before the break, not in the
            # new paragraph. Move into it before setting style/content, otherwise setString
            # clobbers the previous paragraph instead of filling the new one.
            temp_cursor.gotoNextParagraph(False)
            temp_cursor.gotoEndOfParagraph(True)
            applied = range_copy.apply_temp_para_style(temp_doc, temp_cursor, style)

        if not has_formula:
            temp_cursor.setString(para_text)
            src_style = range_copy.source_style(model, style, style_cache) if applied else None
            range_copy.paint_direct_formatting(el, portions, temp_text, trim_start, trim_end, src_style)
        else:
            _copy_formula_runs(portions, trim_start, trim_end, temp_text, temp_cursor, math_records)

        copied_urls.extend(_hyperlink_urls(portions, trim_start, trim_end))
        # RubyText lives on the empty Ruby start mark, not the Text run, so
        # _COPIED_CHAR_PROPERTIES cannot recreate ruby on the temp doc.
        # Collect source spans and rewrite the exported HTML after XHTML.
        copied_ruby.extend(html_ruby._ruby_spans_in_window(el, trim_start, trim_end))
        added_any = True

    return (copied_urls, copied_ruby, math_records) if added_any else None


def _export_semantic(doc: Any, *, read_ruby: bool) -> tuple[str, bool | None]:
    """Export *doc* as XHTML and convert to semantic HTML using flat-ODF autostyles.

    The sidecar is a second full export. It runs when there are autostyles to
    name, or when the caller needs the ruby flag (the full read skips a portion
    walk with it). has_ruby is None when the sidecar did not run, and finish()
    then walks portions itself. The function raises on export failure, and each
    caller owns its fallback.
    """
    xhtml = _export_xhtml(doc)
    if read_ruby or xhtml_post.xhtml_has_paragraph_autostyle(xhtml):
        parents, overrides, has_ruby = _autostyle_maps(doc)
    else:
        parents, overrides, has_ruby = {}, {}, None
    return xhtml_post.xhtml_to_semantic_html(xhtml, parents, overrides), has_ruby


def _range_to_content_via_temp_doc(
    model: Any,
    ctx: Any,
    start: int,
    end: int,
    max_chars: int | None,
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
        # Blank scratch doc: the factory URL honours the user's default template,
        # and this function reads the whole body back (see new_blank_writer).
        temp_doc = new_blank_writer(ctx, target="_default")
        if not temp_doc:
            # Missing scratch Writer raises ToolExecutionError instead of returning
            # empty content with status ok.
            raise ToolExecutionError(
                "Could not read that part of the document (no scratch document)."
            )

        text = source_range.getText() if source_range is not None else model.getText()
        past_end, window = (
            _selection_window(text, source_range)
            if source_range is not None
            else _offset_window(text, start, end)
        )
        copied = _copy_window_to_scratch(model, text, temp_doc, past_end, window, walk_warnings)
        if copied is None:
            return ""
        copied_urls, copied_ruby, math_records = copied

        try:
            content, _unused_ruby = _export_semantic(temp_doc, read_ruby=False)
        except Exception as e:
            # A closed document must not start a second export.
            reraise_if_disposed(e)
            log.exception("_range_to_content_via_temp_doc (XHTML) failed; falling back to StarWriter")
            content = _export_starwriter_html(temp_doc)
        content = _restore_anchor_hrefs(content, copied_urls)
        content = _apply_image_export_options(content, include_images=include_images)
        content = substitute_math_placeholders(content, math_records, ctx)
        content = html_ruby.inject_ruby_into_html(content, copied_ruby)
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
            best_effort(lambda: temp_doc.close(True), "_range_to_content_via_temp_doc: close temp doc failed")



def document_to_content(
    model: Any,
    ctx: Any,
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

    def _done(content: str, path: str) -> str:
        # Hang diagnosis: log path, scope, content length, and total time.
        log.debug(
            "document_to_content: done path=%s scope=%r content_len=%d total_ms=%.1f",
            path,
            scope,
            len(content),
            (time.perf_counter() - t0) * 1000.0,
        )
        return content

    def finish(content: str, ruby_flag: bool | None, why: str) -> str:
        content = _apply_image_export_options(content, include_images=include_images)
        content = inject_exported_math_tex(model, ctx, content)
        content = html_ruby._inject_ruby_from_model(model, content, ruby_flag)
        _raise_if_empty_export(model, content, why)
        if max_chars and len(content) > max_chars:
            content = _truncate_html(content, max_chars)
        return content

    if scope in ("selection", "range"):
        if scope == "selection":
            # Copy the selection's own paragraphs. Character offsets from
            # get_selection_range and this walk disagree across an index section,
            # and measuring each paragraph from the document start hung on a long file.
            source = _selection_cursor(model)
            if source is None:
                return _done("", "selection")
            start, end = 0, 0
        else:
            source = None
            start = max(0, int(range_start) if range_start is not None else 0)
            end = int(range_end) if range_end is not None else 0
            # No clamp to the document length: the walk below skips paragraphs past the end on its
            # own. Clamping to CharacterCount cut a read at the end short ("o dan"), and measuring
            # the real length here would walk the whole body a second time per range read.
        return _done(
            _range_to_content_via_temp_doc(
                model, ctx, start, end, max_chars,
                include_images=include_images, walk_warnings=walk_warnings,
                source_range=source),
            scope,
        )

    # scope == "full" — preferred: XHTML (+ flat-ODF parent map) -> semantic data-lo-style.
    xhtml_error: Exception | None = None
    try:
        content, fodt_has_ruby = _export_semantic(model, read_ruby=True)
        content = finish(content, fodt_has_ruby, "the XHTML export came back empty")
        return _done(content, "xhtml")
    except Exception as e:
        # A closed document must not start a second export.
        reraise_if_disposed(e)
        xhtml_error = e
        log.exception("document_to_content (full, XHTML) failed; falling back to StarWriter")

    # Fallback: legacy StarWriter export (so reads never hard-fail).
    try:
        content = _export_starwriter_html(model)
        content = finish(content, None, "the HTML export came back empty")
        if walk_warnings is not None:
            walk_warnings.append(
                "XHTML export failed; content came from the legacy HTML export and has no style tokens."
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


def _reason(error: Exception) -> str:
    """Short text for an export failure: the message, or the exception type when it has none
    (UNO exceptions often have an empty message)."""
    return str(error).strip() or type(error).__name__


def _raise_if_empty_export(model: Any, content: str, why: str) -> None:
    """Raise when an export produced nothing although the document has text."""
    if content and content.strip():
        return
    has_text = uno_read(lambda: bool(model.getText().getString().strip()), True)
    if has_text:
        raise ToolExecutionError(why)


_PAGE_NUMBER_SUBTYPE_NAMES: dict[int, str] = {
    0: "PREV",
    1: "CURRENT",
    2: "NEXT",
}

_PAGE_NUMBER_SPAN_RE = re.compile(
    r'<span\b(?=[^>]*\btitle=["\']page-number["\'])[^>]*>',
    re.IGNORECASE,
)


def _enum_token(value: Any, names_by_value: dict[int, str], valid_names: Any, default: str, label: str) -> str:
    if hasattr(value, "value"):
        val = value.value
        if isinstance(val, str) and val in valid_names:
            return val
    elif isinstance(value, int):
        if value in names_by_value:
            return names_by_value[value]
    elif isinstance(value, str):
        u = value.strip().upper()
        if u in valid_names:
            return u
        if u.isdigit() and int(u) in names_by_value:
            return names_by_value[int(u)]
    log.warning("Unknown %s: %r, defaulting to %s", label, value, default)
    return default


def _page_number_subtype_to_name(subtype: Any) -> str:
    """Return a readable SubType enum name ('PREV', 'CURRENT', 'NEXT')."""
    return _enum_token(subtype, _PAGE_NUMBER_SUBTYPE_NAMES, ("PREV", "CURRENT", "NEXT"), "CURRENT", "PageNumber SubType")


def _page_number_numbering_type_to_name(num_type: Any) -> str:
    """Return a readable NumberingType enum name (e.g. 'ROMAN_UPPER', 'ARABIC')."""
    return _enum_token(num_type, PAGE_NUMBER_NUMBERING_TYPE_NAMES, PAGE_NUMBER_NUMBERING_TYPE_VALS, "ARABIC", "PageNumber NumberingType")


def extract_page_number_fields(text_obj: Any) -> list[dict[str, Any]]:
    """Walk *text_obj* in document order and collect PageNumber field properties."""
    fields: list[dict[str, Any]] = []
    for para in range_copy.iter_xtext_paragraphs(text_obj):
        try:
            portions = para.createEnumeration()
        except Exception as e:
            log.debug("extract_page_number_fields createEnumeration failed: %s", e)
            continue
        for portion, kind in range_copy.iter_portions(portions):
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
    return fields


def _inject_page_number_attributes(content: str, fields: list[dict[str, Any]]) -> str:
    """Add data-subtype/offset/numbering-type to each page-number span; defaults are omitted."""
    if not content or not fields:
        return content
    spans = list(_PAGE_NUMBER_SPAN_RE.finditer(content))
    if len(spans) != len(fields):
        log.warning(
            "xtext_to_content: PageNumber fields count mismatch. Extracted %d, found %d spans. Skipping attribute injection.",
            len(fields), len(spans),
        )
        return content
    out: list[str] = []
    last_end = 0
    for match, props in zip(spans, fields):
        attrs = ""
        # Only inject attributes if different from defaults:
        # com.sun.star.text.PageNumberType.CURRENT = 1
        # Offset = 0
        # ARABIC = 4
        st_name = _page_number_subtype_to_name(props.get("SubType"))
        if st_name != "CURRENT":
            attrs += f' data-subtype="{st_name}"'
        offset_val = props.get("Offset", 0)
        if offset_val != 0:
            attrs += f' data-offset="{offset_val}"'
        nt_name = _page_number_numbering_type_to_name(props.get("NumberingType"))
        if nt_name != "ARABIC":
            attrs += f' data-numbering-type="{nt_name}"'
        out.append(content[last_end:match.start()])
        out.append(xhtml_post.inject_attr(match.group(0), attrs) if attrs else match.group(0))
        last_end = match.end()
    out.append(content[last_end:])
    return "".join(out)


def xtext_to_content(
    text_obj: Any,
    model: Any,
    ctx: Any,
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

    temp_doc = new_blank_writer(ctx)
    if not temp_doc:
        # Missing scratch Writer raises ToolExecutionError instead of reporting
        # ok with empty header/footer text.
        raise ToolExecutionError(
            "Could not read the document: no scratch document."
        )
    try:
        math_records: list[tuple[str, str, bool]] = []
        range_copy.copy_xtext_into_doc(model, text_obj, temp_doc, math_records=math_records)
        content = document_to_content(
            temp_doc,
            ctx,
            max_chars=max_chars,
            scope="full",
            include_images=include_images,
        )
        content = substitute_math_placeholders(content, math_records, ctx)

        content = _inject_page_number_attributes(content, extracted_fields)

        return content
    finally:
        best_effort(lambda: temp_doc.close(True), "xtext_to_content: close temp doc failed")


