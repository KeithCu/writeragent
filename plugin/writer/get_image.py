# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Core image_get tool — return an embedded image as a real (viewable) image.

Lazy image perception (see docs/images/recognition-multimodal-LLMs.md and the Keith/Augusto design):
b64 is stripped from get_content by default, so a model never pays vision tokens up front.
When it actually needs to SEE one image, it calls image_get, which returns that single picture as a
native MCP image content block (only then are vision tokens spent). One self-documenting tool, by
graphic name, by the current selection, or page=<n> to render a whole page (_render_page_png below).
On Draw/Impress, page=N is the vision screenshot (0-based, same as slide_list / shape_get_tree); shape_get_tree remains the structure read.
"""

from __future__ import annotations

import base64
import copy
import logging
from typing import TYPE_CHECKING, Any, cast

from plugin.doc import visual_helpers
from plugin.framework.doc_type import CALC_DOCUMENT_SERVICE, DRAW_DOCUMENT_SERVICE, IMPRESS_DOCUMENT_SERVICE, TEXT_DOCUMENT_SERVICE
from plugin.framework.errors import reraise_if_disposed
from plugin.framework.tool import ToolBase
from plugin.writer.images.image_tools import export_graphic_object_to_bytes
from plugin.writer.page_png import render_writer_page_png, write_page_png

if TYPE_CHECKING:
    from plugin.framework.tool import ToolContext


log = logging.getLogger(__name__)


def _is_draw_family(doc: Any) -> bool:
    """True for Draw or Impress. Check PresentationDocument first: Impress also supports DrawingDocument."""
    return bool(doc.supportsService(IMPRESS_DOCUMENT_SERVICE)) or bool(doc.supportsService(DRAW_DOCUMENT_SERVICE))


def _ok_image(source: str, raw: bytes) -> dict[str, Any]:
    b64 = base64.b64encode(raw).decode("utf-8")
    return {"status": "ok", "source": source, "_mcp_image": {"data": b64, "mimeType": "image/png"}}


def _render_draw_page_png(ctx: Any, doc: Any, page: int) -> tuple[bytes | None, str | None]:
    """Render 0-based *page* of a Draw/Impress doc to PNG bytes, or (None, reason).

    *page* is the 0-based ``getByIndex`` index (first slide = 0). Applying ±1 would
    skip or mis-render slides because callers already pass the native index.

    Native ``com.sun.star.drawing.GraphicExportFilter`` with the XDrawPage as source (MediaType
    image/png). Verified live 2026-09-09: this yields a valid PNG and different pages produce
    different bytes. ``setCurrentPage`` + ``storeToURL(draw_png_Export / impress_png_Export)``
    produces the same image but mutates the view; FilterData PageNumber on the document model
    does not work (filter returns false). Fail-loud — never a silent empty image.
    """
    try:
        pages = doc.getDrawPages()
        total = int(pages.getCount())
    except Exception as e:
        reraise_if_disposed(e)
        return None, "could not render page %d: no draw pages available (%s)" % (page, e)

    if page < 0 or page >= total:
        return None, "could not render page %d: page not found (document has %d page(s))." % (page, total)

    try:
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
    except Exception as e:
        reraise_if_disposed(e)
        return None, "could not render page %d: %s" % (page, e)

    import uno
    from com.sun.star.beans import PropertyValue

    def export(tmp_path: str) -> None:
        ok = filt.filter((
            PropertyValue(Name="URL", Value=uno.systemPathToFileUrl(tmp_path)),
            PropertyValue(Name="MediaType", Value="image/png"),
        ))
        if not ok:
            raise RuntimeError("GraphicExportFilter returned false.")

    return write_page_png(page, export)


def _render_page_png(ctx: Any, doc: Any, page: int) -> tuple[bytes | None, str | None]:
    """Render 0-based *page* of Writer, Draw, or Impress to PNG bytes, or (None, reason)."""
    if _is_draw_family(doc):
        return _render_draw_page_png(ctx, doc, page)
    return render_writer_page_png(doc, page)


# Where the model reads graphic names, per doc_type family label (Impress is "draw").
# No core Calc read lists image names, so Calc points at the images domain.
_DEFAULT_IMAGE_NAME_SOURCE = "image_list (images domain)"
_IMAGE_NAME_SOURCE = {"writer": "get_content or image_list", "draw": "shape_get_tree", "calc": _DEFAULT_IMAGE_NAME_SOURCE}


def _one_mode(doc_type: str | None) -> str:
    """Return the exclusive-modes sentence; Calc has no page mode."""
    return "Pass at most one of image or selection." if doc_type == "calc" else "Pass at most one of image, selection, or page."


class GetImage(ToolBase):
    name: str | None = "image_get"
    tier: str = "core"
    description: str = (
        "Return an image so you can SEE it (vision-capable models). One of: image=<the graphic's name "
        "from shape_get_tree on Draw/Impress, or get_content / get_sheet_summary / image_list "
        "on Writer/Calc> for an embedded picture; selection=true for the image "
        "currently selected; or page=<n> (0-based) to render that whole PAGE as an image "
        "(Writer/Draw/Impress layout). On Draw/Impress, shape_get_tree is the shape tree — use page= "
        "here when you need to see the rendered page. Returns the picture itself, not a description. "
        "b64 is stripped from normal reads, so use this when you actually need to look. "
        "Pass at most one of image, selection, or page."
    )
    parameters: dict[str, Any] | None = {
        "type": "object",
        "properties": {
            "image": {"type": "string", "description": "Name of the embedded graphic to fetch (from shape_get_tree on Draw/Impress, or document reads / image_list). Pass at most one of image, selection, or page."},
            "selection": {"type": "boolean", "description": "If true, fetch the currently selected image instead of naming one. Pass at most one of image, selection, or page."},
            "page": {"type": "integer", "description": "0-based page/slide index (the user's page 2 is page=1). Renders that whole page layout as an image (Writer/Draw/Impress only). Pass at most one of image, selection, or page."},
            "sheet_index": {"type": "integer", "description": "Optional 0-based sheet index on Calc when fetching by image name (disambiguates duplicate names)."},
        },
        "required": [],
    }

    def get_description(self, doc_type: str | None = None) -> str:
        """Name only the reads and modes of *doc_type*; unprobed callers get the all-app text."""
        source = _IMAGE_NAME_SOURCE.get(doc_type or "")
        if source is None:
            return self.description
        if doc_type == "calc":
            modes = "for an embedded picture; or selection=true for the image currently selected."
        else:
            draw_note = " shape_get_tree is structure, not pixels; use page= to see the rendered page." if doc_type == "draw" else ""
            modes = (
                "for an embedded picture; selection=true for the image currently selected; or page=<n> (0-based) to render "
                f"that whole page as an image.{draw_note}"
            )
        return (
            f"Return an image so you can SEE it (vision-capable models). One of: image=<the graphic's name from {source}> "
            f"{modes} Returns the picture itself, not a description. "
            f"b64 is stripped from normal reads, so use this when you actually need to look. {_one_mode(doc_type)}"
        )

    def get_parameters(self, doc_type: str | None = None) -> dict[str, Any] | None:
        params = visual_helpers.without_sheet_index_off_calc(cast("dict[str, Any]", self.parameters), doc_type)
        source = _IMAGE_NAME_SOURCE.get(doc_type or "")
        if source is None:
            return params
        out = copy.deepcopy(params)
        one_mode = _one_mode(doc_type)
        out["properties"]["image"]["description"] = f"Name of the embedded graphic to fetch (from {source}). {one_mode}"
        if doc_type == "calc":
            # Calc has no per-page render: calc_png_Export goes through GraphicExportFilter, whose
            # gatherProperties reads only PixelWidth/PixelHeight, and DocumentToGraphicRenderer::getCurrentPage
            # returns page 1 for every non-Writer document. So page is left out of the Calc schema.
            del out["properties"]["page"]
            out["properties"]["selection"]["description"] = f"If true, fetch the currently selected image instead of naming one. {one_mode}"
        else:
            out["properties"]["page"]["description"] = f"0-based page index (the user's page 2 is page=1). Renders that whole page as an image. {one_mode}"
        return out

    # Impress also supports DrawingDocument; list both so execution accepts either service set.
    uno_services: list[str] | None = [
        TEXT_DOCUMENT_SERVICE,
        DRAW_DOCUMENT_SERVICE,
        IMPRESS_DOCUMENT_SERVICE,
        CALC_DOCUMENT_SERVICE,
    ]

    def execute(self, ctx: ToolContext, **kwargs: Any) -> dict[str, Any]:
        # page, image, and selection are exclusive; prepare_tool_call has already
        # rejected wrong types, so this body checks only ranges and combinations.
        doc = ctx.doc
        selection = kwargs.get("selection")

        name = kwargs.get("image")
        page = kwargs.get("page")
        sheet_index = kwargs.get("sheet_index")
        names_from = _IMAGE_NAME_SOURCE.get(ctx.doc_type or "", _DEFAULT_IMAGE_NAME_SOURCE)
        if sheet_index is not None:
            if sheet_index < 0:
                return self._tool_error("sheet_index must be a non-negative integer (0-based).")
            if name is None:
                return self._tool_error("sheet_index applies only with image=<name>.")
        if page is not None:
            if page < 0:
                return self._tool_error("page must be a non-negative integer (0-based).")
            if name is not None or selection is True:
                return self._tool_error(_one_mode(ctx.doc_type))
            raw, reason = _render_page_png(ctx.ctx, doc, page)
            if raw is None:
                return self._tool_error(reason or "Could not render the page.")
            return _ok_image(f"page {page}", raw)

        if name is not None:
            if not name.strip():
                return self._tool_error("image must be a non-empty string (the graphic's name).")
            if selection is True:
                return self._tool_error(_one_mode(ctx.doc_type))
            lookup_kwargs: dict[str, Any] = {}
            if sheet_index is not None:
                lookup_kwargs["sheet_index"] = sheet_index
            try:
                obj = visual_helpers.get_graphic_object_by_name(doc, name, **lookup_kwargs)
            except visual_helpers.AmbiguousGraphicNameError as exc:
                extra: dict[str, Any] = {"image_name": name, "sheet_indices": exc.sheet_indices}
                if exc.shape_indices:
                    extra["shape_indices"] = exc.shape_indices
                return self._tool_error(
                    str(exc),
                    code="AMBIGUOUS_NAME",
                    **extra,
                )
            if obj is None:
                return self._tool_error(f"No embedded image named '{name}'. Read the available names with {names_from}.")
            raw = export_graphic_object_to_bytes(ctx.ctx, obj)
            if not raw:
                return self._tool_error(f"Could not export image '{name}'.")
            source = name
        elif selection is False:
            page_mode = "" if ctx.doc_type == "calc" else "page=<n>, "
            return self._tool_error(f"selection=false means no mode was chosen; pass image=<name>, {page_mode}or selection=true.")
        else:
            no_image = f"No image selected. Select an image in the document, or pass image=<name> (read names with {names_from})."
            obj = visual_helpers.selected_graphic_object(doc)
            if obj is None:
                log.debug("get_selected_image_base64: no graphic in selection")
                return self._tool_error(no_image)
            raw = export_graphic_object_to_bytes(ctx.ctx, obj)
            if not raw:
                return self._tool_error(no_image)
            source = "selection"
        # _mcp_image is recognized by the MCP tools/call serializer (mcp_protocol) and returned as a
        # native image content block; over non-image transports it is just an ignorable marker.
        return _ok_image(source, raw)
