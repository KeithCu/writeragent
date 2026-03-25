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

Alternative (not implemented): consolidate the document image tools
(ListImages, GetImageInfo, SetImageProperties, DownloadImage, InsertImage,
DeleteImage, ReplaceImage) into a single manage_image tool with
action: list | info | set_properties | download | insert | delete | replace
and action-specific parameters. Would reduce 7 tools to 1 but yield a
larger single schema.
"""

import logging
import hashlib
import os
import tempfile

from plugin.modules.writer.base import ToolWriterShapeBase as ToolBase, ToolBaseDummy
from plugin.framework.image_tools import (
    insert_image, replace_image_in_place, get_selected_image_base64,
    get_selected_image_dimensions_px,
)

log = logging.getLogger("writeragent.writer")


class GenerateImage(ToolBase):
    """Generate a new image from a prompt, or edit an existing image (Img2Img)."""

    name = "generate_image"
    intent = "media"
    description = (
        "Generate an image from a text prompt and insert it. "
        "To edit an existing image, pass source_image='selection' and select an image first."
    )
    parameters = {
        "type": "object",
        "properties": {
            "prompt": {
                "type": "string",
                "description": "Descriptive prompt for image generation or editing"
            },
            "source_image": {
                "type": "string",
                "description": (
                    "Optional. Use 'selection' to edit the currently selected image (Img2Img). "
                    "Omit to generate a new image."
                )
            },
            "strength": {
                "type": "number",
                "description": "For editing: how much to change the image (0.0-1.0). Ignored when generating new.",
                "default": 0.75
            },
            "aspect_ratio": {
                "type": "string",
                "enum": ["square", "landscape_16_9", "portrait_9_16", "landscape_3_2", "portrait_2_3", "1:1", "4:3", "3:4", "16:9", "9:16"],
                "default": "square"
            },
            "base_size": {
                "type": "integer",
                "description": "Base dimension for scaling",
                "default": 512
            },
            "width": {"type": "integer", "description": "Override calculated width"},
            "height": {"type": "integer", "description": "Override calculated height"},
            "provider": {"type": "string", "description": "Override default provider"}
        },
        "required": ["prompt"]
    }
    uno_services = ["com.sun.star.text.TextDocument", "com.sun.star.sheet.SpreadsheetDocument", "com.sun.star.drawing.DrawingDocument", "com.sun.star.presentation.PresentationDocument"]
    is_mutation = True
    long_running = True

    def execute(self, ctx, prompt, **args):
        from plugin.framework.config import get_config_dict, as_bool, get_text_model, update_lru_history

        status_callback = getattr(ctx, "status_callback", None)
        config = get_config_dict(ctx.ctx)

        provider = args.get("provider", config.get("image_provider", "aihorde"))
        add_to_gallery = as_bool(config.get("image_auto_gallery", True))
        add_frame = as_bool(config.get("image_insert_frame", False))

        source_image = args.get("source_image")
        if isinstance(source_image, str):
            source_image = source_image.strip() or None

        is_edit = source_image and source_image.lower() == "selection"
        source_b64 = None
        edit_width, edit_height = 512, 512

        if is_edit:
            source_b64 = get_selected_image_base64(ctx.doc, ctx.ctx)
            if not source_b64:
                return self._tool_error(
                    "No image selected. Please select an image in the document first.",
                    code="NO_SELECTION",
                    action="edit_image"
                )
            edit_width, edit_height = get_selected_image_dimensions_px(ctx.doc)
            if edit_width is None:
                edit_width, edit_height = 512, 512

        base_size = args.get("base_size", config.get("image_base_size", 512))
        try:
            base_size = int(base_size)
        except (ValueError, TypeError):
            base_size = 512

        aspect = args.get("aspect_ratio", config.get("image_default_aspect", "square"))
        if aspect in ("landscape_16_9", "16:9"):
            w, h = int(base_size * 16 / 9), base_size
        elif aspect in ("portrait_9_16", "9:16"):
            w, h = base_size, int(base_size * 16 / 9)
        elif aspect in ("landscape_3_2", "4:3"):
            w, h = int(base_size * 1.5), base_size
        elif aspect in ("portrait_2_3", "3:4"):
            w, h = base_size, int(base_size * 1.5)
        else:
            w, h = base_size, base_size

        w = (w // 64) * 64
        h = (h // 64) * 64

        width = args.get("width", edit_width if is_edit else w)
        height = args.get("height", edit_height if is_edit else h)

        from plugin.framework.image_utils import ImageService
        image_svc = ImageService(ctx.ctx, config)
        args_copy = {
            k: v
            for k, v in args.items()
            if k not in ("prompt", "base_size", "aspect_ratio", "width", "height", "provider", "source_image")
        }
        if is_edit:
            args_copy["source_image"] = source_b64
            args_copy["strength"] = args.get("strength", 0.75)

        paths, error_msg = image_svc.generate_image(
            prompt,
            provider_name=provider,
            width=width,
            height=height,
            status_callback=status_callback,
            **args_copy,
        )

        if not paths:
            return self._tool_error(
                error_msg or "No image returned.",
                code="PROVIDER_ERROR",
                provider=provider
            )

        if is_edit:
            replaced = replace_image_in_place(
                ctx.ctx, ctx.doc, paths[0], width, height, title=prompt,
                description="Edited by %s" % provider,
                add_to_gallery=add_to_gallery, add_frame=add_frame
            )
            if not replaced:
                insert_image(ctx.ctx, ctx.doc, paths[0], width, height, title=prompt,
                             description="Edited by %s" % provider,
                             add_to_gallery=add_to_gallery, add_frame=add_frame)
            msg = "Image edited and inserted from %s." % provider
        else:
            insert_image(ctx.ctx, ctx.doc, paths[0], width, height, title=prompt,
                         description="Generated by %s" % provider,
                         add_to_gallery=add_to_gallery, add_frame=add_frame)
            msg = "Image generated and inserted from %s." % provider

        if provider in ("endpoint", "openrouter"):
            image_model_used = args.get("image_model") or config.get("image_model") or get_text_model(ctx.ctx)
            if image_model_used:
                endpoint = str(config.get("endpoint", "")).strip()
                update_lru_history(ctx.ctx, image_model_used.strip(), "image_model_lru", endpoint)

        return {"status": "ok", "message": msg}

# Persistent cache directory for downloaded images.
_IMAGE_CACHE_DIR = os.path.join(tempfile.gettempdir(), "writeragent_images")


# ------------------------------------------------------------------
# ListImages
# ------------------------------------------------------------------

class ListImages(ToolBaseDummy):
    """List all images/graphic objects in the document."""

    name = "list_images"
    intent = "media"
    description = (
        "List all images/graphic objects in the document with name, "
        "dimensions, title, and description."
    )
    parameters = {
        "type": "object",
        "properties": {},
        "required": [],
    }
    uno_services = ["com.sun.star.text.TextDocument"]

    def execute(self, ctx, **kwargs):
        doc = ctx.doc
        graphics = self.get_collection(doc, "getGraphicObjects", "Document does not support graphic objects.")
        if isinstance(graphics, dict):
            return graphics

        doc_svc = ctx.services.document
        para_ranges = doc_svc.get_paragraph_ranges(doc)
        text_obj = doc.getText()

        images = []
        for name in graphics.getElementNames():
            try:
                graphic = graphics.getByName(name)
                size = graphic.getPropertyValue("Size")
                title = ""
                description = ""
                try:
                    title = graphic.getPropertyValue("Title")
                except Exception:
                    pass
                try:
                    description = graphic.getPropertyValue("Description")
                except Exception:
                    pass

                # Paragraph index via anchor
                paragraph_index = -1
                try:
                    anchor = graphic.getAnchor()
                    paragraph_index = doc_svc.find_paragraph_for_range(
                        anchor, para_ranges, text_obj
                    )
                except Exception:
                    pass

                # Page number via view cursor
                page = None
                try:
                    anchor = graphic.getAnchor()
                    vc = doc.getCurrentController().getViewCursor()
                    vc.gotoRange(anchor.getStart(), False)
                    page = vc.getPage()
                except Exception:
                    pass

                entry = {
                    "name": name,
                    "width_mm": size.Width / 100.0,
                    "height_mm": size.Height / 100.0,
                    "width_100mm": size.Width,
                    "height_100mm": size.Height,
                    "title": title,
                    "description": description,
                    "paragraph_index": paragraph_index,
                }
                if page is not None:
                    entry["page"] = page
                images.append(entry)
            except Exception as e:
                log.debug("list_images: skip '%s': %s", name, e)

        return {"status": "ok", "images": images, "count": len(images)}


# ------------------------------------------------------------------
# GetImageInfo
# ------------------------------------------------------------------

class GetImageInfo(ToolBaseDummy):
    """Get detailed info about a specific image."""

    name = "get_image_info"
    intent = "media"
    description = (
        "Get detailed info about a specific image: URL, dimensions, "
        "anchor type, orientation, and paragraph index."
    )
    parameters = {
        "type": "object",
        "properties": {
            "image_name": {
                "type": "string",
                "description": "Name of the image (from list_images).",
            },
        },
        "required": ["image_name"],
    }
    uno_services = ["com.sun.star.text.TextDocument"]

    def execute(self, ctx, **kwargs):
        image_name = kwargs.get("image_name", "")

        graphic = self.get_item(
            ctx.doc, "getGraphicObjects", image_name,
            missing_msg="Document does not support graphic objects.",
            not_found_msg="Image '%s' not found." % image_name
        )
        if isinstance(graphic, dict):
            return graphic

        size = graphic.getPropertyValue("Size")

        # Graphic URL — try the modern property first, then legacy.
        graphic_url = ""
        try:
            graphic_url = graphic.getPropertyValue("GraphicURL")
        except Exception:
            pass
        if not graphic_url:
            try:
                graphic_url = str(graphic.getPropertyValue("GraphicObjectFillBitmap"))
            except Exception:
                pass

        # Anchor type
        anchor_type = None
        try:
            anchor_type = int(graphic.getPropertyValue("AnchorType").value)
        except Exception:
            try:
                anchor_type = int(graphic.getPropertyValue("AnchorType"))
            except Exception:
                pass

        # Orientation
        hori_orient = None
        vert_orient = None
        try:
            hori_orient = int(graphic.getPropertyValue("HoriOrient"))
        except Exception:
            pass
        try:
            vert_orient = int(graphic.getPropertyValue("VertOrient"))
        except Exception:
            pass

        # Title / description
        title = ""
        description = ""
        try:
            title = graphic.getPropertyValue("Title")
        except Exception:
            pass
        try:
            description = graphic.getPropertyValue("Description")
        except Exception:
            pass

        # Paragraph index via anchor
        paragraph_index = -1
        try:
            anchor = graphic.getAnchor()
            doc_svc = ctx.services.document
            para_ranges = doc_svc.get_paragraph_ranges(ctx.doc)
            text_obj = ctx.doc.getText()
            paragraph_index = doc_svc.find_paragraph_for_range(
                anchor, para_ranges, text_obj
            )
        except Exception:
            pass

        return {
            "status": "ok",
            "image_name": image_name,
            "graphic_url": graphic_url,
            "width_mm": size.Width / 100.0,
            "height_mm": size.Height / 100.0,
            "width_100mm": size.Width,
            "height_100mm": size.Height,
            "anchor_type": anchor_type,
            "hori_orient": hori_orient,
            "vert_orient": vert_orient,
            "title": title,
            "description": description,
            "paragraph_index": paragraph_index,
        }


# ------------------------------------------------------------------
# SetImageProperties
# ------------------------------------------------------------------

class SetImageProperties(ToolBaseDummy):
    """Resize, reposition, crop, or update caption/alt-text for an image."""

    name = "set_image_properties"
    intent = "media"
    description = (
        "Resize, reposition, crop, or update caption/alt-text for an image."
    )
    parameters = {
        "type": "object",
        "properties": {
            "image_name": {
                "type": "string",
                "description": "Name of the image (from list_images).",
            },
            "width_mm": {
                "type": "number",
                "description": "New width in millimetres.",
            },
            "height_mm": {
                "type": "number",
                "description": "New height in millimetres.",
            },
            "title": {
                "type": "string",
                "description": "Image title (tooltip text).",
            },
            "description": {
                "type": "string",
                "description": "Image alternative text (alt-text).",
            },
            "anchor_type": {
                "type": "integer",
                "description": (
                    "Anchor type: 0=AT_PARAGRAPH, 1=AS_CHARACTER, "
                    "2=AT_PAGE, 3=AT_FRAME, 4=AT_CHARACTER."
                ),
            },
            "hori_orient": {
                "type": "integer",
                "description": "Horizontal orientation constant.",
            },
            "vert_orient": {
                "type": "integer",
                "description": "Vertical orientation constant.",
            },
        },
        "required": ["image_name"],
    }
    uno_services = ["com.sun.star.text.TextDocument"]
    is_mutation = True

    def execute(self, ctx, **kwargs):
        image_name = kwargs.get("image_name", "")
        if not image_name:
            return self._tool_error("image_name is required.", code="MISSING_PARAMETER", parameter="image_name")

        graphic = self.get_item(
            ctx.doc, "getGraphicObjects", image_name,
            missing_msg="Document does not support graphic objects.",
            not_found_msg="Image '%s' not found." % image_name
        )
        if isinstance(graphic, dict):
            return graphic

        updated = []

        # Size
        width_mm = kwargs.get("width_mm")
        height_mm = kwargs.get("height_mm")
        if width_mm is not None or height_mm is not None:
            from com.sun.star.awt import Size
            current = graphic.getPropertyValue("Size")
            new_size = Size()
            new_size.Width = int(width_mm * 100) if width_mm is not None else current.Width
            new_size.Height = int(height_mm * 100) if height_mm is not None else current.Height
            graphic.setPropertyValue("Size", new_size)
            updated.append("size")

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
        anchor_type = kwargs.get("anchor_type")
        if anchor_type is not None:
            from com.sun.star.text.TextContentAnchorType import (
                AT_PARAGRAPH, AS_CHARACTER, AT_PAGE, AT_FRAME, AT_CHARACTER,
            )
            anchor_map = {
                0: AT_PARAGRAPH,
                1: AS_CHARACTER,
                2: AT_PAGE,
                3: AT_FRAME,
                4: AT_CHARACTER,
            }
            if anchor_type in anchor_map:
                graphic.setPropertyValue("AnchorType", anchor_map[anchor_type])
                updated.append("anchor_type")

        # Orientation
        hori_orient = kwargs.get("hori_orient")
        if hori_orient is not None:
            graphic.setPropertyValue("HoriOrient", hori_orient)
            updated.append("hori_orient")

        vert_orient = kwargs.get("vert_orient")
        if vert_orient is not None:
            graphic.setPropertyValue("VertOrient", vert_orient)
            updated.append("vert_orient")

        return {
            "status": "ok",
            "image_name": image_name,
            "updated": updated,
        }


# ------------------------------------------------------------------
# DownloadImage
# ------------------------------------------------------------------

class DownloadImage(ToolBaseDummy):
    """Download an image from URL to local cache."""

    name = "download_image"
    intent = "media"
    description = (
        "Download an image from URL to local cache. Returns local path "
        "for insert_image/replace_image."
    )
    parameters = {
        "type": "object",
        "properties": {
            "url": {
                "type": "string",
                "description": "URL of the image to download.",
            },
            "verify_ssl": {
                "type": "boolean",
                "description": "Verify SSL certificates (default: false).",
            },
            "force": {
                "type": "boolean",
                "description": "Force re-download even if cached (default: false).",
            },
        },
        "required": ["url"],
    }
    uno_services = ["com.sun.star.text.TextDocument"]

    def execute(self, ctx, **kwargs):
        url = kwargs.get("url", "")

        verify_ssl = kwargs.get("verify_ssl", False)
        force = kwargs.get("force", False)

        local_path = _download_image_to_cache(url, verify_ssl=verify_ssl, force=force)
        return {
            "status": "ok",
            "local_path": local_path,
            "url": url,
        }


# ------------------------------------------------------------------
# InsertImage
# ------------------------------------------------------------------

class InsertImage(ToolBaseDummy):
    """Insert an image from local path or URL into the document."""

    name = "insert_image"
    intent = "media"
    description = (
        "Insert an image from local path or URL into the document. "
        "URLs are auto-downloaded first."
    )
    parameters = {
        "type": "object",
        "properties": {
            "image_path": {
                "type": "string",
                "description": (
                    "Local file path or URL of the image to insert."
                ),
            },
            "locator": {
                "type": "string",
                "description": (
                    "Unified locator for insertion point "
                    "(e.g. 'bookmark:NAME', 'heading_text:Title')."
                ),
            },
            "paragraph_index": {
                "type": "integer",
                "description": "Paragraph index for insertion point.",
            },
            "width_mm": {
                "type": "integer",
                "description": "Width in millimetres (default: 80).",
            },
            "height_mm": {
                "type": "integer",
                "description": "Height in millimetres (default: 80).",
            },
        },
        "required": ["image_path"],
    }
    uno_services = ["com.sun.star.text.TextDocument"]
    is_mutation = True

    def execute(self, ctx, **kwargs):
        import uno

        image_path = kwargs.get("image_path", "")

        width_mm = kwargs.get("width_mm", 80)
        height_mm = kwargs.get("height_mm", 80)
        locator = kwargs.get("locator")
        paragraph_index = kwargs.get("paragraph_index")

        doc = ctx.doc

        # Auto-download URLs
        if image_path.startswith("http://") or image_path.startswith("https://"):
            image_path = _download_image_to_cache(image_path)
        if not os.path.isfile(image_path):
            return self._tool_error(
                f"File not found: {image_path}",
                code="FILE_NOT_FOUND",
                path=image_path
            )

        # Convert to file:// URL
        file_url = uno.systemPathToFileUrl(os.path.abspath(image_path))

        # Create graphic object
        graphic = doc.createInstance("com.sun.star.text.TextGraphicObject")
        graphic.setPropertyValue("GraphicURL", file_url)

        # Set size
        from com.sun.star.awt import Size
        size = Size()
        size.Width = int(width_mm) * 100
        size.Height = int(height_mm) * 100
        graphic.setPropertyValue("Size", size)

        # Resolve insertion point
        doc_text = doc.getText()
        doc_svc = ctx.services.document

        if locator is not None and paragraph_index is None:
            resolved = doc_svc.resolve_locator(doc, locator)
            paragraph_index = resolved.get("para_index")

        if paragraph_index is not None:
            target, _ = doc_svc.find_paragraph_element(doc, paragraph_index)
            if target is None:
                return self._tool_error(
                    f"Paragraph {paragraph_index} not found.",
                    code="PARAGRAPH_NOT_FOUND",
                    paragraph_index=paragraph_index
                )
            cursor = doc_text.createTextCursorByRange(target.getEnd())
        else:
            # Insert at current cursor position (end of document)
            cursor = doc_text.createTextCursor()
            cursor.gotoEnd(False)

        doc_text.insertTextContent(cursor, graphic, False)

        return {
            "status": "ok",
            "image_name": graphic.getName(),
            "width_mm": width_mm,
            "height_mm": height_mm,
        }


# ------------------------------------------------------------------
# DeleteImage
# ------------------------------------------------------------------

class DeleteImage(ToolBaseDummy):
    """Delete an image from the document."""

    name = "delete_image"
    intent = "media"
    description = "Delete an image from the document."
    parameters = {
        "type": "object",
        "properties": {
            "image_name": {
                "type": "string",
                "description": "Name of the image to delete (from list_images).",
            },
            "remove_frame": {
                "type": "boolean",
                "description": "Also remove the containing frame (default: true).",
            },
        },
        "required": ["image_name"],
    }
    uno_services = ["com.sun.star.text.TextDocument"]
    is_mutation = True

    def execute(self, ctx, **kwargs):
        image_name = kwargs.get("image_name", "")

        graphic = self.get_item(
            ctx.doc, "getGraphicObjects", image_name,
            missing_msg="Document does not support graphic objects.",
            not_found_msg="Image '%s' not found." % image_name
        )
        if isinstance(graphic, dict):
            return graphic

        anchor = graphic.getAnchor()
        text = anchor.getText()
        text.removeTextContent(graphic)

        return {"status": "ok", "deleted": image_name}


# ------------------------------------------------------------------
# ReplaceImage
# ------------------------------------------------------------------

class ReplaceImage(ToolBaseDummy):
    """Replace an image's source file keeping position and frame."""

    name = "replace_image"
    intent = "media"
    description = (
        "Replace an image's source file keeping position and frame."
    )
    parameters = {
        "type": "object",
        "properties": {
            "image_name": {
                "type": "string",
                "description": "Name of the image to replace (from list_images).",
            },
            "new_image_path": {
                "type": "string",
                "description": "Local file path or URL of the replacement image.",
            },
            "width_mm": {
                "type": "number",
                "description": "Optionally update width in millimetres.",
            },
            "height_mm": {
                "type": "number",
                "description": "Optionally update height in millimetres.",
            },
        },
        "required": ["image_name", "new_image_path"],
    }
    uno_services = ["com.sun.star.text.TextDocument"]
    is_mutation = True

    def execute(self, ctx, **kwargs):
        import uno

        image_name = kwargs.get("image_name", "")
        new_image_path = kwargs.get("new_image_path", "")

        graphic = self.get_item(
            ctx.doc, "getGraphicObjects", image_name,
            missing_msg="Document does not support graphic objects.",
            not_found_msg="Image '%s' not found." % image_name
        )
        if isinstance(graphic, dict):
            return graphic

        # Auto-download URLs
        if new_image_path.startswith("http://") or new_image_path.startswith("https://"):
            new_image_path = _download_image_to_cache(new_image_path)
        if not os.path.isfile(new_image_path):
            return self._tool_error(
                f"File not found: {new_image_path}",
                code="FILE_NOT_FOUND",
                path=new_image_path
            )

        file_url = uno.systemPathToFileUrl(os.path.abspath(new_image_path))

        graphic.setPropertyValue("GraphicURL", file_url)

        # Optionally update size
        width_mm = kwargs.get("width_mm")
        height_mm = kwargs.get("height_mm")
        if width_mm is not None or height_mm is not None:
            from com.sun.star.awt import Size
            current = graphic.getPropertyValue("Size")
            new_size = Size()
            new_size.Width = int(width_mm * 100) if width_mm is not None else current.Width
            new_size.Height = int(height_mm * 100) if height_mm is not None else current.Height
            graphic.setPropertyValue("Size", new_size)

        return {
            "status": "ok",
            "image_name": image_name,
        }


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------

def _download_image_to_cache(url, verify_ssl=False, force=False):
    """Download an image URL to the local cache directory.

    Returns the local file path. Uses a URL-based hash for caching.
    """
    import urllib.request
    import ssl

    os.makedirs(_IMAGE_CACHE_DIR, exist_ok=True)

    # Derive a stable filename from the URL
    url_hash = hashlib.sha256(url.encode("utf-8")).hexdigest()[:16]
    # Try to preserve the file extension
    ext = ""
    url_path = url.split("?")[0]
    if "." in url_path.split("/")[-1]:
        ext = "." + url_path.split("/")[-1].rsplit(".", 1)[-1]
        # Sanitize extension
        ext = ext[:6].lower()
        if not ext.replace(".", "").isalnum():
            ext = ""
    if not ext:
        ext = ".png"

    local_path = os.path.join(_IMAGE_CACHE_DIR, url_hash + ext)

    if not force and os.path.isfile(local_path):
        log.debug("download_image: cache hit %s -> %s", url, local_path)
        return local_path

    log.info("download_image: downloading %s -> %s", url, local_path)

    if verify_ssl:
        context = None
    else:
        context = ssl.create_default_context()
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE

    request = urllib.request.Request(url)
    request.add_header("User-Agent", "WriterAgent/1.0")

    with urllib.request.urlopen(request, context=context) as response:
        data = response.read()

    with open(local_path, "wb") as f:
        f.write(data)

    return local_path
