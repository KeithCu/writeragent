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

from plugin.framework.tool_base import ToolBase
from plugin.framework.image_tools import (
    insert_image, replace_image_in_place, get_selected_image_base64,
)

log = logging.getLogger("localwriter.writer")


class GenerateImage(ToolBase):
    """Generate an image from a text prompt and insert it."""

    name = "generate_image"
    intent = "media"
    description = (
        "Generate an image from a text prompt and insert it "
        "into the document."
    )
    parameters = {
        "type": "object",
        "properties": {
            "prompt": {
                "type": "string",
                "description": "The visual description of the image to generate.",
            },
            "width": {
                "type": "integer",
                "description": "Width in pixels (default 512).",
            },
            "height": {
                "type": "integer",
                "description": "Height in pixels (default 512).",
            },
        },
        "required": ["prompt"],
    }
    doc_types = ["writer", "calc", "draw", "impress"]
    is_mutation = True
    long_running = True

    def execute(self, ctx, **kwargs):
        prompt = kwargs.get("prompt")
        width = kwargs.get("width", 512)
        height = kwargs.get("height", 512)

        paths, error = ctx.services.ai.generate_image(
            prompt, width=width, height=height)

        if not paths:
            return {
                "status": "error",
                "message": error or "Generation failed: no image returned.",
            }

        insert_image(ctx.ctx, ctx.doc, paths[0], width, height, title=prompt)
        return {
            "status": "ok",
            "message": "Image generated and inserted.",
        }


class EditImage(ToolBase):
    """Edit the currently selected image using img2img."""

    name = "edit_image"
    intent = "media"
    description = (
        "Edit the selected image using a text prompt (Img2Img). "
        "If no image is selected, it will fail."
    )
    parameters = {
        "type": "object",
        "properties": {
            "prompt": {
                "type": "string",
                "description": "The visual description of the desired image version.",
            },
            "strength": {
                "type": "number",
                "description": "How much to change the image (0.0=none, 1.0=full). Default 0.6.",
            },
        },
        "required": ["prompt"],
    }
    doc_types = ["writer", "calc", "draw", "impress"]
    is_mutation = True
    long_running = True

    def execute(self, ctx, **kwargs):
        prompt = kwargs.get("prompt")
        strength = kwargs.get("strength", 0.6)

        source_b64 = get_selected_image_base64(ctx.doc, ctx.ctx)
        if not source_b64:
            return {
                "status": "error",
                "message": "No image selected. Please select an image first.",
            }

        paths, error = ctx.services.ai.generate_image(
            prompt, source_image=source_b64, strength=strength,
        )

        if not paths:
            return {
                "status": "error",
                "message": error or "Editing failed: no image returned.",
            }

        replaced = replace_image_in_place(
            ctx.ctx, ctx.doc, paths[0], 512, 512, title=prompt,
        )
        if not replaced:
            insert_image(ctx.ctx, ctx.doc, paths[0], 512, 512, title=prompt)

        return {
            "status": "ok",
            "message": "Image edited and inserted.",
        }

# Persistent cache directory for downloaded images.
_IMAGE_CACHE_DIR = os.path.join(tempfile.gettempdir(), "localwriter_images")


# ------------------------------------------------------------------
# ListImages
# ------------------------------------------------------------------

class ListImages(ToolBase):
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
    doc_types = ["writer"]

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

class GetImageInfo(ToolBase):
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
    doc_types = ["writer"]

    def execute(self, ctx, **kwargs):
        image_name = kwargs.get("image_name", "")
        if not image_name:
            return {"status": "error", "message": "image_name is required."}

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
            para_ranges = doc_svc.get_paragraph_ranges(doc)
            text_obj = doc.getText()
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

class SetImageProperties(ToolBase):
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
    doc_types = ["writer"]
    is_mutation = True

    def execute(self, ctx, **kwargs):
        image_name = kwargs.get("image_name", "")
        if not image_name:
            return {"status": "error", "message": "image_name is required."}

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

class DownloadImage(ToolBase):
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
    doc_types = ["writer"]

    def execute(self, ctx, **kwargs):
        url = kwargs.get("url", "")
        if not url:
            return {"status": "error", "message": "url is required."}

        verify_ssl = kwargs.get("verify_ssl", False)
        force = kwargs.get("force", False)

        try:
            local_path = _download_image_to_cache(url, verify_ssl=verify_ssl, force=force)
            return {
                "status": "ok",
                "local_path": local_path,
                "url": url,
            }
        except Exception as e:
            return {"status": "error", "message": str(e)}


# ------------------------------------------------------------------
# InsertImage
# ------------------------------------------------------------------

class InsertImage(ToolBase):
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
    doc_types = ["writer"]
    is_mutation = True

    def execute(self, ctx, **kwargs):
        import uno

        image_path = kwargs.get("image_path", "")
        if not image_path:
            return {"status": "error", "message": "image_path is required."}

        width_mm = kwargs.get("width_mm", 80)
        height_mm = kwargs.get("height_mm", 80)
        locator = kwargs.get("locator")
        paragraph_index = kwargs.get("paragraph_index")

        doc = ctx.doc

        # Auto-download URLs
        if image_path.startswith("http://") or image_path.startswith("https://"):
            try:
                image_path = _download_image_to_cache(image_path)
            except Exception as e:
                return {"status": "error", "message": "Download failed: %s" % e}

        # Verify local file exists
        if not os.path.isfile(image_path):
            return {
                "status": "error",
                "message": "File not found: %s" % image_path,
            }

        # Convert to file:// URL
        file_url = uno.systemPathToFileUrl(os.path.abspath(image_path))

        try:
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
                    return {
                        "status": "error",
                        "message": "Paragraph %d not found." % paragraph_index,
                    }
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
        except Exception as e:
            return {"status": "error", "message": str(e)}


# ------------------------------------------------------------------
# DeleteImage
# ------------------------------------------------------------------

class DeleteImage(ToolBase):
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
    doc_types = ["writer"]
    is_mutation = True

    def execute(self, ctx, **kwargs):
        image_name = kwargs.get("image_name", "")
        if not image_name:
            return {"status": "error", "message": "image_name is required."}

        graphic = self.get_item(
            ctx.doc, "getGraphicObjects", image_name,
            missing_msg="Document does not support graphic objects.",
            not_found_msg="Image '%s' not found." % image_name
        )
        if isinstance(graphic, dict):
            return graphic

        try:
            anchor = graphic.getAnchor()
            text = anchor.getText()
            text.removeTextContent(graphic)
        except Exception as e:
            return {"status": "error", "message": "Failed to delete image: %s" % e}

        return {"status": "ok", "deleted": image_name}


# ------------------------------------------------------------------
# ReplaceImage
# ------------------------------------------------------------------

class ReplaceImage(ToolBase):
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
    doc_types = ["writer"]
    is_mutation = True

    def execute(self, ctx, **kwargs):
        import uno

        image_name = kwargs.get("image_name", "")
        new_image_path = kwargs.get("new_image_path", "")
        if not image_name:
            return {"status": "error", "message": "image_name is required."}
        if not new_image_path:
            return {"status": "error", "message": "new_image_path is required."}

        graphic = self.get_item(
            ctx.doc, "getGraphicObjects", image_name,
            missing_msg="Document does not support graphic objects.",
            not_found_msg="Image '%s' not found." % image_name
        )
        if isinstance(graphic, dict):
            return graphic

        # Auto-download URLs
        if new_image_path.startswith("http://") or new_image_path.startswith("https://"):
            try:
                new_image_path = _download_image_to_cache(new_image_path)
            except Exception as e:
                return {"status": "error", "message": "Download failed: %s" % e}

        if not os.path.isfile(new_image_path):
            return {
                "status": "error",
                "message": "File not found: %s" % new_image_path,
            }

        file_url = uno.systemPathToFileUrl(os.path.abspath(new_image_path))

        try:
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
        except Exception as e:
            return {"status": "error", "message": str(e)}


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
    request.add_header("User-Agent", "LocalWriter/1.0")

    with urllib.request.urlopen(request, context=context) as response:
        data = response.read()

    with open(local_path, "wb") as f:
        f.write(data)

    return local_path
