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
"""Writer text frame management tools (layout domain, specialized tier).

Frame tool logic adapted from nelson-mcp (MPL 2.0):
nelson-mcp/plugin/modules/writer/tools/frames.py
"""

import logging

from plugin.modules.writer.base import ToolWriterLayoutBase

log = logging.getLogger("writeragent.writer")


class ListTextFrames(ToolWriterLayoutBase):
    """List all text frames in the document."""

    name = "list_text_frames"
    description = "List all text frames in the document."
    parameters = {
        "type": "object",
        "properties": {},
        "required": [],
    }

    def execute(self, ctx, **kwargs):
        doc = ctx.doc
        text_frames = self.get_collection(doc, "getTextFrames", "Document does not support text frames.")
        if isinstance(text_frames, dict):
            return text_frames

        frames = []
        for name in text_frames.getElementNames():
            try:
                frame = text_frames.getByName(name)
                size = frame.getPropertyValue("Size")

                # Text content preview (first 200 chars)
                content_preview = ""
                try:
                    frame_text = frame.getText()
                    cursor = frame_text.createTextCursor()
                    cursor.gotoStart(False)
                    cursor.gotoEnd(True)
                    full_text = cursor.getString()
                    if len(full_text) > 200:
                        content_preview = full_text[:200] + "..."
                    else:
                        content_preview = full_text
                except Exception:
                    pass

                frames.append({
                    "name": name,
                    "width_mm": size.Width / 100.0,
                    "height_mm": size.Height / 100.0,
                    "width_100mm": size.Width,
                    "height_100mm": size.Height,
                    "content_preview": content_preview,
                })
            except Exception as e:
                log.debug("list_text_frames: skip '%s': %s", name, e)

        return {"status": "ok", "frames": frames, "count": len(frames)}


# ------------------------------------------------------------------
# GetTextFrameInfo
# ------------------------------------------------------------------

class GetTextFrameInfo(ToolWriterLayoutBase):
    """Get detailed info about a text frame."""

    name = "get_text_frame_info"
    description = "Get detailed info about a text frame."
    parameters = {
        "type": "object",
        "properties": {
            "frame_name": {
                "type": "string",
                "description": "Name of the text frame (from list_text_frames).",
            },
        },
        "required": ["frame_name"],
    }

    def execute(self, ctx, **kwargs):
        frame_name = kwargs.get("frame_name", "")
        if not frame_name:
            return self._tool_error("frame_name is required.")

        frame = self.get_item(
            ctx.doc, "getTextFrames", frame_name,
            missing_msg="Document does not support text frames.",
            not_found_msg="Text frame '%s' not found." % frame_name
        )
        if isinstance(frame, dict):
            return frame

        size = frame.getPropertyValue("Size")

        # Anchor type
        anchor_type = None
        try:
            anchor_type = int(frame.getPropertyValue("AnchorType").value)
        except Exception:
            try:
                anchor_type = int(frame.getPropertyValue("AnchorType"))
            except Exception:
                pass

        # Orientation
        hori_orient = None
        vert_orient = None
        try:
            hori_orient = int(frame.getPropertyValue("HoriOrient"))
        except Exception:
            pass
        try:
            vert_orient = int(frame.getPropertyValue("VertOrient"))
        except Exception:
            pass

        # Full text content
        content = ""
        try:
            frame_text = frame.getText()
            cursor = frame_text.createTextCursor()
            cursor.gotoStart(False)
            cursor.gotoEnd(True)
            content = cursor.getString()
        except Exception:
            pass

        # Paragraph index via anchor
        paragraph_index = -1
        try:
            anchor = frame.getAnchor()
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
            "frame_name": frame_name,
            "width_mm": size.Width / 100.0,
            "height_mm": size.Height / 100.0,
            "width_100mm": size.Width,
            "height_100mm": size.Height,
            "anchor_type": anchor_type,
            "hori_orient": hori_orient,
            "vert_orient": vert_orient,
            "content": content,
            "paragraph_index": paragraph_index,
        }


# ------------------------------------------------------------------
# SetTextFrameProperties
# ------------------------------------------------------------------

class SetTextFrameProperties(ToolWriterLayoutBase):
    """Resize or reposition a text frame."""

    name = "set_text_frame_properties"
    description = "Resize or reposition a text frame."
    parameters = {
        "type": "object",
        "properties": {
            "frame_name": {
                "type": "string",
                "description": "Name of the text frame (from list_text_frames).",
            },
            "width_mm": {
                "type": "number",
                "description": "New width in millimetres.",
            },
            "height_mm": {
                "type": "number",
                "description": "New height in millimetres.",
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
        "required": ["frame_name"],
    }
    is_mutation = True

    def execute(self, ctx, **kwargs):
        frame_name = kwargs.get("frame_name", "")
        if not frame_name:
            return self._tool_error("frame_name is required.")

        frame = self.get_item(
            ctx.doc, "getTextFrames", frame_name,
            missing_msg="Document does not support text frames.",
            not_found_msg="Text frame '%s' not found." % frame_name
        )
        if isinstance(frame, dict):
            return frame

        updated = []

        # Size
        width_mm = kwargs.get("width_mm")
        height_mm = kwargs.get("height_mm")
        if width_mm is not None or height_mm is not None:
            from com.sun.star.awt import Size
            current = frame.getPropertyValue("Size")
            new_size = Size()
            new_size.Width = int(width_mm * 100) if width_mm is not None else current.Width
            new_size.Height = int(height_mm * 100) if height_mm is not None else current.Height
            frame.setPropertyValue("Size", new_size)
            updated.append("size")

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
                frame.setPropertyValue("AnchorType", anchor_map[anchor_type])
                updated.append("anchor_type")

        # Orientation
        hori_orient = kwargs.get("hori_orient")
        if hori_orient is not None:
            frame.setPropertyValue("HoriOrient", hori_orient)
            updated.append("hori_orient")

        vert_orient = kwargs.get("vert_orient")
        if vert_orient is not None:
            frame.setPropertyValue("VertOrient", vert_orient)
            updated.append("vert_orient")

        return {
            "status": "ok",
            "frame_name": frame_name,
            "updated": updated,
        }
