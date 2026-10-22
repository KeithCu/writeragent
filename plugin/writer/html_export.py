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
base run (``html_ruby.extract_and_strip_ruby`` / ``_apply_ruby_spans``).
"""

from __future__ import annotations

import logging
import re
import time
import zlib
from html import escape as html_escape
from typing import Any, Iterator

from plugin.framework.errors import best_effort
from plugin.doc.text_helpers import (
    get_string_without_tracked_deletions,
    _visible_portions as _shared_visible_portions,
)
from plugin.framework.errors import ToolExecutionError, reraise_if_disposed, uno_read
from plugin.framework.scratch_writer import new_blank_writer
from plugin.writer.math.math_mml_export import (
    _OBJECT_REPLACEMENT,
    portion_math_embed,
)
from . import range_copy
from . import xhtml_style_postprocess as xhtml_post

log = logging.getLogger("writeragent.writer")
# uno_read default where None is a real UNO result.
_FAILED: Any = object()


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
        if range_copy._supports_service(el, "com.sun.star.text.TextTable"):
            try:
                names = el.getCellNames() or ()
            except Exception as exc:
                log.debug("_iter_xtext_paragraphs: getCellNames failed: %s", exc)
                continue
            for name in names:
                try:
                    cell = el.getCellByName(name)
                except Exception as exc:
                    log.debug("_iter_xtext_paragraphs: getCellByName failed: %s", exc)
                    continue
                yield from _iter_xtext_paragraphs(cell)
            continue
        if hasattr(el, "createEnumeration"):
            yield el


from . import html_ruby


def _substitute_math_placeholders(
    content: str,
    math_records: list[tuple[str, str, bool]],
    ctx: Any,
) -> str:
    """Replace private-use formula placeholders with delimited TeX."""
    if not math_records or not content:
        return content
    from plugin.writer.math.math_mml_export import convert_starmath_to_latex, wrap_latex_delimiters

    for placeholder, starmath, display_block in math_records:
        res = convert_starmath_to_latex(ctx, starmath) if (ctx and starmath) else None
        if res and res.ok and res.latex:
            tex = res.latex
        else:
            snippet = starmath[:200]
            tex = f"[Math export fallback] {snippet}".strip()
        wrapped = wrap_latex_delimiters(tex, display_block=display_block)

        code = ord(placeholder)
        for target in (placeholder, f"&#x{code:x};", f"&#x{code:X};", f"&#{code};"):
            if target in content:
                content = content.replace(target, wrapped)
    return content


def _inject_exported_math_tex(model: Any, ctx: Any, content: str) -> str:
    """Replace formula OLE holes with delimited TeX for the model/chat.

    Failures stay in the HTML as a visible fallback; never drop formulas.
    """
    if not content or model is None or ctx is None:
        return content
    try:
        from plugin.writer.math.math_mml_export import inject_math_tex_into_html

        return inject_math_tex_into_html(model, ctx, content)
    except Exception as exc:
        reraise_if_disposed(exc)
        log.exception("_inject_exported_math_tex failed")
        return content



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



def _export_xhtml(doc: Any) -> str:
    """Export *doc* via the XHTML Writer File filter; return the raw XHTML string."""
    from . import format as format_mod
    with format_mod._with_temp_buffer(ext=format_mod.XHTML_EXTENSION) as (path, file_url):
        props = (format_mod.create_property_value("FilterName", format_mod.XHTML_FILTER),)
        doc.storeToURL(file_url, props)
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return f.read()



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
    onto the temp document first (see _paint_direct_formatting)."""
    def read_maps() -> tuple[dict[str, str], dict[str, str], bool | None]:
        from . import format as format_mod
        with format_mod._with_temp_buffer(ext=format_mod.FODT_EXTENSION) as (path, file_url):
            props = (format_mod.create_property_value("FilterName", format_mod.FLAT_ODF_FILTER),)
            doc.storeToURL(file_url, props)
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                fodt = f.read()
        parents, overrides = xhtml_post._parse_fodt_autostyles(fodt)
        return (parents, overrides, "<text:ruby" in fodt)

    failed: tuple[dict[str, str], dict[str, str], bool | None] = ({}, {}, None)
    return best_effort(read_maps, "_autostyle_maps: flat-ODF export failed", failed)



_COPY_PORTION_LIMIT = 50000


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
        from . import format as format_mod
        msg = format_mod.walk_cap_warning("text portions", hit[0], limit)
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

        math_records: list[tuple[str, str, bool]] = []
        while enum.hasMoreElements():
            el = enum.nextElement()
            if not hasattr(el, "getString"):
                continue
            if source_range is not None and _starts_at_or_after(text, el.getStart(), source_range.getEnd()):
                break
            if range_copy._supports_service(el, "com.sun.star.text.TextTable"):
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

            if not has_formula:
                temp_cursor.setString(para_text)
                range_copy._paint_direct_formatting(el, portions, temp_text, trim_start, trim_end,
                                          _source_style(model, style, style_cache))
            else:
                # Standalone formula: paragraph has no other visible text
                display_block = not any(
                    chunk.strip() and chunk != _OBJECT_REPLACEMENT and not portion_math_embed(p)
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
                    elif run_end <= trim_start or run_start >= trim_end:
                        continue
                    if emb is not None:
                        ph = range_copy._record_math_placeholder(math_records, emb, display_block=display_block)
                        slice_text = ph
                    else:
                        s_start = max(0, trim_start - run_start)
                        s_end = run_len - max(0, run_end - trim_end)
                        slice_text = chunk[s_start:s_end]
                    if not slice_text:
                        continue
                    start_pos = temp_cursor.getStart()
                    temp_text.insertString(temp_cursor, slice_text, False)
                    run = temp_text.createTextCursorByRange(start_pos)
                    run.gotoRange(temp_cursor.getEnd(), True)
                    range_copy._copy_properties(p, run, range_copy._COPIED_CHAR_PROPERTIES)
                    temp_cursor.gotoRange(run.getEnd(), False)

            copied_urls.extend(_hyperlink_urls(portions, trim_start, trim_end))
            # RubyText lives on the empty Ruby start mark, not the Text run, so
            # _COPIED_CHAR_PROPERTIES cannot recreate ruby on the temp doc.
            # Collect source spans and rewrite the exported HTML after XHTML.
            copied_ruby.extend(html_ruby._ruby_spans_in_window(el, trim_start, trim_end))
            added_any = True

        if not added_any:
            return ""

        try:
            xhtml = _export_xhtml(temp_doc)
            if xhtml_post.xhtml_has_paragraph_autostyle(xhtml):
                parents, overrides, _unused_ruby = _autostyle_maps(temp_doc)
            else:
                parents, overrides = {}, {}
            content = xhtml_post.xhtml_to_semantic_html(xhtml, parents, overrides)
        except Exception as e:
            # A closed document must not start a second export.
            reraise_if_disposed(e)
            log.exception("_range_to_content_via_temp_doc (XHTML) failed; falling back to StarWriter")
            from . import format as format_mod
            filter_name = format_mod.HTML_FILTER
            with format_mod._with_temp_buffer() as (path, file_url):
                props = (format_mod.create_property_value("FilterName", filter_name),)
                temp_doc.storeToURL(file_url, props)
                with open(path, "r", encoding="utf-8", errors="replace") as f:
                    content = f.read()
            content = format_mod._strip_html_boilerplate(content)
        content = _restore_anchor_hrefs(content, copied_urls)
        content = _apply_image_export_options(content, include_images=include_images)
        content = _substitute_math_placeholders(content, math_records, ctx)
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
                model, ctx, 0, 0, max_chars,
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
                model, ctx, start, end, max_chars,
                include_images=include_images, walk_warnings=walk_warnings),
            "range",
        )

    # scope == "full" — preferred: XHTML (+ flat-ODF parent map) -> semantic data-lo-style.
    xhtml_error: Exception | None = None
    try:
        t_phase = time.perf_counter()
        xhtml = _export_xhtml(model)
        log.debug(
            "document_to_content: phase=_export_xhtml elapsed_ms=%.1f xhtml_len=%d",
            (time.perf_counter() - t_phase) * 1000.0,
            len(xhtml) if isinstance(xhtml, str) else -1,
        )
        t_phase = time.perf_counter()
        # Full export always runs _autostyle_maps: fodt_has_ruby skips iter_ruby_spans, and None triggers a portion walk.
        parents, overrides, fodt_has_ruby = _autostyle_maps(model)
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
        content = html_ruby._inject_ruby_from_model(model, content, fodt_has_ruby)
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
        from . import format as format_mod
        filter_name = format_mod.HTML_FILTER
        with format_mod._with_temp_buffer() as (path, file_url):
            props = (format_mod.create_property_value("FilterName", filter_name),)
            model.storeToURL(file_url, props)
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                content = f.read()
            content = format_mod._strip_html_boilerplate(content)
            content = _apply_image_export_options(content, include_images=include_images)
            content = _inject_exported_math_tex(model, ctx, content)
            content = html_ruby._inject_ruby_from_model(model, content, None)
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
    from . import format as format_mod
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
        if range_copy._supports_service(el, "com.sun.star.text.TextTable"):
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
        math_records: list[tuple[str, str, bool]] = []
        range_copy._copy_xtext_into_doc(model, text_obj, temp_doc, math_records=math_records)
        content = document_to_content(
            temp_doc,
            ctx,
            services,
            max_chars=max_chars,
            scope="full",
            include_images=include_images,
        )
        if math_records:
            content = _substitute_math_placeholders(content, math_records, ctx)

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
            best_effort(lambda: temp_doc.close(True), "xtext_to_content: close temp doc failed")


