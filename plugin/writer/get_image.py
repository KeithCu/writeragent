# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Core get_image tool — return an embedded image as a real (viewable) image.

Lazy image perception (see docs/image-recognition-multimodal-LLMs.md and the Keith/Augusto design):
b64 is stripped from get_document_content by default, so a model never pays vision tokens up front.
When it actually needs to SEE one image, it calls get_image, which returns that single picture as a
native MCP image content block (only then are vision tokens spent). One self-documenting tool, by
graphic name, by the current selection, or page=<n> to render a whole page (_render_page_png below).
"""

import base64

from plugin.framework.tool import ToolBase
from plugin.writer.images.image_tools import export_graphic_object_to_bytes, get_selected_image_base64


def _render_page_png(ctx, doc, page):
    """Render 1-based *page* of a Writer doc to PNG bytes, or (None, reason).

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
        vc.jumpToPage(page)
        actual = int(vc.getPage())
        if actual != page:
            # jumpToPage clamps out-of-range targets; jump to the end to report the real total.
            try:
                vc.jumpToLastPage()
                total = int(vc.getPage())
            except Exception:
                total = actual
            return None, "could not render page %d: page not found (document has %d page(s))." % (page, total)
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
        with open(tmp_path, "rb") as f:
            png = f.read()
        if not png or png[:8] != b"\x89PNG\r\n\x1a\n":
            return None, "could not render page %d: the PNG export produced no valid image." % page
        return png, None
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


class GetImage(ToolBase):
    name = "get_image"
    tier = "core"
    description = (
        "Return an image so you can SEE it (vision-capable models). One of: image=<the graphic's name "
        "from list_images / get_page_objects> for an embedded picture; selection=true for the image "
        "currently selected; or page=<n> to render that whole PAGE as an image (layout/what-it-looks-like). "
        "Returns the picture itself, not a description. b64 is stripped from normal reads, so use this "
        "when you actually need to look."
    )
    parameters = {
        "type": "object",
        "properties": {
            "image": {"type": "string", "description": "Name of the embedded graphic to fetch (from list_images / get_page_objects)."},
            "selection": {"type": "boolean", "description": "If true, fetch the currently selected image instead of naming one."},
            "page": {"type": "integer", "description": "Render this 1-based page as an image (the whole page layout), instead of fetching one embedded image."},
        },
        "required": [],
    }
    uno_services = ["com.sun.star.text.TextDocument"]

    def execute(self, ctx, **kwargs):
        doc = ctx.doc
        name = kwargs.get("image")
        want_selection = bool(kwargs.get("selection"))
        page = kwargs.get("page")
        try:
            if page is not None:
                if not isinstance(page, int) or page < 1:
                    return self._tool_error("page must be a positive integer (1-based).")
                raw, reason = _render_page_png(ctx.ctx, doc, int(page))
                if raw is None:
                    return self._tool_error(reason or "Could not render the page.")
                b64 = base64.b64encode(raw).decode("utf-8")
                return {"status": "ok", "source": f"page {page}", "_mcp_image": {"data": b64, "mimeType": "image/png"}}
            if want_selection or not name:
                b64 = get_selected_image_base64(doc, ctx.ctx)
                if not b64:
                    return self._tool_error("No image selected. Select an image in the document, or pass image=<name> (see list_images).")
                source = "selection"
            else:
                if not hasattr(doc, "getGraphicObjects") or not doc.getGraphicObjects().hasByName(name):
                    return self._tool_error(f"No embedded image named '{name}'. Call list_images to see the available names.")
                obj = doc.getGraphicObjects().getByName(name)
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
