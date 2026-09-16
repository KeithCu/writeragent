# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Core get_image tool — return an embedded image as a real (viewable) image.

Lazy image perception (see docs/images/recognition-multimodal-LLMs.md and the Keith/Augusto design):
b64 is stripped from get_document_content by default, so a model never pays vision tokens up front.
When it actually needs to SEE one image, it calls get_image, which returns that single picture as a
native MCP image content block (only then are vision tokens spent). One self-documenting tool, by
graphic name, by the current selection, or page=<n> to render a whole page (_render_page_png below).
On Draw/Impress, page=N is the vision screenshot (0-based, same as list_pages / get_draw_tree); get_draw_tree remains the structure read.
"""

import base64

from plugin.framework.tool import ToolBase
from plugin.writer.images.image_tools import export_graphic_object_to_bytes, get_selected_image_base64


_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
_TEXT_DOCUMENT = "com.sun.star.text.TextDocument"
_DRAW_DOCUMENT = "com.sun.star.drawing.DrawingDocument"
_IMPRESS_DOCUMENT = "com.sun.star.presentation.PresentationDocument"


def _supports_service(doc, service):
    try:
        return bool(doc.supportsService(service))
    except Exception:
        return False


def _is_draw_family(doc):
    """True for Draw or Impress. Check PresentationDocument first: Impress also supports DrawingDocument."""
    return _supports_service(doc, _IMPRESS_DOCUMENT) or _supports_service(doc, _DRAW_DOCUMENT)


def _read_png_or_reason(tmp_path, page):
    with open(tmp_path, "rb") as f:
        png = f.read()
    if not png or png[:8] != _PNG_MAGIC:
        return None, "could not render page %d: the PNG export produced no valid image." % page
    return png, None


def _render_writer_page_png(doc, page):
    """Render 0-based *page* of a Writer doc to PNG bytes, or (None, reason).

    *page* is model-facing (first page = 0). jumpToPage is 1-based, so the only conversion is
    ``lo_page = page + 1`` here — error text still quotes the 0-based index the model sent.

    NATIVE LibreOffice path ONLY — the writer_png_Export graphic filter, targeted per page with the
    view cursor (jumpToPage): cross-platform, no external binary, no PDF round-trip, and the filter
    reuses LO's own page painting (headers, frames, shapes, images all faithful). The XRenderable
    route was abandoned: on real multi-page documents its getRendererCount reports 1 page no matter
    which options are passed, and its getDIB() bitmaps are build-dependent (BUG-5 diagnostics,
    2026-07-01). The view cursor is saved and restored best-effort (the jump may briefly scroll the
    window). There is intentionally NO fallback — on any failure this returns a clear reason
    (surfaced by the caller as a tool error), never a silent failure or an empty image."""
    import os
    import tempfile

    try:
        vc = doc.getCurrentController().getViewCursor()
    except Exception as e:
        return None, "could not render page %d: no document view available (%s)" % (page, e)

    saved = None
    try:
        # Same save/restore idiom as get_page_objects (structural.py). If the cursor sits in nested
        # text (table cell / frame) this raises and we simply skip the best-effort restore.
        saved = doc.getText().createTextCursorByRange(vc.getStart())
    except Exception:
        saved = None

    tmp_path = None
    try:
        # jumpToPage / getPage are 1-based Writer APIs; *page* is 0-based at the tool boundary.
        lo_page = page + 1
        vc.jumpToPage(lo_page)
        actual = int(vc.getPage())
        if actual != lo_page:
            # jumpToPage clamps out-of-range targets; jump to the end to report the real total.
            try:
                vc.jumpToLastPage()
                total = int(vc.getPage())
            except Exception:
                total = actual
            return None, "could not render page %d: page not found (document has %d page(s); 0-based (first page is 0))." % (page, total)
        # UNO imports only here: nothing above needs them, so validation/error paths stay
        # exception-free even where the uno module is unavailable (e.g. mocked test envs).
        import uno
        from com.sun.star.beans import PropertyValue

        fd, tmp_path = tempfile.mkstemp(prefix="wa_page_render_", suffix=".png")
        os.close(fd)
        doc.storeToURL(
            uno.systemPathToFileUrl(tmp_path),
            (PropertyValue(Name="FilterName", Value="writer_png_Export"),),
        )
        return _read_png_or_reason(tmp_path, page)
    except Exception as e:
        return None, "could not render page %d: %s" % (page, e)
    finally:
        if tmp_path:
            try:
                os.remove(tmp_path)
            except Exception:
                pass
        if saved is not None:
            try:
                vc.gotoRange(saved, False)
            except Exception:
                pass


def _render_draw_page_png(ctx, doc, page):
    """Render 0-based *page* of a Draw/Impress doc to PNG bytes, or (None, reason).

    *page* is already the ``getByIndex`` index (first slide = 0). No ±1 here — that used to
    exist when the tool was 1-based and is what made get_image the Draw/Impress oddball.

    Native ``com.sun.star.drawing.GraphicExportFilter`` with the XDrawPage as source (MediaType
    image/png). Verified live 2026-09-09: this yields a valid PNG and different pages produce
    different bytes. ``setCurrentPage`` + ``storeToURL(draw_png_Export / impress_png_Export)``
    produces the same image but mutates the view; FilterData PageNumber on the document model
    does not work (filter returns false). Fail-loud — never a silent empty image.
    """
    import os
    import tempfile

    try:
        pages = doc.getDrawPages()
        total = int(pages.getCount())
    except Exception as e:
        return None, "could not render page %d: no draw pages available (%s)" % (page, e)

    if page < 0 or page >= total:
        return None, "could not render page %d: page not found (document has %d page(s); 0-based (first page is 0))." % (page, total)

    tmp_path = None
    try:
        import uno
        from com.sun.star.beans import PropertyValue

        if ctx is None:
            return None, "could not render page %d: no component context for GraphicExportFilter." % page
        smgr = getattr(ctx, "ServiceManager", None)
        if smgr is None:
            smgr = ctx.getServiceManager()
        filt = smgr.createInstanceWithContext("com.sun.star.drawing.GraphicExportFilter", ctx)
        if filt is None:
            return None, "could not render page %d: GraphicExportFilter is unavailable." % page
        # setSourceDocument accepts XDrawPage here (not only XModel) — that is how a specific
        # slide is targeted without a controller page jump.
        filt.setSourceDocument(pages.getByIndex(page))
        fd, tmp_path = tempfile.mkstemp(prefix="wa_page_render_", suffix=".png")
        os.close(fd)
        ok = filt.filter((
            PropertyValue(Name="URL", Value=uno.systemPathToFileUrl(tmp_path)),
            PropertyValue(Name="MediaType", Value="image/png"),
        ))
        if not ok:
            return None, "could not render page %d: GraphicExportFilter returned false." % page
        return _read_png_or_reason(tmp_path, page)
    except Exception as e:
        return None, "could not render page %d: %s" % (page, e)
    finally:
        if tmp_path:
            try:
                os.remove(tmp_path)
            except Exception:
                pass


def _render_page_png(ctx, doc, page):
    """Render 0-based *page* of Writer, Draw, or Impress to PNG bytes, or (None, reason)."""
    if _is_draw_family(doc):
        return _render_draw_page_png(ctx, doc, page)
    return _render_writer_page_png(doc, page)


class GetImage(ToolBase):
    name = "get_image"
    tier = "core"
    description = (
        "Return an image so you can SEE it (vision-capable models). One of: image=<the graphic's name "
        "from image_list / get_page_objects> for an embedded picture; selection=true for the image "
        "currently selected; or page=<n> (0-based page/slide index, same as list_pages / add_slide / "
        "set_placeholder_text; first page is 0) to render that whole PAGE as an image "
        "(Writer/Draw/Impress layout). On Draw/Impress, get_draw_tree is the shape tree — use page= "
        "here when you need to see the rendered page. Returns the picture itself, not a description. "
        "b64 is stripped from normal reads, so use this when you actually need to look."
    )
    parameters = {
        "type": "object",
        "properties": {
            "image": {"type": "string", "description": "Name of the embedded graphic to fetch (from image_list / get_page_objects)."},
            "selection": {"type": "boolean", "description": "If true, fetch the currently selected image instead of naming one."},
            "page": {"type": "integer", "description": "0-based page/slide index; first page is 0 — same as list_pages / add_slide / set_placeholder_text. Renders that whole page layout as an image, instead of fetching one embedded image."},
        },
        "required": [],
    }
    # Impress also supports DrawingDocument; list both so execution accepts either service set.
    uno_services = [_TEXT_DOCUMENT, _DRAW_DOCUMENT, _IMPRESS_DOCUMENT]

    def execute(self, ctx, **kwargs):
        doc = ctx.doc
        name = kwargs.get("image")
        want_selection = bool(kwargs.get("selection"))
        page = kwargs.get("page")
        try:
            if page is not None:
                if not isinstance(page, int) or page < 0:
                    return self._tool_error("page must be a non-negative integer — 0-based (first page is 0).")
                raw, reason = _render_page_png(ctx.ctx, doc, int(page))
                if raw is None:
                    return self._tool_error(reason or "Could not render the page.")
                b64 = base64.b64encode(raw).decode("utf-8")
                return {"status": "ok", "source": f"page {page}", "_mcp_image": {"data": b64, "mimeType": "image/png"}}
            if want_selection or not name:
                b64 = get_selected_image_base64(doc, ctx.ctx)
                if not b64:
                    return self._tool_error("No image selected. Select an image in the document, or pass image=<name> (see image_list).")
                source = "selection"
            else:
                # visual_helpers walks Writer GraphicObjects and Draw/Impress GraphicObjectShapes.
                # Do not call getGraphicObjects() here — that Writer-only API is missing on Draw
                # and would look like a successful miss ("no such name") instead of a real lookup.
                from plugin.doc.visual_helpers import get_graphic_object_by_name

                obj = get_graphic_object_by_name(doc, name)
                if obj is None:
                    return self._tool_error(f"No embedded image named '{name}'. Call image_list to see the available names.")
                raw = export_graphic_object_to_bytes(ctx.ctx, obj)
                if not raw:
                    return self._tool_error(f"Could not export image '{name}'.")
                b64 = base64.b64encode(raw).decode("utf-8")
                source = name
            # _mcp_image is recognized by the MCP tools/call serializer (mcp_protocol) and returned as a
            # native image content block; over non-image transports it is just an ignorable marker.
            return {"status": "ok", "source": source, "_mcp_image": {"data": b64, "mimeType": "image/png"}}
        except Exception as e:
            return self._tool_error(f"get_image failed: {e}")
