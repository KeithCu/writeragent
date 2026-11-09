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

from plugin.doc.text_helpers import get_string_without_tracked_deletions
from plugin.doc.undo import commit_or_rollback_mutation
from plugin.doc.visual_helpers import anchor_type_index, mm_to_unit
from plugin.framework.uno_context import uno_same
from ..specialized_base import ToolWriterTextFramesBase, target_params
from ..target_resolver import resolve_target_cursor

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


def _frame_or_error(tool: Any, ctx: Any, name: str | None) -> Any:
    if not name:
        return tool._tool_error("name is required.")
    return tool.get_item(
        ctx.doc,
        "getTextFrames",
        name,
        missing_msg="Document does not support text frames.",
        not_found_msg="Text frame '%s' not found." % name,
        list_tool="frame_list",
    )


def _validate_frame_mm(tool: Any, param_name: str, val: Any) -> dict[str, Any] | None:
    try:
        fval = float(val)
        if type(val) is bool or not (math.isfinite(fval) and fval > 0):
            raise ValueError
    except (TypeError, ValueError):
        return tool._tool_error(
            f"{param_name} must be a positive finite number.",
            code="INVALID_PARAMETER",
            parameter=param_name,
        )
    return None


def _validate_frame_anchor_type(tool: Any, val: Any) -> dict[str, Any] | None:
    if type(val) is not int or not (0 <= val <= 4):
        return tool._tool_error(
            "anchor_type must be an integer between 0 and 4.",
            code="INVALID_PARAMETER",
            parameter="anchor_type",
        )
    return None


def _validate_frame_orientation(tool: Any, param_name: str, val: Any) -> dict[str, Any] | None:
    if type(val) is not int:
        return tool._tool_error(
            f"{param_name} must be an integer.",
            code="INVALID_PARAMETER",
            parameter=param_name,
        )
    return None


def _validate_frame_measurement(tool: Any, param_name: str, val: Any) -> dict[str, Any] | None:
    if val is None:
        return None
    if param_name in ("width_mm", "height_mm"):
        return _validate_frame_mm(tool, param_name, val)
    if param_name == "anchor_type":
        return _validate_frame_anchor_type(tool, val)
    return _validate_frame_orientation(tool, param_name, val)


def text_content_anchor(index: int) -> Any:
    from com.sun.star.text.TextContentAnchorType import AT_PARAGRAPH, AS_CHARACTER, AT_PAGE, AT_FRAME, AT_CHARACTER

    return (AT_PARAGRAPH, AS_CHARACTER, AT_PAGE, AT_FRAME, AT_CHARACTER)[index]


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
            paragraph_index = doc_svc.find_paragraph_for_range(anchor, para_ranges, text_obj)

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
            **(
                {
                    "hint": (
                        "content is the first 4000 of %d characters. This tool has no offset. "
                        "Read the body with get_document_content scope='range'."
                        % content_length
                    )
                }
                if truncated
                else {}
            ),
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
        frame = _frame_or_error(self, ctx, frame_name)
        if isinstance(frame, dict):
            return frame

        width_mm = kwargs.get("width_mm")
        height_mm = kwargs.get("height_mm")
        anchor_type = kwargs.get("anchor_type")
        hori_orient = kwargs.get("hori_orient")
        vert_orient = kwargs.get("vert_orient")

        for param_name, val in (
            ("width_mm", width_mm),
            ("height_mm", height_mm),
            ("anchor_type", anchor_type),
            ("hori_orient", hori_orient),
            ("vert_orient", vert_orient),
        ):
            if val is None:
                continue
            err = _validate_frame_measurement(self, param_name, val)
            if err is not None:
                return err

        pending: dict[str, tuple[str, Any]] = {}
        if width_mm is not None or height_mm is not None:
            from com.sun.star.awt import Size

            current = frame.getPropertyValue("Size")
            new_size = Size()
            new_size.Width = mm_to_unit(width_mm) if width_mm is not None else current.Width
            new_size.Height = mm_to_unit(height_mm) if height_mm is not None else current.Height
            pending["size"] = ("Size", new_size)

        if anchor_type is not None:
            pending["anchor_type"] = ("AnchorType", text_content_anchor(anchor_type))
        if hori_orient is not None:
            pending["hori_orient"] = ("HoriOrient", hori_orient)
        if vert_orient is not None:
            pending["vert_orient"] = ("VertOrient", vert_orient)

        if not pending:
            return self._tool_error("No properties to set.", code="NO_CHANGES")

        def _apply() -> None:
            for prop, val in pending.values():
                frame.setPropertyValue(prop, val)

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

        return {"status": "ok", "frame_name": frame_name, "updated": list(pending)}


class FrameInsert(ToolWriterTextFramesBase):
    """Insert a new text frame at a document target."""

    name: str | None = "frame_insert"
    description: str = "Insert a new text frame with optional initial text and size (millimetres)."
    parameters: dict[str, Any] | None = {
        "type": "object",
        "properties": {
            **target_params("the text frame"),
            "name": {"type": "string", "description": "Optional frame name (from frame_list when set)."},
            "text": {"type": "string", "description": "Initial text placed inside the frame."},
            "width_mm": {"type": "number", "description": "Frame width in millimetres (default 80)."},
            "height_mm": {"type": "number", "description": "Frame height in millimetres (default 30)."},
            "anchor_type": {
                "type": "integer",
                "description": ("Anchor type: 0=AT_PARAGRAPH (default), 1=AS_CHARACTER, 2=AT_PAGE, 3=AT_FRAME, 4=AT_CHARACTER."),
            },
        },
        "required": [],
    }
    is_mutation: bool | None = True

    def execute(self, ctx: Any, **kwargs: Any) -> dict[str, Any]:
        target = kwargs.get("target", "selection")
        old_content = kwargs.get("old_content")
        frame_name = kwargs.get("name")
        text = kwargs.get("text") or ""
        width_mm = kwargs.get("width_mm")
        height_mm = kwargs.get("height_mm")
        anchor_type = kwargs.get("anchor_type", 0)

        for param_name, val in (("width_mm", width_mm), ("height_mm", height_mm), ("anchor_type", anchor_type)):
            if val is None:
                continue
            err = _validate_frame_measurement(self, param_name, val)
            if err is not None:
                return err

        doc = ctx.doc
        if frame_name:
            text_frames = doc.getTextFrames()
            if text_frames.hasByName(frame_name):
                return self._tool_error(
                    "Text frame %r already exists." % frame_name,
                    code="INVALID_PARAMETER",
                    parameter="name",
                )
        try:
            cursor = resolve_target_cursor(ctx, target, old_content, case_sensitive=kwargs.get("case_sensitive"))
        except ValueError as ve:
            return self._tool_error(str(ve))

        from com.sun.star.awt import Size

        frame = doc.createInstance("com.sun.star.text.TextFrame")
        size = Size()
        size.Width = mm_to_unit(width_mm if width_mm is not None else 80.0)
        size.Height = mm_to_unit(height_mm if height_mm is not None else 30.0)
        frame.setPropertyValue("Size", size)
        frame.setPropertyValue("AnchorType", text_content_anchor(anchor_type))

        replace_match = target == "search" and bool(old_content)

        def mutate() -> dict[str, Any] | None:
            if replace_match:
                cursor.setString("")
            host = cursor.getText()
            host.insertTextContent(cursor, frame, False)
            if frame_name:
                # XNamed::setName is valid only after the frame is anchored (not a Name property).
                frame.setName(frame_name)
            if text:
                frame_text = frame.getText()
                frame_cursor = frame_text.createTextCursor()
                frame_cursor.gotoStart(False)
                frame_text.insertString(frame_cursor, text, False)
            return None

        err = commit_or_rollback_mutation(
            self,
            doc,
            "WriterAgent: Insert text frame",
            mutate,
            "Undo is unavailable; the text frame was not inserted.",
            "Failed to insert text frame; the change was rolled back",
            code="INSERT_FRAME_FAILED",
        )
        if err is not None:
            return err

        inserted_name = frame_name or frame.getName() or ""
        return {"status": "ok", "frame_name": inserted_name, "anchor_type": anchor_type}


class FrameDelete(ToolWriterTextFramesBase):
    """Delete a text frame by name."""

    name: str | None = "frame_delete"
    description: str = "Delete a text frame by name (see frame_list)."
    parameters: dict[str, Any] | None = {
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "Name of the text frame (from frame_list)."},
        },
        "required": ["name"],
    }
    is_mutation: bool | None = True

    def execute(self, ctx: Any, **kwargs: Any) -> dict[str, Any]:
        frame_name = kwargs.get("name")
        frame = _frame_or_error(self, ctx, frame_name)
        if isinstance(frame, dict):
            return frame

        doc = ctx.doc

        def mutate() -> dict[str, Any] | None:
            anchor = frame.getAnchor()
            anchor.getText().removeTextContent(frame)
            return None

        err = commit_or_rollback_mutation(
            self,
            doc,
            "WriterAgent: Delete text frame",
            mutate,
            "Undo is unavailable; the text frame was not deleted.",
            "Failed to delete text frame; the change was rolled back",
            code="DELETE_FRAME_FAILED",
        )
        if err is not None:
            return err

        return {"status": "ok", "frame_name": frame_name, "deleted": True}
