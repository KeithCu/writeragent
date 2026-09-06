# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2024 John Balis
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""XHTML/FODT export and image stripping for Writer documents.

Public entry: ``document_to_content`` (also re-exported from ``plugin.writer.format``).
"""

import logging
import re
import time

from plugin.doc.text_helpers import get_string_without_tracked_deletions
from plugin.framework.uno_context import get_desktop
from . import xhtml_style_postprocess as xhtml_post
from . import format as format_mod

log = logging.getLogger("writeragent.writer")

# com.sun.star.text.ControlCharacter.PARAGRAPH_BREAK
_PARAGRAPH_BREAK = 0

_DATA_URI_IMAGE_RE = re.compile(
    r"data:image/[^\"'\s);>]+;base64,[A-Za-z0-9+/=\s]+",
    re.IGNORECASE,
)


def strip_embedded_image_data(html: str) -> str:
    """Remove inline ``data:image`` base64 payloads from exported HTML; external URLs unchanged."""
    if not html:
        return html
    return _DATA_URI_IMAGE_RE.sub("", html)



def _apply_image_export_options(content: str, *, include_images: bool) -> str:
    if include_images or not content:
        return content
    return strip_embedded_image_data(content)


def _inject_exported_math_tex(model, ctx, content: str) -> str:
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



def _export_xhtml(doc, config_svc):
    """Export *doc* via the XHTML Writer File filter; return the raw XHTML string."""
    with format_mod._with_temp_buffer(None, config_svc, ext=format_mod.XHTML_EXTENSION) as (path, file_url):
        props = (format_mod.create_property_value("FilterName", format_mod.XHTML_FILTER),)
        doc.storeToURL(file_url, props)
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return f.read()



def _autostyle_maps(doc, config_svc):
    """Export *doc* as flat ODF once and return ``(parents, overrides)`` for the autostyles.

    ``parents`` (Pn -> base style name) lets the read path recover an autostyle paragraph's real
    style name when the XHTML CSS fingerprint matches nothing. ``overrides`` (Pn -> CSS text) is
    the paragraph's DIRECT formatting, which the flattened XHTML cannot distinguish from inherited
    values. Both come from the same export. Returns ``({}, {})`` on any failure (the read still
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
                xhtml_post.extract_autostyle_overrides_from_fodt(fodt))
    except Exception:
        log.debug("_autostyle_maps: flat-ODF export failed", exc_info=True)
        return ({}, {})



# Direct formatting the range copy carries into the temp document. Whitelists rather than "every
# property": a blanket copy drags UNO structs and page/section properties along, which either fail
# to set or change the temp document's layout. Char* is painted per text portion so a bold run
# inside a sentence survives; Para* is set once per paragraph.
_COPIED_CHAR_PROPERTIES = (
    "CharStyleName", "CharFontName", "CharHeight", "CharWeight", "CharPosture",
    "CharUnderline", "CharStrikeout", "CharColor", "CharBackColor", "CharCaseMap",
    "CharEscapement", "CharEscapementHeight",
)
_COPIED_PARA_PROPERTIES = (
    "ParaLeftMargin", "ParaRightMargin", "ParaTopMargin", "ParaBottomMargin",
    "ParaFirstLineIndent", "ParaAdjust", "ParaBackColor",
)

_COPY_PORTION_LIMIT = 50000


def _copy_properties(src, dst, names, style=None):
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


def _source_style(model, style_name, cache):
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


def _visible_portions(para):
    """Yield ``(portion, text)`` for a paragraph's visible text, skipping tracked deletions.

    Mirrors the walk in ``get_string_without_tracked_deletions`` exactly — same Redline/Delete
    toggle, same skips — so an offset taken from that string indexes into these chunks without
    drift. Any divergence here would paint one run's formatting onto another's characters.
    """
    try:
        portion_enum = para.createEnumeration()
    except Exception:
        return
    in_delete = False
    seen = 0
    while portion_enum.hasMoreElements() is True and seen < _COPY_PORTION_LIMIT:
        seen += 1
        try:
            portion = portion_enum.nextElement()
            portion_type = portion.getPropertyValue("TextPortionType")
        except Exception:
            return
        if portion_type == "Redline":
            try:
                if str(portion.getPropertyValue("RedlineType")) == "Delete":
                    in_delete = not in_delete
            except Exception:
                pass
            continue
        if in_delete:
            continue
        try:
            chunk = portion.getString()
        except Exception:
            continue
        if chunk:
            yield portion, chunk


def _paint_direct_formatting(para, portions, temp_text, trim_start, trim_end, style=None):
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
        para_cursor.gotoEndOfParagraph(True)
        _copy_properties(para, para_cursor, _COPIED_PARA_PROPERTIES, style)
    except Exception:
        log.debug("_paint_direct_formatting: paragraph properties skipped", exc_info=True)
        return

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


def _range_to_content_via_temp_doc(model, ctx, start, end, max_chars, config_svc, *, include_images=False):
    """Export a character range to content via a hidden temp document."""
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
        text = model.getText()
        enum = text.createEnumeration()
        first_para = True
        added_any = False

        while enum.hasMoreElements():
            el = enum.nextElement()
            if not hasattr(el, "getString"):
                continue
            try:
                style = el.getPropertyValue("ParaStyleName")
            except Exception:
                style = ""
            # Built from the portion walk rather than get_string_without_tracked_deletions: that
            # helper enumerates a paragraph's PORTIONS as if they were paragraphs and joins them
            # with "\n", so a bold run mid-sentence used to come back as "text\nbold\ntext" —
            # spurious <br/> in the output, and offsets that no longer match the portions the
            # formatting has to be painted onto.
            portions = list(_visible_portions(el))
            para_text = "".join(chunk for _unused, chunk in portions)
            style = style or ""
            # Compute paragraph start offset
            start_cursor = model.getText().createTextCursor()
            start_cursor.gotoStart(False)
            start_cursor.gotoRange(el.getStart(), True)
            para_start = len(get_string_without_tracked_deletions(start_cursor))

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
            added_any = True

        if not added_any:
            return ""

        try:
            xhtml = _export_xhtml(temp_doc, config_svc)
            parents, overrides = _autostyle_maps(temp_doc, config_svc)
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
        content = _apply_image_export_options(content, include_images=include_images)
        content = _inject_exported_math_tex(model, ctx, content)
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
    model,
    ctx,
    services,
    max_chars=None,
    scope="full",
    range_start=None,
    range_end=None,
    *,
    include_images=False,
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
        # Import via format so LibrePy (which ships html_export but not document_helpers)
        # selection path no longer names document_helpers in this file.
        start, end = format_mod._selection_range_for_export(model)
        return _done(
            _range_to_content_via_temp_doc(model, ctx, start, end, max_chars, config_svc, include_images=include_images),
            "selection",
        )

    if scope == "range":
        start = int(range_start) if range_start is not None else 0
        end = int(range_end) if range_end is not None else 0
        doc_len = services.document.get_document_length(model) if services else 0
        start = max(0, min(start, doc_len))
        end = min(end, doc_len)
        return _done(
            _range_to_content_via_temp_doc(model, ctx, start, end, max_chars, config_svc, include_images=include_images),
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
        parents, overrides = _autostyle_maps(model, config_svc)
        log.debug(
            "document_to_content: phase=_autostyle_maps elapsed_ms=%.1f parents=%d overrides=%d",
            (time.perf_counter() - t_phase) * 1000.0,
            len(parents) if isinstance(parents, dict) else -1,
            len(overrides) if isinstance(overrides, dict) else -1,
        )
        t_phase = time.perf_counter()
        content = xhtml_post.xhtml_to_semantic_html(xhtml, parents, overrides)
        content = _apply_image_export_options(content, include_images=include_images)
        content = _inject_exported_math_tex(model, ctx, content)
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



