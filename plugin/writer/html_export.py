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



def _export_xhtml(doc, config_svc):
    """Export *doc* via the XHTML Writer File filter; return the raw XHTML string."""
    with format_mod._with_temp_buffer(None, config_svc, ext=format_mod.XHTML_EXTENSION) as (path, file_url):
        props = (format_mod.create_property_value("FilterName", format_mod.XHTML_FILTER),)
        doc.storeToURL(file_url, props)
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return f.read()



def _autostyle_parents(doc, config_svc):
    """Export *doc* as flat ODF and return the autostyle -> parent-style map (Pn -> base name).

    Lets the read path recover an autostyle paragraph's real style name when the XHTML CSS
    fingerprint matches nothing. Returns ``{}`` on any failure (the read still works, just
    without the autostyle-name recovery)."""
    try:
        with format_mod._with_temp_buffer(None, config_svc, ext=format_mod.FODT_EXTENSION) as (path, file_url):
            props = (format_mod.create_property_value("FilterName", format_mod.FLAT_ODF_FILTER),)
            doc.storeToURL(file_url, props)
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                fodt = f.read()
        return xhtml_post.extract_autostyle_parents_from_fodt(fodt)
    except Exception:
        log.debug("_autostyle_parents: flat-ODF export failed", exc_info=True)
        return {}



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
            para_text = get_string_without_tracked_deletions(el)
            style = style or ""
            # Compute paragraph start offset
            start_cursor = model.getText().createTextCursor()
            start_cursor.gotoStart(False)
            start_cursor.gotoRange(el.getStart(), True)
            para_start = len(get_string_without_tracked_deletions(start_cursor))

            para_end = para_start + len(para_text)

            if para_end <= start or para_start >= end:
                continue
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
            added_any = True

        if not added_any:
            return ""

        try:
            xhtml = _export_xhtml(temp_doc, config_svc)
            parents = _autostyle_parents(temp_doc, config_svc)
            content = xhtml_post.xhtml_to_semantic_html(xhtml, parents)
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
        parents = _autostyle_parents(model, config_svc)
        log.debug(
            "document_to_content: phase=_autostyle_parents elapsed_ms=%.1f parents=%d",
            (time.perf_counter() - t_phase) * 1000.0,
            len(parents) if isinstance(parents, dict) else -1,
        )
        t_phase = time.perf_counter()
        content = xhtml_post.xhtml_to_semantic_html(xhtml, parents)
        content = _apply_image_export_options(content, include_images=include_images)
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



