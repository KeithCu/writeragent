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
"""Writer text frame tools (textframes domain, specialized tier).

Frame tool logic adapted from nelson-mcp (MPL 2.0):
nelson-mcp/plugin/writer/tools/frames.py
"""

import math
from typing import Any

from plugin.doc.paragraph_search import confirm_paragraph_index
from plugin.doc.text_helpers import get_string_without_tracked_deletions
from plugin.doc.undo import commit_or_rollback_mutation
from plugin.doc.visual_helpers import anchor_type_index, mm_to_unit
from plugin.framework.uno_context import uno_same
from ..specialized_base import ToolWriterTextFramesBase

_FRAME_INFO_MAX_CHARS = 4000


def _frame_size_fields(size: Any) -> dict[str, Any]:
    return {
        "width_mm": size.Width / 100.0,
        "height_mm": size.Height / 100.0,
        "width_100mm": size.Width,
        "height_100mm": size.Height,
    }


def _frame_text(frame: Any, limit: int | None = None) -> str:
    full_text = get_string_without_tracked_deletions(frame.getText())
    if limit is not None and len(full_text) > limit:
        return full_text[:limit] + "..."
    return full_text


def _frame_or_error(tool: Any, ctx: Any, name: str) -> Any:
    if not name:
        return tool._tool_error("name is required.")
    return tool.get_item(
        ctx.doc,
        "getTextFrames",
        name,
        missing_msg="Document does not support text frames.",
        not_found_msg="Text frame '%s' not found." % name,
    )


def text_content_anchor(index: int) -> Any | None:
    from com.sun.star.text.TextContentAnchorType import AT_PARAGRAPH, AS_CHARACTER, AT_PAGE, AT_FRAME, AT_CHARACTER

    anchor_map = {0: AT_PARAGRAPH, 1: AS_CHARACTER, 2: AT_PAGE, 3: AT_FRAME, 4: AT_CHARACTER}
    return anchor_map.get(index)


class FrameList(ToolWriterTextFramesBase):
    """List all text frames in the document."""

    name: str | None = "frame_list"
    description: str = "List all text frames in the document."
    parameters: dict[str, Any] | None = {"type": "object", "properties": {}, "required": []}

    def execute(self, ctx: Any, **kwargs: Any) -> dict[str, Any]:
        doc = ctx.doc
        text_frames = self.get_collection(doc, "getTextFrames", "Document does not support text frames.")
        if isinstance(text_frames, dict):
            return text_frames

        frames = []
        for name in text_frames.getElementNames():
            frame = text_frames.getByName(name)
            size = frame.getPropertyValue("Size")
            content_preview = _frame_text(frame, limit=200)
            frames.append({"name": name, **_frame_size_fields(size), "content_preview": content_preview})

        return {"status": "ok", "frames": frames, "count": len(frames)}


# ------------------------------------------------------------------
# FrameGetInfo
# ------------------------------------------------------------------


class FrameGetInfo(ToolWriterTextFramesBase):
    """Get detailed info about a text frame. content is capped at 4000 chars; content_length is the full visible length."""

    name: str | None = "frame_get_info"
    description: str = (
        "Get detailed info about a text frame. content is capped at 4000 chars; content_length is the full visible length."
    )
    parameters: dict[str, Any] | None = {"type": "object", "properties": {"name": {"type": "string", "description": "Name of the text frame (from frame_list)."}}, "required": ["name"]}

    def execute(self, ctx: Any, **kwargs: Any) -> dict[str, Any]:
        frame_name = kwargs["name"]
        frame = _frame_or_error(self, ctx, frame_name)
        if isinstance(frame, dict):
            return frame

        size = frame.getPropertyValue("Size")
        anchor_type = anchor_type_index(frame.getPropertyValue("AnchorType"))
        hori_orient = int(frame.getPropertyValue("HoriOrient"))
        vert_orient = int(frame.getPropertyValue("VertOrient"))
        full_content = _frame_text(frame)
        content_length = len(full_content)
        truncated = content_length > _FRAME_INFO_MAX_CHARS

        anchor = frame.getAnchor()
        text_obj = ctx.doc.getText()
        paragraph_index = None
        if uno_same(anchor.getText(), text_obj):
            doc_svc = ctx.services.document
            para_ranges = doc_svc.get_paragraph_ranges(ctx.doc)
            found = doc_svc.find_paragraph_for_range(anchor, para_ranges, text_obj)
            paragraph_index = confirm_paragraph_index(text_obj, anchor, para_ranges, found)

        return {
            "status": "ok",
            "frame_name": frame_name,
            **_frame_size_fields(size),
            "anchor_type": anchor_type,
            "hori_orient": hori_orient,
            "vert_orient": vert_orient,
            "content": full_content[:_FRAME_INFO_MAX_CHARS] if truncated else full_content,
            "content_length": content_length,
            "truncated": truncated,
            "paragraph_index": paragraph_index,
        }


# ------------------------------------------------------------------
# FrameSetProperties
# ------------------------------------------------------------------


class FrameSetProperties(ToolWriterTextFramesBase):
    """Resize, re-anchor, or set the orientation of a text frame."""

    name: str | None = "frame_set_properties"
    description: str = "Resize, re-anchor, or set the orientation of a text frame."
    parameters: dict[str, Any] | None = {
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "Name of the text frame (from frame_list)."},
            "width_mm": {"type": "number", "description": "New width in millimetres (> 0)."},
            "height_mm": {"type": "number", "description": "New height in millimetres (> 0)."},
            "anchor_type": {"type": "integer", "description": ("Anchor type: 0=AT_PARAGRAPH, 1=AS_CHARACTER, 2=AT_PAGE, 3=AT_FRAME, 4=AT_CHARACTER.")},
            "hori_orient": {"type": "integer", "description": "Raw UNO horizontal orientation integer."},
            "vert_orient": {"type": "integer", "description": "Raw UNO vertical orientation integer."},
        },
        "required": ["name"],
    }
    is_mutation: bool | None = True

    def execute(self, ctx: Any, **kwargs: Any) -> dict[str, Any]:
        frame_name = kwargs.get("name")
        frame = _frame_or_error(self, ctx, frame_name or "")
        if isinstance(frame, dict):
            return frame

        width_mm = kwargs.get("width_mm")
        height_mm = kwargs.get("height_mm")
        anchor_type = kwargs.get("anchor_type")
        hori_orient = kwargs.get("hori_orient")
        vert_orient = kwargs.get("vert_orient")

        if (
            width_mm is None
            and height_mm is None
            and anchor_type is None
            and hori_orient is None
            and vert_orient is None
        ):
            return self._tool_error("No properties to set.", code="NO_CHANGES")

        for param_name, val in (("width_mm", width_mm), ("height_mm", height_mm)):
            if val is not None:
                if type(val) is bool:
                    return self._tool_error(
                        f"{param_name} must be a positive finite number.",
                        code="INVALID_PARAMETER",
                        parameter=param_name,
                    )
                try:
                    fval = float(val)
                    if not (math.isfinite(fval) and fval > 0):
                        return self._tool_error(
                            f"{param_name} must be a positive finite number.",
                            code="INVALID_PARAMETER",
                            parameter=param_name,
                        )
                except (TypeError, ValueError):
                    return self._tool_error(
                        f"{param_name} must be a positive finite number.",
                        code="INVALID_PARAMETER",
                        parameter=param_name,
                    )

        if anchor_type is not None:
            if type(anchor_type) is not int or not (0 <= anchor_type <= 4):
                return self._tool_error(
                    "anchor_type must be an integer between 0 and 4.",
                    code="INVALID_PARAMETER",
                    parameter="anchor_type",
                )

        if hori_orient is not None and type(hori_orient) is not int:
            return self._tool_error(
                "hori_orient must be an integer.",
                code="INVALID_PARAMETER",
                parameter="hori_orient",
            )

        if vert_orient is not None and type(vert_orient) is not int:
            return self._tool_error(
                "vert_orient must be an integer.",
                code="INVALID_PARAMETER",
                parameter="vert_orient",
            )

        new_size = None
        if width_mm is not None or height_mm is not None:
            from com.sun.star.awt import Size

            current = frame.getPropertyValue("Size")
            new_size = Size()
            new_size.Width = mm_to_unit(width_mm) if width_mm is not None else current.Width
            new_size.Height = mm_to_unit(height_mm) if height_mm is not None else current.Height

        anchor_val = text_content_anchor(anchor_type) if anchor_type is not None else None

        updated: list[str] = []

        def _apply() -> None:
            if new_size is not None:
                frame.setPropertyValue("Size", new_size)
                updated.append("size")
            if anchor_val is not None:
                frame.setPropertyValue("AnchorType", anchor_val)
                updated.append("anchor_type")
            if hori_orient is not None:
                frame.setPropertyValue("HoriOrient", hori_orient)
                updated.append("hori_orient")
            if vert_orient is not None:
                frame.setPropertyValue("VertOrient", vert_orient)
                updated.append("vert_orient")

        err = commit_or_rollback_mutation(
            self,
            ctx.doc,
            "WriterAgent: Set text frame properties",
            _apply,
            "Undo is unavailable; the text frame was not changed.",
            "Failed to set text frame properties; the change was rolled back",
            code="SET_PROPERTIES_FAILED",
        )
        if err is not None:
            return err

        return {"status": "ok", "frame_name": frame_name, "updated": updated}
