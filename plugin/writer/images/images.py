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
"""Writer image generation and editing tools.

The seven image tools stay separate; an action-union schema is not the plan.
"""

import hashlib
import logging
import os
import socket
import ssl
import tempfile
import time
import typing
from typing import Any
import urllib.parse
import urllib.request

from plugin.framework.errors import (
    ToolExecutionError,
    best_effort,
    reraise_if_disposed,
    resolve_exception_message,
)
from plugin.framework.queue_executor import execute_on_main_thread, _SCOPE_UNSET
from plugin.framework.thread_guard import on_main_thread
from ..specialized_base import ToolWriterImageBase, enum_param

def _run_on_main(
    fn: typing.Any,
    *args: typing.Any,
    timeout: float = 60.0,
    bound_scope: typing.Any = _SCOPE_UNSET,
    **kwargs: typing.Any,
) -> Any:
    if on_main_thread():
        return fn(*args, **kwargs)
    return execute_on_main_thread(fn, *args, timeout=timeout, bound_scope=bound_scope, **kwargs)

# Queue pickup bound for document mutations when bytes are already in hand.
# Independent of the HTTP read budget so a short network timeout doesn't drop an insert.
_DOCUMENT_MARSHAL_TIMEOUT_SEC = 120.0
from .image_utils import (
    IMAGE_CONTENT_TYPE_DENYLIST,
    IMAGE_DOWNLOAD_MAX_BYTES,
    ImageService,
    validated_image_suffix,
)
from . import image_mark, image_tools
from plugin.framework.config import get_config_int, get_config_bool, get_config_str
from plugin.framework.config_schema import DEFAULT_IMAGE_BASE_SIZE
from plugin.framework.client.base_provider_shim import canonical_aspect_ratio
from plugin.framework.client.model_fetcher import get_image_model
from plugin.framework.constants import USER_AGENT
from plugin.framework.model_lru import update_lru_history
from plugin.doc.document_research import list_nearby_files
from plugin.doc import visual_helpers
from .image_tools import (
    _cursor_for_writer_view,
    add_image_to_gallery,
    export_graphic_object_to_bytes,
    insert_image,
    insert_image_at_locator,
    insert_image_into_header_footer,
    replace_graphic_source,
)

log = logging.getLogger("writeragent.writer")

_IMAGE_DOWNLOAD_CHUNK = 64 * 1024


def _optional_property(obj: Any, name: str, default: Any = None) -> Any:
    """Property some shapes do not have. Disposal is not a missing property."""
    try:
        return obj.getPropertyValue(name)
    except Exception as exc:
        reraise_if_disposed(exc)
        return default


def _orient_number(raw: Any) -> int | None:
    # 0 is HoriOrientation.NONE. Do not treat 0 as missing.
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def resolve_image_generate_is_edit(source_image: typing.Any, *, selection_available: bool) -> bool:
    """True when image_generate should img2img the selected graphic.

    Explicit ``source_image='selection'`` always edits. Omitting ``source_image``
    also edits when a graphic is selected: the parent often rewrites
    "make it look like a wizard" into a generate-new task and drops the
    argument; defaulting to the selection keeps ``replace_image_in_place``.
    Create-new when nothing is selected (clear the selection to force create).
    """
    if isinstance(source_image, str):
        source_image = source_image.strip() or None
    if source_image and str(source_image).lower() == "selection":
        return True
    return source_image is None and selection_available


def resolve_generation_size(
    base_size: int,
    aspect: str | None,
    width: int | None = None,
    height: int | None = None,
) -> tuple[int, int, str | None]:
    """Resolve pixel dimensions and wire aspect ratio for image generation."""
    normalized = canonical_aspect_ratio(named=aspect) if aspect else "1:1"
    if not normalized:
        normalized = "1:1"

    num, den = map(int, normalized.split(":"))

    if width is not None and height is not None:
        final_w = width
        final_h = height
    elif width is not None:
        final_w = width
        final_h = round(width * den / num)
    elif height is not None:
        final_w = round(height * num / den)
        final_h = height
    else:
        if num > den:
            calc_w = int(base_size * num / den)
            calc_h = base_size
        elif num < den:
            calc_w = base_size
            calc_h = int(base_size * den / num)
        else:
            calc_w = base_size
            calc_h = base_size

        calc_w = (calc_w // 64) * 64 or 64
        calc_h = (calc_h // 64) * 64 or 64
        final_w = calc_w
        final_h = calc_h

    wire_ratio = canonical_aspect_ratio(final_w, final_h)
    return final_w, final_h, wire_ratio


class ImageGenerate(ToolWriterImageBase):
    """Generate a new image from a prompt, or edit an existing image (Img2Img)."""

    name: str | None = "image_generate"
    intent: str | None = "media"
    description: str = (
        "Generate an image from a text prompt using the Settings endpoint and insert it. "
        "To edit an existing image, pass source_image='selection' (or omit it while a graphic is selected)."
    )
    parameters: dict[str, Any] | None = {
        "type": "object",
        "properties": {
            "prompt": {"type": "string", "description": "Descriptive prompt for image generation or editing"},
            "source_image": {"type": "string", "description": (
                "Optional. Use 'selection' to edit the currently selected image (Img2Img). "
                "Omit while a graphic is selected to edit in place; omit with no selection to create a new image."
            )},
            "aspect_ratio": {"type": "string", "enum": ["square", "landscape_16_9", "portrait_9_16", "landscape_3_2", "portrait_2_3", "1:1", "4:3", "3:4", "16:9", "9:16"], "default": "square"},
            "base_size": {
                "type": "integer",
                "description": (
                    "Generate resolution in pixels (> 0, default 1024). "
                    "On-page display is capped independently (~135mm longer edge)."
                ),
                "default": DEFAULT_IMAGE_BASE_SIZE,
            },
            "width": {"type": "integer", "description": "Optional width in pixels (> 0). Overrides base_size / aspect_ratio resolution."},
            "height": {"type": "integer", "description": "Optional height in pixels (> 0). Overrides base_size / aspect_ratio resolution."},
            "image_model": {"type": "string", "description": "Override Settings image model for this request (endpoint / OpenRouter)."},
        },
        "required": ["prompt"],
    }
    is_mutation: bool | None = True
    long_running: bool = True

    def is_async(self) -> bool:
        """HTTP generation runs on the tool worker; UNO is marshalled in execute()."""
        return True

    def execute(self, ctx: typing.Any, **args: typing.Any) -> typing.Any:
        prompt = args.get("prompt", "")

        base_size_arg = args.get("base_size")
        if base_size_arg is not None and base_size_arg <= 0:
            return self._tool_error("base_size must be greater than 0.", code="INVALID_PARAMETER", parameter="base_size")
        width_arg = args.get("width")
        if width_arg is not None and width_arg <= 0:
            return self._tool_error("width must be greater than 0.", code="INVALID_PARAMETER", parameter="width")
        height_arg = args.get("height")
        if height_arg is not None and height_arg <= 0:
            return self._tool_error("height must be greater than 0.", code="INVALID_PARAMETER", parameter="height")

        status_callback = getattr(ctx, "status_callback", None)
        stop_checker = getattr(ctx, "stop_checker", None)
        mt_timeout = float(get_config_int("request_timeout"))
        add_to_gallery = get_config_bool("image_auto_gallery")
        add_frame = get_config_bool("image_insert_frame")

        source_image = args.get("source_image")
        if isinstance(source_image, str):
            source_image = source_image.strip() or None

        if source_image is not None and (not isinstance(source_image, str) or source_image.lower() != "selection"):
            return self._tool_error(
                "Invalid source_image: only 'selection' or omitted is supported.",
                code="INVALID_PARAMETER",
                parameter="source_image",
            )

        explicit_edit = bool(source_image and source_image.lower() == "selection")
        target_obj = None
        source_b64 = None
        edit_width, edit_height = DEFAULT_IMAGE_BASE_SIZE, DEFAULT_IMAGE_BASE_SIZE
        display_width, display_height = DEFAULT_IMAGE_BASE_SIZE, DEFAULT_IMAGE_BASE_SIZE
        is_edit = False

        # Peek selection when the caller asked to edit, or omitted source_image
        # (omitted + selected graphic → img2img; omitted + no selection → create).
        if explicit_edit or source_image is None:

            def _read_selection_for_edit() -> tuple[str, tuple[Any, str, int, int, int, int] | None]:
                # Export failures propagate. execute_safe logs once and maps them
                # to TOOL_EXECUTION_ERROR, or DOCUMENT_DISPOSED. An empty export
                # is "no selection", not a failed read.
                obj = visual_helpers.selected_graphic_object(ctx.doc)
                if obj is None:
                    return ("no_selection", None)
                try:
                    png_bytes = export_graphic_object_to_bytes(ctx.ctx, obj)
                except Exception as exc:
                    reraise_if_disposed(exc)
                    raise
                if not png_bytes:
                    return ("no_selection", None)
                import base64

                b64 = base64.b64encode(png_bytes).decode("ascii")

                # Native pixel size for generation resolution
                graphic = visual_helpers.graphic_from_object(obj)
                nw, nh = None, None
                if graphic is not None:
                    try:
                        px = visual_helpers.safe_get_property(graphic, "SizePixel")
                        if px is None and hasattr(graphic, "getPropertyValue"):
                            px = graphic.getPropertyValue("SizePixel")
                        if px is not None and getattr(px, "Width", 0) > 0 and getattr(px, "Height", 0) > 0:
                            nw, nh = int(px.Width), int(px.Height)
                    except Exception as ex:
                        reraise_if_disposed(ex)
                        log.debug("Failed to read SizePixel: %s", ex)

                # Frame size in 1/100 mm for on-page display
                frame_w, frame_h = None, None
                try:
                    size = _object_size(obj)
                    if size is not None and getattr(size, "Width", 0) > 0 and getattr(size, "Height", 0) > 0:
                        frame_w, frame_h = int(size.Width), int(size.Height)
                except Exception as ex:
                    reraise_if_disposed(ex)
                    log.debug("Failed to read frame size: %s", ex)

                if nw is None or nh is None:
                    if frame_w is not None and frame_h is not None:
                        nw, nh = visual_helpers.units_to_px(frame_w, frame_h)
                    else:
                        nw, nh = DEFAULT_IMAGE_BASE_SIZE, DEFAULT_IMAGE_BASE_SIZE
                if frame_w is None or frame_h is None:
                    frame_w, frame_h = visual_helpers.px_to_display_units(nw, nh)

                return ("ok", (obj, b64, nw, nh, frame_w, frame_h))

            tag, payload = _run_on_main(_read_selection_for_edit, timeout=mt_timeout)
            selection_available = tag == "ok"
            is_edit = resolve_image_generate_is_edit(source_image, selection_available=selection_available)
            if explicit_edit and not selection_available:
                return self._tool_error("No image selected. Please select an image in the document first.", code="NO_SELECTION", action="edit_image")
            if is_edit:
                picked = typing.cast("tuple[Any, str, int, int, int, int]", payload)
                target_obj, source_b64, edit_width, edit_height = (picked[0], str(picked[1]), int(picked[2]), int(picked[3]))
                display_width, display_height = int(picked[4]), int(picked[5])

        base_size = args.get("base_size", get_config_int("image_base_size"))

        if is_edit:
            arg_w = args.get("width")
            arg_h = args.get("height")
            if arg_w is not None and arg_h is not None:
                width = arg_w
                height = arg_h
            elif arg_w is not None:
                width = arg_w
                height = round(arg_w * edit_height / edit_width) if edit_width > 0 else edit_height
            elif arg_h is not None:
                height = arg_h
                width = round(arg_h * edit_width / edit_height) if edit_height > 0 else edit_width
            else:
                width = edit_width
                height = edit_height
            wire_ratio = None
        else:
            aspect = args.get("aspect_ratio") or get_config_str("image_default_aspect")
            width, height, wire_ratio = resolve_generation_size(
                base_size,
                aspect,
                width=args.get("width"),
                height=args.get("height"),
            )

        image_svc = ImageService(ctx.ctx, config=None)
        gen_kwargs: dict[str, Any] = {}
        image_model = args.get("image_model")
        if image_model:
            gen_kwargs["image_model"] = image_model
        if is_edit:
            gen_kwargs["source_image"] = source_b64

        try:
            paths, error_msg = image_svc.generate_image(
                prompt,
                width=width,
                height=height,
                aspect_ratio=wire_ratio,
                status_callback=status_callback,
                stop_checker=stop_checker,
                **gen_kwargs,
            )
        except ToolExecutionError as exc:
            if getattr(exc, "code", None) == "USER_STOPPED":
                return self._tool_error(str(exc), code="USER_STOPPED")
            raise

        if not paths:
            return self._tool_error(error_msg or "No image returned.", code="PROVIDER_ERROR", provider="endpoint")

        img_path = paths[0]

        def _insert_or_replace() -> str:
            # Bytes are already in hand. Stop may abort the network wait above;
            # it does not skip this document insert.
            if is_edit:
                replaced = False
                try:
                    if target_obj is not None:
                        replaced = replace_graphic_source(
                            ctx.ctx,
                            ctx.doc,
                            target_obj,
                            img_path,
                            display_width,
                            display_height,
                            title=prompt,
                            description="Edited by endpoint",
                        )
                        if replaced and add_to_gallery:
                            add_image_to_gallery(ctx.ctx, img_path)
                except Exception as ex:
                    reraise_if_disposed(ex)
                    log.debug("replace_graphic_source failed: %s", ex)
                    replaced = False

                if not replaced:
                    insert_image(
                        ctx.ctx,
                        ctx.doc,
                        img_path,
                        width,
                        height,
                        title=prompt,
                        description="Edited by endpoint",
                        add_to_gallery=add_to_gallery,
                        add_frame=add_frame,
                        size_units=(display_width, display_height),
                    )
                return "Image edited and inserted from endpoint."
            insert_image(ctx.ctx, ctx.doc, img_path, width, height, title=prompt, description="Generated by endpoint", add_to_gallery=add_to_gallery, add_frame=add_frame)
            return "Image generated and inserted from endpoint."

        # _run_on_main defaults bound_scope to the current send scope, so Stop would
        # cancel the queued insert after the bytes are downloaded. Insert unscoped.
        # Stop after the download must not turn a finished insert into an error.
        try:
            msg = _run_on_main(_insert_or_replace, timeout=_DOCUMENT_MARSHAL_TIMEOUT_SEC, bound_scope=None)
        finally:
            from plugin.writer.images.image_utils import _unlink_temp_paths

            _unlink_temp_paths(paths)

        image_model_used = str(args.get("image_model") or get_image_model() or "").strip()
        if image_model_used:
            endpoint = get_config_str("endpoint").strip()
            update_lru_history(image_model_used, "image_model_lru", endpoint)

        return {"status": "ok", "message": msg}


class _DownloadStopped(Exception):
    pass


def _download_for_tool(
    tool: Any,
    url: str,
    *,
    verify_ssl: bool = True,
    force: bool = False,
    stop_checker: Any = None,
) -> str | dict[str, Any]:
    try:
        return _download_image_to_cache(url, verify_ssl=verify_ssl, force=force, stop_checker=stop_checker)
    except _DownloadStopped:
        return tool._tool_error("Image download stopped by user.", code="USER_STOPPED", url=url)
    except Exception as exc:
        log.exception("image download failed")
        return tool._tool_error("Could not download image: %s" % exc, code="DOWNLOAD_FAILED", url=url)


# Bugfix: Setting only width_mm or only height_mm left the unpassed dimension
# at its old absolute size, distorting the aspect ratio. When only one dimension
# is specified, we scale the other side proportionally to preserve the aspect ratio.
def _replacement_size_units(
    current_width: int,
    current_height: int,
    width_mm: int | float | None,
    height_mm: int | float | None,
) -> tuple[int, int]:
    if width_mm is not None and height_mm is not None:
        return visual_helpers.mm_to_unit(width_mm), visual_helpers.mm_to_unit(height_mm)
    if width_mm is not None:
        width_units = visual_helpers.mm_to_unit(width_mm)
        if current_width == 0:
            height_units = int(current_height)
        else:
            height_units = max(1, int(round(width_units * current_height / current_width)))
        return width_units, height_units
    if height_mm is not None:
        height_units = visual_helpers.mm_to_unit(height_mm)
        if current_height == 0:
            width_units = int(current_width)
        else:
            width_units = max(1, int(round(height_units * current_width / current_height)))
        return width_units, height_units
    return int(current_width), int(current_height)


# ------------------------------------------------------------------
# ImageListNearbyFiles
# ------------------------------------------------------------------


class ImageListNearbyFiles(ToolWriterImageBase):
    """List image files in the active document's directory (images delegate only)."""

    name: str | None = "image_list_nearby_files"
    intent: str | None = "media"
    description: str = (
        "List image files (.png, .jpg, .jpeg, .gif, .webp, .bmp, .svg) in the same folder as the active document "
        "(newest first). Excludes the active file. Use returned path with image_insert or image_replace. "
        "Optional filter is a case-insensitive substring on the basename."
    )
    parameters: dict[str, Any] | None = {
        "type": "object",
        "properties": {
            "filter": {"type": "string", "description": "Optional basename substring (e.g. 'logo')."},
        },
        "required": [],
    }

    is_mutation: bool | None = False

    def is_async(self) -> bool:
        return True

    def execute(self, ctx: typing.Any, **kwargs: typing.Any) -> typing.Any:
        filt = kwargs.get("filter")

        def _run() -> dict[str, typing.Any]:
            return list_nearby_files(ctx.ctx, ctx.doc, filter=filt, file_kind="images")

        return _run_on_main(_run)


# ------------------------------------------------------------------
# ImageList
# ------------------------------------------------------------------


def _is_at_page(graphic: Any) -> bool:
    """True when graphic is anchored AT_PAGE."""
    try:
        from com.sun.star.text.TextContentAnchorType import AT_PAGE

        anchor_type = graphic.getPropertyValue("AnchorType")
        return bool(anchor_type == AT_PAGE or (isinstance(anchor_type, int) and anchor_type == 2))
    except Exception as e:
        reraise_if_disposed(e)
        return False


class ImageList(ToolWriterImageBase):
    """List all images/graphic objects in the document."""

    name: str | None = "image_list"
    intent: str | None = "media"
    description: str = "List all images/graphic objects in the document with name, dimensions, title, and description."
    parameters: dict[str, Any] | None = {"type": "object", "properties": {}, "required": []}


    def execute(self, ctx: typing.Any, **kwargs: typing.Any) -> dict[str, Any]:
        doc = ctx.doc
        doc_type = visual_helpers.get_visual_doc_type(doc)
        is_calc = doc_type == "calc"
        is_draw = doc_type in ("draw", "impress")
        if not is_calc and not is_draw and not hasattr(doc, "getGraphicObjects"):
            return self._tool_error("Document does not support graphic objects.")
        graphics_names = visual_helpers.list_graphic_objects(doc, doc_type=doc_type)

        doc_svc = getattr(ctx.services, "document", None)
        para_ranges = None
        text_obj = None
        entries = None
        if not is_calc and not is_draw and doc_svc:
            para_ranges = doc_svc.get_paragraph_ranges(doc)
            from plugin.doc.paragraph_search import paragraph_entries

            entries = paragraph_entries(para_ranges)
            text_obj = doc.getText()

        images = []
        skipped: list[dict[str, str]] = []

        vc = None
        saved_vc = None
        vc_saved_attempted = False

        try:
            for item in graphics_names:
                name, graphic = item[0], item[1]
                try:
                    size = _object_size(graphic)
                    title = _optional_property(graphic, "Title", "")
                    description = _optional_property(graphic, "Description", "")

                    # Read anchor once and reuse for paragraph index and page
                    anchor = None
                    paragraph_index = -1
                    if not is_calc and not is_draw:
                        try:
                            anchor = graphic.getAnchor()
                            if doc_svc and anchor is not None and para_ranges is not None:
                                raw_pi = doc_svc.find_paragraph_for_range(anchor, para_ranges, text_obj, entries=entries)
                                from plugin.doc.paragraph_search import confirm_paragraph_index

                                placed = confirm_paragraph_index(text_obj, anchor, para_ranges, raw_pi)
                                if placed is not None:
                                    paragraph_index = placed
                        except Exception as exc:
                            reraise_if_disposed(exc)

                    # Page number
                    page = None
                    entry_pos: dict[str, typing.Any] = {}
                    if is_draw:
                        try:
                            pos = graphic.getPosition()
                            entry_pos = {"x_mm": pos.X / 100.0, "y_mm": pos.Y / 100.0}
                        except Exception as exc:
                            reraise_if_disposed(exc)
                    elif not is_calc:
                        if _is_at_page(graphic):
                            page_no = _optional_property(graphic, "AnchorPageNo", 0)
                            try:
                                p = int(page_no)
                                if p > 0:
                                    page = p
                            except (TypeError, ValueError):
                                pass
                        else:
                            if not vc_saved_attempted:
                                vc_saved_attempted = True
                                try:
                                    ctrl = doc.getCurrentController()
                                    vc = ctrl.getViewCursor() if ctrl is not None else None
                                    if vc is not None:
                                        _, saved_vc = _cursor_for_writer_view(vc)
                                except Exception:
                                    saved_vc = None
                            try:
                                if saved_vc is not None and anchor is not None and vc is not None:
                                    start = anchor.getStart()
                                    if start is not None:
                                        vc.gotoRange(start, False)
                                        try:
                                            p = int(vc.getPage())
                                            if p > 0:
                                                page = p
                                        except (TypeError, ValueError):
                                            pass
                            except Exception as exc:
                                reraise_if_disposed(exc)
                    else:
                        entry_pos = {}

                    entry = {"name": name, "width_mm": size.Width / 100.0, "height_mm": size.Height / 100.0, "width_100mm": size.Width, "height_100mm": size.Height, "title": title, "description": description, "paragraph_index": paragraph_index}
                    if is_calc:
                        s_idx = getattr(item, "sheet_index", getattr(graphic, "_calc_sheet_index", None))
                        s_name = getattr(item, "sheet_name", getattr(graphic, "_calc_sheet_name", None))
                        if s_idx is not None:
                            entry["sheet_index"] = s_idx
                        if s_name is not None:
                            entry["sheet_name"] = s_name
                    if page is not None:
                        entry["page"] = page
                    if is_draw:
                        entry.update(entry_pos)
                    images.append(entry)
                except Exception as exc:
                    reraise_if_disposed(exc)
                    log.exception("image_list: could not read '%s'", name)
                    skipped.append({"name": name, "error": resolve_exception_message(exc)})
        finally:
            if vc is not None and saved_vc is not None:
                best_effort(lambda: vc.gotoRange(saved_vc, False), "image_list: restore view cursor failed")

        result: dict[str, Any] = {"status": "ok", "images": images, "count": len(images)}
        if skipped:
            result["skipped"] = skipped
        return result


# ------------------------------------------------------------------
# ImageGetInfo
# ------------------------------------------------------------------


def _get_graphic_object(ctx: typing.Any, doc: typing.Any, image_name: str, sheet_index: int | None = None) -> Any | None:
    return visual_helpers.get_graphic_object_by_name(doc, image_name, sheet_index=sheet_index)


def _resolve_graphic(tool: Any, ctx: Any, image_name: str, sheet_index: int | None = None) -> tuple[Any, dict[str, Any] | None]:
    try:
        graphic = _get_graphic_object(ctx, ctx.doc, image_name, sheet_index=sheet_index)
    except ValueError as exc:
        sheets = getattr(exc, "sheet_indices", None)
        return None, tool._tool_error(str(exc), code="AMBIGUOUS_NAME", image_name=image_name, sheet_indices=sheets, sheets=sheets)
    if not graphic:
        return None, tool._tool_error("Image '%s' not found or document does not support graphic objects." % image_name, code="IMAGE_NOT_FOUND", image_name=image_name)
    return graphic, None


def _validate_dimensions(tool: Any, kwargs: dict[str, Any]) -> dict[str, Any] | None:
    width_mm_val = kwargs.get("width_mm")
    if width_mm_val is not None and width_mm_val <= 0:
        return tool._tool_error("width_mm must be greater than 0.", code="INVALID_PARAMETER", parameter="width_mm")
    height_mm_val = kwargs.get("height_mm")
    if height_mm_val is not None and height_mm_val <= 0:
        return tool._tool_error("height_mm must be greater than 0.", code="INVALID_PARAMETER", parameter="height_mm")
    return None


def _object_size(obj: typing.Any) -> Any:
    """Frame size in 1/100 mm via getSize(), which matches the Size property wherever it exists."""
    return obj.getSize()


class ImageGetInfo(ToolWriterImageBase):
    """Get detailed info about a specific image."""

    name: str | None = "image_get_info"
    intent: str | None = "media"
    description: str = "Get detailed info about a specific image: URL, dimensions (mm, and width_px/height_px of the picture itself), anchor type, orientation, crop (crop_mm, mm trimmed per edge), hyperlink_url (the image link, including an internal target), and paragraph index."
    parameters: dict[str, Any] | None = {
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "Name of the image (from image_list)."},
            "sheet_index": {"type": "integer", "description": "Optional 0-based sheet index on Calc."},
        },
        "required": ["name"],
    }


    def execute(self, ctx: typing.Any, **kwargs: typing.Any) -> dict[str, Any]:
        image_name = kwargs["name"]
        sheet_index = kwargs.get("sheet_index")

        graphic, err = _resolve_graphic(self, ctx, image_name, sheet_index=sheet_index)
        if err is not None:
            return err

        size = _object_size(graphic)

        # GraphicObjectFillBitmap is an XGraphic, not a URL. str() of it was a pyuno repr.
        # Keep GraphicURL only when it is already a string.
        raw_url = _optional_property(graphic, "GraphicURL", "")
        graphic_url = raw_url if isinstance(raw_url, str) else ""

        # Anchor type
        anchor_type = visual_helpers.anchor_type_index(_optional_property(graphic, "AnchorType", None))

        # Orientation
        hori_orient = _orient_number(_optional_property(graphic, "HoriOrient", None))
        vert_orient = _orient_number(_optional_property(graphic, "VertOrient", None))

        # Title / description
        title = _optional_property(graphic, "Title", "")
        description = _optional_property(graphic, "Description", "")

        # Pixel frame that image_crop_and_highlight boxes refer to.
        width_px = height_px = None
        source = visual_helpers.graphic_from_object(graphic)
        if source is not None:
            try:
                width_px, height_px = image_mark.pixel_size(source)
            except Exception as exc:
                reraise_if_disposed(exc)

        # Crop (mm trimmed from each edge), so a reader/agent can see and adjust it.
        crop_mm = None
        try:
            cc = graphic.getPropertyValue("GraphicCrop")
            if cc is not None and (cc.Top or cc.Bottom or cc.Left or cc.Right):
                crop_mm = {"top": cc.Top / 100.0, "bottom": cc.Bottom / 100.0, "left": cc.Left / 100.0, "right": cc.Right / 100.0}
        except Exception as exc:
            reraise_if_disposed(exc)

        # What was wrong: image_get_info never reported an image hyperlink, so a
        # footer button whose ODT link lives on the enclosing draw:a looked unlinked.
        # LibreOffice maps that href onto HyperLinkURL (BaseFrameProperties).
        # Shapes without the property (some Calc/Draw pictures) stay "".
        raw_link = visual_helpers.safe_get_property(graphic, "HyperLinkURL", "")
        hyperlink_url = raw_link if isinstance(raw_link, str) else ""

        # Paragraph index via anchor
        paragraph_index = -1
        is_writer = visual_helpers.get_visual_doc_type(ctx.doc) in ("writer", "web")
        if is_writer:
            try:
                anchor = graphic.getAnchor()
                doc_svc = ctx.services.document
                para_ranges = doc_svc.get_paragraph_ranges(ctx.doc)
                text_obj = ctx.doc.getText()
                raw_pi = doc_svc.find_paragraph_for_range(anchor, para_ranges, text_obj)
                from plugin.doc.paragraph_search import confirm_paragraph_index

                placed = confirm_paragraph_index(text_obj, anchor, para_ranges, raw_pi)
                if placed is not None:
                    paragraph_index = placed
            except Exception as exc:
                reraise_if_disposed(exc)

        return {
            "status": "ok",
            "image_name": image_name,
            "graphic_url": graphic_url,
            "width_mm": size.Width / 100.0,
            "height_mm": size.Height / 100.0,
            "width_100mm": size.Width,
            "height_100mm": size.Height,
            "width_px": width_px,
            "height_px": height_px,
            "anchor_type": anchor_type,
            "hori_orient": hori_orient,
            "vert_orient": vert_orient,
            "title": title,
            "description": description,
            "crop_mm": crop_mm,
            "hyperlink_url": hyperlink_url,
            "paragraph_index": paragraph_index,
        }


# ------------------------------------------------------------------
# ImageSetProperties
# ------------------------------------------------------------------


# Friendly position names -> UNO orientation constants. Imported by name (never hard-coded ints) so
# the values always match this LibreOffice build. Resolved lazily in _resolve_orient().
_HORI_ORIENT_NAMES = ("left", "center", "right")
_VERT_ORIENT_NAMES = ("top", "center", "bottom")


# Published UNO constant values (com.sun.star.text.HoriOrientation / VertOrientation) — a stable,
# documented part of the UNO API. Used ONLY as a fallback when the live enum import is unavailable
# (e.g. headless tests); production resolves through the live import below, so production never
# depends on these literals being right — they just keep the name-mapping testable without UNO.
_HORI_ORIENT_FALLBACK = {"left": 3, "center": 2, "right": 1}
_VERT_ORIENT_FALLBACK = {"top": 1, "center": 2, "bottom": 3}


def _resolve_orient(value: typing.Any, axis: str) -> tuple[Any | None, str | None]:
    """Map a friendly position to a UNO orientation constant. *axis* is 'hori' or 'vert'.

    Accepts a name ('left'/'center'/'right' for hori; 'top'/'center'/'bottom' for vert; 'centre' ok)
    OR a raw UNO integer constant (back-compat). Returns (constant, None) on success or
    (None, error_message) on an unknown name. The name is validated first (no UNO needed); the value
    is then read from the LIVE UNO enum, falling back to the published constant only if that import
    is unavailable."""
    if isinstance(value, bool):  # guard: bool is an int subclass
        return None, "orientation must be a position name or an integer, not a boolean."
    if isinstance(value, int):
        return value, None  # raw UNO constant — pass through (back-compat)
    if not isinstance(value, str):
        return None, "orientation must be a position name (e.g. 'center') or an integer."
    name = value.strip().lower()
    if name == "centre":
        name = "center"
    valid = _HORI_ORIENT_NAMES if axis == "hori" else _VERT_ORIENT_NAMES
    if name not in valid:
        return None, f"unknown {axis} position '{value}'. Use one of: {', '.join(valid)}."
    group = "com.sun.star.text.HoriOrientation" if axis == "hori" else "com.sun.star.text.VertOrientation"
    member = name.upper()  # left/center/right/top/bottom -> LEFT/CENTER/RIGHT/TOP/BOTTOM
    try:
        import uno
        return uno.getConstantByName("%s.%s" % (group, member)), None
    except Exception:
        fallback = _HORI_ORIENT_FALLBACK if axis == "hori" else _VERT_ORIENT_FALLBACK
        return fallback[name], None


def _resolve_crop_edges(kwargs: typing.Any, current: typing.Any) -> tuple[int, int, int, int]:
    """Return (top, bottom, left, right) in 1/100 mm for the GraphicCrop struct. Each crop_*_mm
    given (in mm) overrides that edge; edges not passed keep their current value. `current` is the
    existing crop as a 4-tuple (top, bottom, left, right) in 1/100 mm. Pure — unit-testable."""
    def _edge(key: str, cur: typing.Any) -> int:
        mm = kwargs.get(key)
        return int(round(mm * 100)) if mm is not None else int(cur)

    return (
        _edge("crop_top_mm", current[0]),
        _edge("crop_bottom_mm", current[1]),
        _edge("crop_left_mm", current[2]),
        _edge("crop_right_mm", current[3]),
    )


class ImageSetProperties(ToolWriterImageBase):
    """Resize, reposition, crop, or update caption/alt-text for an image."""

    name: str | None = "image_set_properties"
    intent: str | None = "media"
    description: str = "Resize, reposition, crop, or update caption/alt-text for an image. Crop trims the given millimetres off each edge (crop_*_mm); only the edges you pass change."
    parameters: dict[str, Any] | None = {
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "Name of the image (from image_list)."},
            "width_mm": {"type": "number", "description": "New width in millimetres (> 0)."},
            "height_mm": {"type": "number", "description": "New height in millimetres (> 0)."},
            "title": {"type": "string", "description": "Image title (tooltip text)."},
            "description": {"type": "string", "description": "Image alternative text (alt-text)."},
            "anchor_type": {
                "type": "integer",
                "enum": [0, 1, 2, 3, 4],
                "description": "Anchor type: 0=AT_PARAGRAPH, 1=AS_CHARACTER, 2=AT_PAGE, 3=AT_FRAME, 4=AT_CHARACTER.",
            },
            "hori_orient": {"type": "string", "description": "Horizontal position: 'left', 'center', or 'right'. (A raw UNO HoriOrientation integer is also accepted.)"},
            "vert_orient": {"type": "string", "description": "Vertical position: 'top', 'center', or 'bottom'. (A raw UNO VertOrientation integer is also accepted.)"},
            "crop_top_mm": {"type": "number", "description": "Millimetres to crop off the TOP edge (0 = no crop). Only edges you pass change."},
            "crop_bottom_mm": {"type": "number", "description": "Millimetres to crop off the BOTTOM edge."},
            "crop_left_mm": {"type": "number", "description": "Millimetres to crop off the LEFT edge."},
            "crop_right_mm": {"type": "number", "description": "Millimetres to crop off the RIGHT edge."},
            "sheet_index": {"type": "integer", "description": "Optional 0-based sheet index on Calc."},
        },
        "required": ["name"],
    }

    is_mutation: bool | None = True

    def execute(self, ctx: typing.Any, **kwargs: typing.Any) -> dict[str, Any]:
        image_name = kwargs["name"]
        if not image_name:
            return self._tool_error("name is required.", code="MISSING_PARAMETER", parameter="name")

        dim_err = _validate_dimensions(self, kwargs)
        if dim_err is not None:
            return dim_err

        anchor_type = kwargs.get("anchor_type")
        if anchor_type is not None and (type(anchor_type) is not int or anchor_type not in (0, 1, 2, 3, 4)):
            return self._tool_error(
                "anchor_type must be an integer 0, 1, 2, 3, or 4.",
                code="INVALID_PARAMETER",
                parameter="anchor_type",
            )

        sheet_index = kwargs.get("sheet_index")
        graphic, resolve_err = _resolve_graphic(self, ctx, image_name, sheet_index=sheet_index)
        if resolve_err is not None:
            return resolve_err

        # Orientation accepts friendly names ('left'/'center'/'right', 'top'/'center'/'bottom') or
        # raw UNO ints (back-compat), resolved via the build's own constants. Resolve before any write.
        hori_orient = kwargs.get("hori_orient")
        resolved_hori = None
        if hori_orient is not None:
            resolved_hori, err = _resolve_orient(hori_orient, "hori")
            if err:
                return self._tool_error(err, code="INVALID_PARAMETER", parameter="hori_orient")

        vert_orient = kwargs.get("vert_orient")
        resolved_vert = None
        if vert_orient is not None:
            resolved_vert, err = _resolve_orient(vert_orient, "vert")
            if err:
                return self._tool_error(err, code="INVALID_PARAMETER", parameter="vert_orient")

        # Validate property support before mutating
        width_mm = kwargs.get("width_mm")
        height_mm = kwargs.get("height_mm")
        if width_mm is not None or height_mm is not None:
            has_size = visual_helpers.has_uno_property(graphic, "Size") or callable(getattr(graphic, "setSize", None))
            if not has_size:
                return self._tool_error("Graphic does not support setting size.", code="UNSUPPORTED_DOC_TYPE")

        if anchor_type is not None and not visual_helpers.has_uno_property(graphic, "AnchorType"):
            return self._tool_error("Graphic does not support AnchorType.", code="UNSUPPORTED_DOC_TYPE")

        if hori_orient is not None and not visual_helpers.has_uno_property(graphic, "HoriOrient"):
            return self._tool_error("Graphic does not support HoriOrient.", code="UNSUPPORTED_DOC_TYPE")

        if vert_orient is not None and not visual_helpers.has_uno_property(graphic, "VertOrient"):
            return self._tool_error("Graphic does not support VertOrient.", code="UNSUPPORTED_DOC_TYPE")

        has_crop = any(kwargs.get(k) is not None for k in ("crop_top_mm", "crop_bottom_mm", "crop_left_mm", "crop_right_mm"))
        if has_crop and not visual_helpers.has_uno_property(graphic, "GraphicCrop"):
            return self._tool_error("Graphic does not support GraphicCrop.", code="UNSUPPORTED_DOC_TYPE")

        if kwargs.get("title") is not None and not visual_helpers.has_uno_property(graphic, "Title"):
            return self._tool_error("Graphic does not support Title.", code="UNSUPPORTED_DOC_TYPE")

        if kwargs.get("description") is not None and not visual_helpers.has_uno_property(graphic, "Description"):
            return self._tool_error("Graphic does not support Description.", code="UNSUPPORTED_DOC_TYPE")

        new_size = None
        if width_mm is not None or height_mm is not None:
            from com.sun.star.awt import Size

            current = _object_size(graphic)
            curr_w, curr_h = current.Width, current.Height

            new_size = Size()
            new_size.Width = visual_helpers.mm_to_unit(width_mm) if width_mm is not None else curr_w
            new_size.Height = visual_helpers.mm_to_unit(height_mm) if height_mm is not None else curr_h

        from plugin.doc.compound_undo import WriterCompoundUndo

        undo_title = "WriterAgent: Set image properties"
        mgr = None
        try:
            candidate = ctx.doc.getUndoManager()
            if candidate is not None and candidate.isLocked() is not True:
                mgr = candidate
        except Exception:
            mgr = None

        updated = []
        error_msg: str | None = None
        try:
            with WriterCompoundUndo(ctx.doc, undo_title):
                # Size
                if new_size is not None:
                    if visual_helpers.safe_set_property(graphic, "Size", new_size) or visual_helpers.safe_try_method(graphic, "setSize", new_size):
                        updated.append("size")
                    else:
                        return self._tool_error("Failed to set image size.", code="SET_SIZE_FAILED")

                # Title
                title = kwargs.get("title")
                if title is not None:
                    graphic.setPropertyValue("Title", title)
                    updated.append("title")

                # Description (alt-text)
                description = kwargs.get("description")
                if description is not None:
                    graphic.setPropertyValue("Description", description)
                    updated.append("description")

                # Anchor type
                if anchor_type is not None:
                    from plugin.writer.specialized.textframes import text_content_anchor

                    anchor_val = text_content_anchor(anchor_type)
                    if anchor_val is not None:
                        graphic.setPropertyValue("AnchorType", anchor_val)
                        updated.append("anchor_type")

                # Orientation
                if resolved_hori is not None:
                    graphic.setPropertyValue("HoriOrient", resolved_hori)
                    updated.append("hori_orient")

                if resolved_vert is not None:
                    graphic.setPropertyValue("VertOrient", resolved_vert)
                    updated.append("vert_orient")

                # Crop: trim mm off each edge via the GraphicCrop struct. Preserve edges the caller didn't pass.
                if has_crop:
                    from com.sun.star.text import GraphicCrop

                    try:
                        cc = graphic.getPropertyValue("GraphicCrop")
                        cur = (cc.Top, cc.Bottom, cc.Left, cc.Right)
                    except Exception as exc:
                        reraise_if_disposed(exc)
                        cur = (0, 0, 0, 0)
                    top, bottom, left, right = _resolve_crop_edges(kwargs, cur)
                    crop = GraphicCrop()
                    crop.Top = top
                    crop.Bottom = bottom
                    crop.Left = left
                    crop.Right = right
                    graphic.setPropertyValue("GraphicCrop", crop)
                    updated.append("crop")
        except Exception as exc:
            reraise_if_disposed(exc)
            error_msg = str(exc)

        if error_msg is not None:
            undone = False
            if mgr is not None:
                try:
                    titles = mgr.getAllUndoActionTitles()
                    if titles and titles[0] == undo_title:
                        mgr.undo()
                        undone = True
                except Exception:
                    log.exception("image_set_properties rollback failed")
            msg = error_msg
            if not undone:
                msg = "the properties were written and could not be rolled back: " + msg
            return self._tool_error(msg, code="SET_PROPERTIES_FAILED")

        return {"status": "ok", "image_name": image_name, "updated": updated}


# ------------------------------------------------------------------
# ImageCropAndHighlight
# ------------------------------------------------------------------


_BOX_SCHEMA: dict[str, Any] = {"type": "array", "items": {"type": "number"}, "minItems": 4, "maxItems": 4}


class ImageCropAndHighlight(ToolWriterImageBase):
    """Cut an image down to a region and mark passages on it, in the picture's own pixels.

    The why and the how live in :mod:`image_mark`."""

    name: str | None = "image_crop_and_highlight"
    intent: str | None = "media"
    description: str = (
        "Cut an image down to a region and/or mark passages on it (highlighter, red box, underline) "
        "with boxes in the picture's OWN pixels: [x, y, width, height] from its top-left corner, "
        "inside width_px x height_px from image_get_info. If you only see a scaled copy, pass "
        "units='percent' (0-100 of the picture's width/height). crop_box and every highlight box use "
        "the same frame: the picture as it is now. The result is baked into the picture: the cut-away "
        "part is removed from the file and marks cannot drift. Afterwards the picture IS the region "
        "(new width_px/height_px). The frame keeps its width (or width_mm) and its height follows the "
        "new shape. Check the result with get_image."
    )
    parameters: dict[str, Any] | None = {
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "Name of the image (from image_list)."},
            "crop_box": {**_BOX_SCHEMA, "description": "Region to keep, [x, y, width, height]. Omit to keep the picture as shown (an existing crop stays)."},
            "highlights": {
                "type": "array",
                "description": "Marks to draw.",
                "items": {
                    "type": "object",
                    "properties": {
                        "box": {**_BOX_SCHEMA, "description": "Area to mark, [x, y, width, height], same frame as crop_box."},
                        "style": enum_param(
                            list(image_mark.STYLES),
                            "highlight = translucent yellow over the text (default); box = red outline around it; underline = red line under it.",
                        ),
                        "color": {"type": "string", "description": "Optional '#RRGGBB' or color name instead of the style's color."},
                    },
                    "required": ["box"],
                },
            },
            "units": enum_param(["px", "percent"], "Units of every box (default px)."),
            "width_mm": {"type": "number", "description": "Display width in millimetres (default: keep the current width)."},
            "sheet_index": {"type": "integer", "description": "Optional 0-based sheet index on Calc."},
        },
        "required": ["name"],
    }

    is_mutation: bool | None = True

    def execute(self, ctx: typing.Any, **kwargs: typing.Any) -> dict[str, Any]:
        image_name = kwargs["name"]
        assert isinstance(image_name, str)
        sheet_index = kwargs.get("sheet_index")
        obj, err = _resolve_graphic(self, ctx, image_name, sheet_index=sheet_index)
        if err is not None:
            return err
        crop_box = kwargs.get("crop_box")
        highlights = kwargs.get("highlights") or []
        if crop_box is None and not highlights:
            return self._tool_error("Pass crop_box and/or highlights.", code="MISSING_PARAMETER", parameter="crop_box")
        source = visual_helpers.graphic_from_object(obj)
        if source is None:
            return self._tool_error("Image '%s' has no picture data to mark." % image_name, code="IMAGE_NOT_FOUND", image_name=image_name)

        units = kwargs.get("units") or "px"
        width_px: int | None = None
        height_px: int | None = None

        def _invalid_param_error(msg: str) -> dict[str, Any]:
            extra = {}
            if width_px is not None:
                extra["width_px"] = width_px
            if height_px is not None:
                extra["height_px"] = height_px
            return self._tool_error(msg, code="INVALID_PARAMETER", **extra)

        try:
            width_px, height_px = image_mark.pixel_size(source)
            if crop_box is not None:
                region = image_mark.to_pixel_box(crop_box, units, width_px, height_px)
            else:
                region = image_mark.visible_region(visual_helpers.safe_get_property(obj, "GraphicCrop"), visual_helpers.safe_get_property(obj, "ActualSize"), source)
            marks = image_mark.parse_marks(highlights, units, width_px, height_px, region)
        except ValueError as e:
            return _invalid_param_error(str(e))

        from com.sun.star.awt import Size
        from com.sun.star.text import GraphicCrop
        from plugin.doc.compound_undo import WriterCompoundUndo

        size = _object_size(obj)
        width_mm = kwargs.get("width_mm")
        if width_mm is not None:
            if isinstance(width_mm, bool) or not isinstance(width_mm, (int, float)) or int(round(width_mm * 100)) < 1:
                return self._tool_error("width_mm must be greater than 0.", code="INVALID_PARAMETER", parameter="width_mm")
            width_100mm = int(round(width_mm * 100))
        else:
            width_100mm = int(size.Width)
        region_w, region_h = region[2], region[3]
        height_100mm = max(1, int(round(width_100mm * region_h / region_w)))
        # Bake failures propagate. The picture is not written until bake returns.
        # execute_safe logs once and maps the error, including disposal.
        try:
            baked = image_mark.bake(ctx.ctx, source, region, marks, image_mark.stroke_px(region_w, width_100mm / 100.0))
        except ValueError as e:
            return _invalid_param_error(str(e))

        with WriterCompoundUndo(ctx.doc, "WriterAgent: Crop and highlight image"):
            saved_graphic = visual_helpers.safe_get_property(obj, "Graphic")
            saved_crop = visual_helpers.safe_get_property(obj, "GraphicCrop") if visual_helpers.has_uno_property(obj, "GraphicCrop") else None
            obj.setPropertyValue("Graphic", baked)
            # The old crop referred to the old picture; the region is already cut out of the new one.
            if visual_helpers.has_uno_property(obj, "GraphicCrop"):
                if not visual_helpers.safe_set_property(obj, "GraphicCrop", GraphicCrop()):
                    if saved_graphic is not None:
                        obj.setPropertyValue("Graphic", saved_graphic)
                    return self._tool_error("Failed to reset image crop.", code="SET_CROP_FAILED")
            new_size = Size(width_100mm, height_100mm)
            if not (visual_helpers.safe_set_property(obj, "Size", new_size) or visual_helpers.safe_try_method(obj, "setSize", new_size)):
                if saved_graphic is not None:
                    obj.setPropertyValue("Graphic", saved_graphic)
                if saved_crop is not None:
                    visual_helpers.safe_set_property(obj, "GraphicCrop", saved_crop)
                return self._tool_error("Failed to set image size.", code="SET_SIZE_FAILED")
        return {
            "status": "ok",
            "image_name": image_name,
            "width_px": region_w,
            "height_px": region_h,
            "width_mm": width_100mm / 100.0,
            "height_mm": height_100mm / 100.0,
            "highlights": len(marks),
        }


# ------------------------------------------------------------------
# ImageDownload
# ------------------------------------------------------------------


class ImageDownload(ToolWriterImageBase):
    """Download an image from URL to local cache."""

    name: str | None = "image_download"
    intent: str | None = "media"
    description: str = "Download an image from URL to local cache. Returns local path for image_insert/image_replace."
    scripting_only_parameters: frozenset[str] = frozenset({"verify_ssl"})
    parameters: dict[str, Any] | None = {
        "type": "object",
        "properties": {
            "url": {"type": "string", "description": "URL of the image to download."},
            "force": {"type": "boolean", "description": "Force re-download even if cached (default: false)."},
        },
        "required": ["url"],
    }


    def is_async(self) -> bool:
        """HTTP runs on the tool worker. This tool does not touch the document."""
        return True

    def execute(self, ctx: typing.Any, **kwargs: typing.Any) -> dict[str, Any]:
        url = kwargs["url"]

        verify_ssl = kwargs.get("verify_ssl", True)
        # A missing or null flag must not turn verification off.
        if verify_ssl is None:
            verify_ssl = True
        force = kwargs.get("force", False)
        stop_checker = getattr(ctx, "stop_checker", None)

        dl_result = _download_for_tool(self, url, verify_ssl=verify_ssl, force=force, stop_checker=stop_checker)
        if isinstance(dl_result, dict):
            return dl_result
        return {"status": "ok", "local_path": dl_result, "url": url}


# ------------------------------------------------------------------
# ImageInsert
# ------------------------------------------------------------------


class ImageInsert(ToolWriterImageBase):
    """Insert an image from local path or URL into the document."""

    name: str | None = "image_insert"
    intent: str | None = "media"
    description: str = (
        "Insert an image from local path or URL into the document. URLs are auto-downloaded first. "
        "Omitted dimensions preserve aspect ratio with the longer edge sized to 80 mm; "
        "specifying one dimension scales the other proportionally; specifying both sets both. "
        "For Writer letterheads, pass target='header' or 'footer' (optionally style) to insert "
        "into the page header/footer with auto-height so the logo does not overlap the body. "
        "A different-first-page letterhead logo needs first_is_shared=false on the page style "
        "(page_set_style_properties) so HeaderTextFirst is its own text object, then "
        "target='header_first' (or footer_first) — otherwise the image lands in the shared "
        "header and repeats on every page. "
        "On Draw/Impress, optional page (0-based) and x_mm/y_mm place the image; omitted x/y centers it. "
        "On Calc, page is a 0-based sheet index; x_mm and y_mm are ignored. "
        "Sizes and positions are millimetres (not 1/100 mm)."
    )
    parameters: dict[str, Any] | None = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": ("Local file path or URL of the image to insert.")},
            "locator": {"type": "string", "description": ("Unified locator for insertion point (e.g. 'bookmark:NAME', 'heading_text:Title').")},
            "paragraph": {"type": "integer", "description": "Paragraph index for insertion point."},
            "page": {"type": "integer", "description": "Draw/Impress: 0-based page index. Calc: 0-based sheet index. Active page or sheet if omitted."},
            "x_mm": {"type": "number", "description": "Draw/Impress: X position in millimetres (default: centered). Ignored on Calc."},
            "y_mm": {"type": "number", "description": "Draw/Impress: Y position in millimetres (default: centered). Ignored on Calc."},
            "width_mm": {"type": "number", "description": "Width in millimetres (must be positive; omitted derives from aspect ratio)."},
            "height_mm": {"type": "number", "description": "Height in millimetres (must be positive; omitted derives from aspect ratio)."},
            "target": enum_param(
                ["body", "header", "footer", "header_first", "footer_first"],
                (
                    "Writer insertion target (default: body). header/footer write the shared "
                    "page-style region. header_first/footer_first write the first-page letterhead "
                    "(set first_is_shared=false first, or the first page still shares HeaderText)."
                ),
            ),
            "style": {
                "type": "string",
                "description": "Page style for header/footer target (default: Standard).",
            },
            "auto_height": {
                "type": "boolean",
                "description": (
                    "When target is a header/footer region, grow the region with the image "
                    "(default: true) so a taller logo does not overlap the body. "
                    "Set false to keep a fixed header/footer height."
                ),
            },
        },
        "required": ["path"],
    }

    is_mutation: bool | None = True

    def is_async(self) -> bool:
        """URL fetch runs on the tool worker. The insert is marshalled to the main thread."""
        return True

    def execute(self, ctx: typing.Any, **kwargs: typing.Any) -> dict[str, Any]:
        image_path = kwargs["path"]

        width_mm_val = kwargs.get("width_mm")
        height_mm_val = kwargs.get("height_mm")
        dim_err = _validate_dimensions(self, kwargs)
        if dim_err is not None:
            return dim_err

        locator = kwargs.get("locator")
        paragraph_index = kwargs.get("paragraph")
        target = kwargs.get("target", "body")
        style_name = kwargs.get("style", "Standard")

        stop_checker = getattr(ctx, "stop_checker", None)
        if image_path.startswith("http://") or image_path.startswith("https://"):
            dl_result = _download_for_tool(self, image_path, stop_checker=stop_checker)
            if isinstance(dl_result, dict):
                return dl_result
            image_path = dl_result
        if not os.path.isfile(image_path):
            return self._tool_error(f"File not found: {image_path}", code="FILE_NOT_FOUND", path=image_path)

        def _place() -> dict[str, Any]:
            nonlocal paragraph_index
            doc = ctx.doc
            doc_type = visual_helpers.get_visual_doc_type(doc)

            if width_mm_val is not None and height_mm_val is not None:
                width_mm = float(width_mm_val)
                height_mm = float(height_mm_val)
            else:
                px_size = image_tools.graphic_pixel_size_from_path(ctx.ctx, image_path)
                px_w, px_h = px_size if px_size is not None else (None, None)
                if px_w is not None and px_h is not None and px_w > 0 and px_h > 0:
                    if width_mm_val is None and height_mm_val is None:
                        if px_w >= px_h:
                            width_mm = 80.0
                            height_mm = round(80.0 * px_h / px_w, 2)
                        else:
                            height_mm = 80.0
                            width_mm = round(80.0 * px_w / px_h, 2)
                    elif width_mm_val is not None:
                        width_mm = float(width_mm_val)
                        height_mm = round(width_mm * px_h / px_w, 2)
                    else:
                        assert height_mm_val is not None
                        height_mm = float(height_mm_val)
                        width_mm = round(height_mm * px_w / px_h, 2)
                else:
                    if width_mm_val is None and height_mm_val is None:
                        width_mm, height_mm = 80.0, 80.0
                    elif width_mm_val is not None:
                        width_mm = float(width_mm_val)
                        height_mm = float(width_mm_val)
                    else:
                        assert height_mm_val is not None
                        height_mm = float(height_mm_val)
                        width_mm = float(height_mm_val)

            if target in ("header", "footer", "header_first", "footer_first"):
                if doc_type not in ("writer", "web"):
                    return self._tool_error(
                        "target='%s' is only supported for Writer documents." % target,
                        code="UNSUPPORTED_DOC_TYPE",
                    )
                # Insert failures propagate. execute_safe reports them, including disposal.
                # A None graphic is a failed insert that did not raise.
                placed = insert_image_into_header_footer(
                    doc,
                    image_path,
                    target,
                    width_mm=width_mm,
                    height_mm=height_mm,
                    style_name=style_name,
                    auto_height=kwargs.get("auto_height", True),
                    ctx=ctx.ctx,
                )
                graphic = placed["graphic"]
                if graphic is None:
                    return self._tool_error("Failed to insert image.", code="INSERT_FAILED", path=image_path)
                get_name = getattr(graphic, "getName", None)
                image_name = get_name() if callable(get_name) else ""
                return {
                    "status": "ok",
                    "image_name": image_name,
                    "width_mm": width_mm,
                    "height_mm": height_mm,
                    "target": target,
                    "style_name": placed["style_name"],
                    "auto_height": placed["auto_height"],
                }

            if (locator is not None or paragraph_index is not None) and doc_type not in ("writer", "web"):
                return self._tool_error(
                    "locator and paragraph parameters are only supported for Writer documents.",
                    code="UNSUPPORTED_DOC_TYPE",
                )

            text_cursor = None
            doc_svc = getattr(ctx.services, "document", None)
            if locator is not None and paragraph_index is None and doc_svc:
                resolved = doc_svc.resolve_locator(doc, locator)
                paragraph_index = resolved.get("para_index")

            if paragraph_index is not None and doc_svc:
                target_para = doc_svc.find_paragraph_element(doc, paragraph_index)
                if target_para is None:
                    return self._tool_error(f"Paragraph {paragraph_index} not found.", code="PARAGRAPH_NOT_FOUND", paragraph_index=paragraph_index)
                text_cursor = doc.getText().createTextCursorByRange(target_para.getEnd())

            try:
                graphic = insert_image_at_locator(
                    ctx.ctx,
                    doc,
                    image_path,
                    width_mm=width_mm,
                    height_mm=height_mm,
                    text_cursor=text_cursor,
                    page_index=kwargs.get("page"),
                    x_mm=kwargs.get("x_mm"),
                    y_mm=kwargs.get("y_mm"),
                )
            except IndexError as exc:
                return self._tool_error(str(exc), code="INVALID_PARAMETER", parameter="page")
            if graphic is None:
                return self._tool_error("Failed to insert image.", code="INSERT_FAILED", path=image_path)

            image_name = graphic.getName() if hasattr(graphic, "getName") else ""
            return {"status": "ok", "image_name": image_name, "width_mm": width_mm, "height_mm": height_mm}

        return _run_on_main(_place, timeout=_DOCUMENT_MARSHAL_TIMEOUT_SEC, bound_scope=None)


# ------------------------------------------------------------------
# ImageDelete
# ------------------------------------------------------------------


class ImageDelete(ToolWriterImageBase):
    """Delete an image from the document."""

    name: str | None = "image_delete"
    intent: str | None = "media"
    description: str = (
        "Delete an image from the document. Removes the graphic object; "
        "any frame created by image_insert_frame is a separate object."
    )
    parameters: dict[str, Any] | None = {
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "Name of the image to delete (from image_list)."},
            "sheet_index": {"type": "integer", "description": "Optional 0-based sheet index on Calc."},
        },
        "required": ["name"],
    }

    is_mutation: bool | None = True

    def execute(self, ctx: typing.Any, **kwargs: typing.Any) -> dict[str, Any]:
        image_name = kwargs["name"]
        sheet_index = kwargs.get("sheet_index")

        graphic, err = _resolve_graphic(self, ctx, image_name, sheet_index=sheet_index)
        if err is not None:
            return err

        doc_type = visual_helpers.get_visual_doc_type(ctx.doc)
        if doc_type in ("calc", "draw", "impress"):
            try:
                if not visual_helpers.remove_graphic_from_draw_pages(ctx.doc, graphic):
                    return self._tool_error("Image '%s' not found or document does not support graphic objects." % image_name, code="IMAGE_NOT_FOUND", image_name=image_name)
            except ValueError as exc:
                return self._tool_error(str(exc), code="AMBIGUOUS_NAME", image_name=image_name)
        else:
            anchor = graphic.getAnchor()
            text = anchor.getText()
            text.removeTextContent(graphic)

        return {"status": "ok", "deleted": image_name}


# ------------------------------------------------------------------
# ImageReplace
# ------------------------------------------------------------------


class ImageReplace(ToolWriterImageBase):
    """Replace an image's source file keeping position and frame."""

    name: str | None = "image_replace"
    intent: str | None = "media"
    description: str = "Replace an image's source file, keeping the same object, position, and frame. The new file is embedded. If only width_mm or only height_mm is set, the other side scales to keep the current aspect ratio. Setting both uses both sizes."
    parameters: dict[str, Any] | None = {
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "Name of the image to replace (from image_list)."},
            "path": {"type": "string", "description": "Local file path or URL of the replacement image."},
            "width_mm": {"type": "number", "description": "New width in millimetres. If height_mm is omitted, height scales to keep the current aspect ratio."},
            "height_mm": {"type": "number", "description": "New height in millimetres. If width_mm is omitted, width scales to keep the current aspect ratio."},
            "sheet_index": {"type": "integer", "description": "Optional 0-based sheet index on Calc."},
        },
        "required": ["name", "path"],
    }

    is_mutation: bool | None = True

    def is_async(self) -> bool:
        """URL fetch runs on the tool worker. Document reads and the replace run on the main thread."""
        return True

    def execute(self, ctx: typing.Any, **kwargs: typing.Any) -> dict[str, Any]:
        image_name = kwargs["name"]
        new_image_path = kwargs["path"]
        sheet_index = kwargs.get("sheet_index")

        dim_err = _validate_dimensions(self, kwargs)
        if dim_err is not None:
            return dim_err

        is_url = new_image_path.startswith("http://") or new_image_path.startswith("https://")

        if is_url:
            # Existence is UNO. Do not hold the graphic proxy across the download.
            try:
                exists = _run_on_main(lambda: _get_graphic_object(ctx, ctx.doc, image_name, sheet_index=sheet_index) is not None)
            except ValueError as exc:
                sheets = getattr(exc, "sheet_indices", None)
                return self._tool_error(str(exc), code="AMBIGUOUS_NAME", image_name=image_name, sheet_indices=sheets, sheets=sheets)
            if not exists:
                return self._tool_error(
                    "Image '%s' not found or document does not support graphic objects." % image_name,
                    code="IMAGE_NOT_FOUND",
                    image_name=image_name,
                )
            stop_checker = getattr(ctx, "stop_checker", None)
            dl_result = _download_for_tool(self, new_image_path, stop_checker=stop_checker)
            if isinstance(dl_result, dict):
                return dl_result
            new_image_path = dl_result
            if not os.path.isfile(new_image_path):
                return self._tool_error(f"File not found: {new_image_path}", code="FILE_NOT_FOUND", path=new_image_path)

        def _apply() -> dict[str, Any]:
            graphic, err = _resolve_graphic(self, ctx, image_name, sheet_index=sheet_index)
            if err is not None:
                return err
            if not os.path.isfile(new_image_path):
                return self._tool_error(f"File not found: {new_image_path}", code="FILE_NOT_FOUND", path=new_image_path)

            width_mm = kwargs.get("width_mm")
            height_mm = kwargs.get("height_mm")
            width_units = height_units = None
            if width_mm is not None or height_mm is not None:
                # Same as image_set_properties: a size-read failure must not become
                # SIZE_UNAVAILABLE. execute_safe reports it before the replace.
                current = _object_size(graphic)
                current_width, current_height = current.Width, current.Height
                width_units, height_units = _replacement_size_units(current_width, current_height, width_mm, height_mm)

            if not replace_graphic_source(ctx.ctx, ctx.doc, graphic, new_image_path, width_units=width_units, height_units=height_units):
                return self._tool_error("Failed to replace image.", code="REPLACE_FAILED", image_name=image_name)
            return {"status": "ok", "image_name": image_name}

        return _run_on_main(_apply, timeout=_DOCUMENT_MARSHAL_TIMEOUT_SEC, bound_scope=None)


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------


IMAGE_CACHE_MAX_AGE_SECONDS = 24 * 3600
IMAGE_CACHE_MAX_BYTES = 256 * 1024 * 1024


def _unlink_stale_partials(cache_dir: str) -> None:
    now = time.time()
    cutoff_age = max(600, get_config_int("request_timeout") + 30)
    try:
        for entry in os.scandir(cache_dir):
            if entry.is_file() and entry.name.endswith(".partial"):
                try:
                    st = entry.stat()
                    if now - st.st_mtime > cutoff_age:
                        os.unlink(entry.path)
                except OSError:
                    pass
    except OSError:
        pass


def _prune_image_cache(cache_dir: str, keep_path: str) -> None:
    _unlink_stale_partials(cache_dir)
    now = time.time()
    keep_real = os.path.realpath(keep_path)
    entries: list[tuple[float, int, str]] = []
    try:
        for entry in os.scandir(cache_dir):
            if not entry.is_file() or entry.name.endswith(".partial"):
                continue
            real = os.path.realpath(entry.path)
            if real == keep_real:
                continue
            try:
                st = entry.stat()
                if now - st.st_mtime > IMAGE_CACHE_MAX_AGE_SECONDS:
                    os.unlink(entry.path)
                else:
                    entries.append((st.st_mtime, st.st_size, entry.path))
            except OSError:
                pass
    except OSError:
        return

    total_size = 0
    try:
        total_size += os.path.getsize(keep_path)
    except OSError:
        pass
    for _, size, _ in entries:
        total_size += size

    if total_size > IMAGE_CACHE_MAX_BYTES:
        entries.sort(key=lambda x: x[0])
        for _, size, path in entries:
            if total_size <= IMAGE_CACHE_MAX_BYTES:
                break
            try:
                os.unlink(path)
                total_size -= size
            except OSError:
                pass


def _download_image_to_cache(
    url: str,
    verify_ssl: bool = True,
    force: bool = False,
    stop_checker: Any = None,
) -> str:
    """Download an image URL to the local cache directory.

    Returns the local file path. Uses a URL-based hash for caching.

    TLS certificates are verified unless the caller passes ``verify_ssl=False``.
    Image replace and image insert use this default, so an https URL is not
    fetched with hostname checks disabled.
    """
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme.lower() not in ("http", "https"):
        raise ValueError("Unsupported URL scheme '%s': only http and https are allowed" % parsed.scheme)

    cache_dir = image_tools._image_cache_dir()
    os.makedirs(cache_dir, exist_ok=True)
    _unlink_stale_partials(cache_dir)

    # Derive a stable filename from the URL
    url_hash = hashlib.sha256(url.encode("utf-8")).hexdigest()[:16]

    if not force:
        now = time.time()
        try:
            for entry in os.scandir(cache_dir):
                if entry.is_file() and not entry.name.endswith(".partial"):
                    name_base = entry.name.split(".")[0]
                    if name_base == url_hash:
                        try:
                            if now - entry.stat().st_mtime <= IMAGE_CACHE_MAX_AGE_SECONDS:
                                log.debug("image_download: cache hit %s -> %s", url, entry.path)
                                return entry.path
                        except OSError:
                            pass
        except OSError:
            pass

    log.info("image_download: downloading %s (hash %s)", url, url_hash)

    # ImageReplace, ImageInsert, and ImageDownload verify https
    # certificates. verify_ssl=False downloads with CERT_NONE.
    # Verification stays on unless the caller opts out.
    if verify_ssl:
        context = None
    else:
        context = ssl.create_default_context()
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE

    request = urllib.request.Request(url)
    request.add_header("User-Agent", USER_AGENT)

    # Runs off the UI thread but stays bounded: socket by request_timeout, body by the
    # byte cap, and local_path is replaced only after the full body is on a temp file
    # (a truncated file would look like a cache hit).
    timeout = get_config_int("request_timeout")
    fd, partial_path = tempfile.mkstemp(dir=cache_dir, prefix="img-", suffix=".partial")
    try:
        with os.fdopen(fd, "wb") as fh:
            fd = -1
            with urllib.request.urlopen(request, context=context, timeout=timeout) as response:
                content_type = (response.headers.get("Content-Type") or "").split(";")[0].strip().lower()
                if content_type in IMAGE_CONTENT_TYPE_DENYLIST:
                    raise ValueError("Invalid Content-Type '%s' for image download" % content_type)

                declared = response.headers.get("Content-Length")
                if declared is not None:
                    try:
                        if int(declared) > IMAGE_DOWNLOAD_MAX_BYTES:
                            raise RuntimeError("image download exceeded %s bytes" % IMAGE_DOWNLOAD_MAX_BYTES)
                    except ValueError:
                        pass

                # Socket timeout is ~1s for responsive Stop during read chunks.
                # Connect/DNS and silent stall before the first byte can take full request_timeout.
                sock = getattr(response, "sock", None)
                if sock is None:
                    fp = getattr(response, "fp", None)
                    if fp is not None:
                        raw_io = getattr(fp, "raw", None)
                        sock = getattr(raw_io, "_sock", None) if raw_io is not None else getattr(fp, "_sock", None)
                if sock is not None and hasattr(sock, "settimeout"):
                    best_effort(lambda: sock.settimeout(1.0), "image download: could not set socket timeout")

                total = 0
                initial_bytes = b""
                deadline = time.monotonic() + timeout
                while True:
                    if stop_checker is not None and stop_checker():
                        response.close()
                        raise _DownloadStopped()
                    try:
                        chunk = response.read(_IMAGE_DOWNLOAD_CHUNK)
                    except (TimeoutError, socket.timeout):
                        if stop_checker is not None and stop_checker():
                            response.close()
                            raise _DownloadStopped()
                        if time.monotonic() > deadline:
                            raise TimeoutError("image download timed out")
                        continue

                    if not chunk:
                        break
                    if len(initial_bytes) < 1024:
                        initial_bytes += chunk[: 1024 - len(initial_bytes)]
                    total += len(chunk)
                    if total > IMAGE_DOWNLOAD_MAX_BYTES:
                        raise RuntimeError("image download exceeded %s bytes" % IMAGE_DOWNLOAD_MAX_BYTES)
                    fh.write(chunk)
            if total == 0:
                raise RuntimeError("empty image download")
            sniffed_ext = validated_image_suffix(initial_bytes)

        local_path = os.path.join(cache_dir, url_hash + sniffed_ext)
        os.replace(partial_path, local_path)
        partial_path = ""

        try:
            for entry in os.scandir(cache_dir):
                if entry.is_file() and entry.name.split(".")[0] == url_hash and os.path.realpath(entry.path) != os.path.realpath(local_path):
                    try:
                        os.unlink(entry.path)
                    except OSError:
                        pass
        except OSError:
            pass

        _prune_image_cache(cache_dir, local_path)
    finally:
        if fd >= 0:
            os.close(fd)
        if partial_path != "":
            try:
                os.unlink(partial_path)
            except OSError:
                pass

    return local_path
