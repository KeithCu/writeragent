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

from com.sun.star.awt import Size
from com.sun.star.text.TextContentAnchorType import AS_CHARACTER, AT_CHARACTER, AT_FRAME, AT_PAGE, AT_PARAGRAPH

from plugin.doc.text_helpers import TrackedDeletions, get_string_without_tracked_deletions
from plugin.doc.undo import commit_or_rollback_mutation
from plugin.doc.visual_helpers import anchor_type_index, mm_to_unit
from plugin.framework.i18n import _
from plugin.framework.uno_context import uno_same
from plugin.writer.record_changes import insert_recorded_content, record_changes
from ..edit_review import flag_tracked, flag_untracked, flag_untracked_delete
from ..search import text_box_shapes
from ..specialized_base import ToolWriterTextFramesBase, grouped_text_box_error, target_params
from ..target_resolver import insert_at_target

_FRAME_INFO_MAX_CHARS = 4000


def _frame_size_fields(size: Any) -> dict[str, Any]:
    return {
        "width_mm": size.Width / 100.0,
        "height_mm": size.Height / 100.0,
        "width_100mm": size.Width,
        "height_100mm": size.Height,
    }


def _frame_text(frame: Any, deletions: TrackedDeletions, limit: int | None = None) -> str:
    text = frame.getText()
    full_text = get_string_without_tracked_deletions(text, deletions.state_at(text))
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
        not_found_msg="Text frame '%s' not found." % name,
        list_tool="frame_list",
        extra_names=lambda: [box_name for _idx, _shape, box_name in text_box_shapes(ctx.doc)],
    )


def _text_box_owner(doc: Any, frame_name: str) -> tuple[int | str, Any] | None:
    return next(((i, shape) for i, shape, name in text_box_shapes(doc) if name == frame_name), None)


# Types are checked by prepare_tool_call (plugin/framework/tool.py); only ranges are checked here.
def _validate_frame_mm(tool: Any, param_name: str, val: Any) -> dict[str, Any] | None:
    if math.isfinite(val) and val > 0:
        return None
    return tool._tool_error(
        f"{param_name} must be a positive finite number.",
        code="INVALID_PARAM",
        parameter=param_name,
    )


def _validate_frame_anchor_type(tool: Any, val: Any) -> dict[str, Any] | None:
    if 0 <= val <= 4:
        return None
    return tool._tool_error(
        "anchor_type must be an integer between 0 and 4.",
        code="INVALID_PARAM",
        parameter="anchor_type",
    )


def _validate_frame_measurement(tool: Any, param_name: str, val: Any) -> dict[str, Any] | None:
    if val is None:
        return None
    if param_name in ("width_mm", "height_mm"):
        return _validate_frame_mm(tool, param_name, val)
    return _validate_frame_anchor_type(tool, val)


def text_content_anchor(index: int) -> Any:
    return (AT_PARAGRAPH, AS_CHARACTER, AT_PAGE, AT_FRAME, AT_CHARACTER)[index]


class FrameList(ToolWriterTextFramesBase):
    """List all text frames in the document."""

    name: str | None = "frame_list"
    description: str = "List all text frames in the document, including Word text boxes (shape_index and shape_name set; frame_delete removes the whole box, except one inside a group, whose shape_index is a dotted path)."
    parameters: dict[str, Any] | None = {"type": "object", "properties": {}, "required": []}

    def execute(self, ctx: Any, **kwargs: Any) -> dict[str, Any]:
        doc = ctx.doc
        text_frames = doc.getTextFrames()

        deletions = TrackedDeletions(doc)

        def entry(name: str, frame: Any) -> dict[str, Any]:
            size = frame.getPropertyValue("Size")
            content_preview = _frame_text(frame, deletions, limit=200)
            return {"name": name, **_frame_size_fields(size), "content_preview": content_preview}

        frames = [entry(name, text_frames.getByName(name)) for name in text_frames.getElementNames()]
        for idx, shape, name in text_box_shapes(doc):
            frames.append({**entry(name, text_frames.getByName(name)), "shape_index": idx, "shape_name": shape.getName()})

        return {"status": "ok", "frames": frames, "count": len(frames)}


class FrameGetInfo(ToolWriterTextFramesBase):
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

        owner = _text_box_owner(ctx.doc, frame_name)
        # LibreOffice gives an inline box's hidden frame AT_CHARACTER (sw/source/core/doc/textboxhelper.cxx:SwTextBoxHelper::changeAnchor).
        anchored = owner[1] if owner is not None else frame
        size = frame.getPropertyValue("Size")
        anchor_type = anchor_type_index(anchored.getPropertyValue("AnchorType"))
        hori_orient = int(anchored.getPropertyValue("HoriOrient"))
        vert_orient = int(anchored.getPropertyValue("VertOrient"))
        full_content = _frame_text(frame, TrackedDeletions(ctx.doc))
        content_length = len(full_content)
        truncated = content_length > _FRAME_INFO_MAX_CHARS

        anchor = anchored.getAnchor()
        text_obj = ctx.doc.getText()
        paragraph_index = None
        # getAnchor() is None for an AT_PAGE frame with a page number (sw/source/core/unocore/unoframe.cxx:SwXFrame::getSwAnchor)
        # and for an AT_PAGE shape (sw/source/core/unocore/unodraw.cxx:SwXShape::getAnchor), so it has no paragraph index.
        if anchor is not None and uno_same(anchor.getText(), text_obj):
            doc_svc = ctx.services.document
            para_ranges = doc_svc.get_paragraph_ranges(ctx.doc)
            paragraph_index = doc_svc.find_paragraph_for_range(anchor, para_ranges, text_obj)

        result: dict[str, Any] = {
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
                        "Read the body with get_content scope='range'."
                        % content_length
                    )
                }
                if truncated
                else {}
            ),
        }
        if owner is not None:
            result["shape_index"] = owner[0]
            result["shape_name"] = owner[1].getName()
        return result


class FrameSetProperties(ToolWriterTextFramesBase):
    """Resize, re-anchor, or set the orientation of a text frame."""

    name: str | None = "frame_set_properties"
    description: str = (
        "Resize, re-anchor, or set the orientation of a text frame. "
        "Not for Word text boxes (frame_list entries with shape_index): resize those with shape_upsert."
    )
    parameters: dict[str, Any] | None = {
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "Name of the text frame (from frame_list)."},
            "width_mm": {"type": "number", "description": "New width in millimetres (> 0)."},
            "height_mm": {"type": "number", "description": "New height in millimetres (> 0)."},
            "anchor_type": {"type": "integer", "description": "Anchor type: 0=AT_PARAGRAPH, 1=AS_CHARACTER, 2=AT_PAGE, 3=AT_FRAME, 4=AT_CHARACTER."},
            "hori_orient": {"type": "integer", "description": "Raw UNO horizontal orientation integer."},
            "vert_orient": {"type": "integer", "description": "Raw UNO vertical orientation integer."},
        },
        "required": ["name"],
    }
    is_mutation: bool | None = True

    def execute(self, ctx: Any, **kwargs: Any) -> dict[str, Any]:
        frame_name = kwargs["name"]
        frame = _frame_or_error(self, ctx, frame_name)
        if isinstance(frame, dict):
            return frame

        owner = _text_box_owner(ctx.doc, frame_name)
        if owner is not None:
            # LibreOffice copies a text box's size, anchor and orientation from the shape to the hidden frame,
            # never back (sw/source/core/doc/docfly.cxx:SwDoc::SetFlyFrameAttr). Writing the frame separates the
            # text from its shape, and AT_PAGE on an inline box's frame aborts LibreOffice.
            shape_index, shape = owner
            return self._tool_error(
                "Text frame '%s' is the text of a Word text box, so its size comes from the shape. "
                "Resize it with shape_upsert(action='edit', index=%r, width=..., height=...) in 1/100 mm "
                "(shapes toolset, delegate_to_subagent domain='shapes'). "
                "Its anchor and orientation cannot be set." % (frame_name, shape_index),
                code="TEXT_BOX_SHAPE",
                shape_index=shape_index,
                shape_name=shape.getName(),
            )

        width_mm = kwargs.get("width_mm")
        height_mm = kwargs.get("height_mm")
        anchor_type = kwargs.get("anchor_type")
        hori_orient = kwargs.get("hori_orient")
        vert_orient = kwargs.get("vert_orient")

        for param_name, val in (
            ("width_mm", width_mm),
            ("height_mm", height_mm),
            ("anchor_type", anchor_type),
        ):
            err = _validate_frame_measurement(self, param_name, val)
            if err is not None:
                return err

        pending: dict[str, tuple[str, Any]] = {}
        if width_mm is not None or height_mm is not None:
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
            "width_mm": {"type": "number", "default": 80.0, "description": "Frame width in millimetres."},
            "height_mm": {"type": "number", "default": 30.0, "description": "Frame height in millimetres."},
            "anchor_type": {
                "type": "integer",
                "default": 0,
                "description": "Anchor type: 0=AT_PARAGRAPH, 1=AS_CHARACTER, 2=AT_PAGE, 3=AT_FRAME, 4=AT_CHARACTER.",
            },
        },
        "required": [],
    }
    is_mutation: bool | None = True

    def execute(self, ctx: Any, **kwargs: Any) -> dict[str, Any]:
        frame_name = kwargs.get("name")
        text = kwargs.get("text") or ""
        width_mm = kwargs["width_mm"]
        height_mm = kwargs["height_mm"]
        anchor_type = kwargs["anchor_type"]

        for param_name, val in (("width_mm", width_mm), ("height_mm", height_mm), ("anchor_type", anchor_type)):
            err = _validate_frame_measurement(self, param_name, val)
            if err is not None:
                return err

        doc = ctx.doc
        if frame_name:
            text_frames = doc.getTextFrames()
            if text_frames.hasByName(frame_name):
                return self._tool_error(
                    "Text frame %r already exists." % frame_name,
                    code="INVALID_PARAM",
                    parameter="name",
                )

        frame = doc.createInstance("com.sun.star.text.TextFrame")
        size = Size()
        size.Width = mm_to_unit(width_mm)
        size.Height = mm_to_unit(height_mm)
        frame.setPropertyValue("Size", size)
        frame.setPropertyValue("AnchorType", text_content_anchor(anchor_type))

        recorded = False
        replaced_pending = False

        def mutate(cursor: Any) -> dict[str, Any] | None:
            nonlocal recorded, replaced_pending
            # insert_at_target has already deleted a search match. A recorded deletion only strikes
            # the match, so the cursor still spans it.
            replaced_pending = kwargs.get("target") == "search" and not cursor.isCollapsed()
            host = cursor.getText()
            if anchor_type == 1:
                recorded = insert_recorded_content(doc, host, cursor, frame)
            else:
                host.insertTextContent(cursor, frame, False)
                if anchor_type == 2:
                    # Inserting an AT_PAGE frame without a page number makes it AT_CHARACTER
                    # (sw/source/core/unocore/unoframe.cxx:SwXFrame::attachToRange). Setting the type on the
                    # inserted frame lets LibreOffice fill in the page it sits on
                    # (sw/source/core/frmedt/fefly1.cxx:lcl_FindAnchorPos).
                    frame.setPropertyValue("AnchorType", AT_PAGE)
            if frame_name:
                # XNamed::setName is valid only after the frame is anchored (not a Name property).
                frame.setName(frame_name)
            if text:
                with record_changes(doc, False):
                    frame_text = frame.getText()
                    frame_cursor = frame_text.createTextCursor()
                    frame_cursor.gotoStart(False)
                    frame_text.insertString(frame_cursor, text, False)
            return None

        err = insert_at_target(
            self,
            ctx,
            kwargs,
            mutate,
            what="the text frame",
            undo_title="WriterAgent: Insert text frame",
            code="INSERT_FRAME_FAILED",
        )
        if err is not None:
            return err

        inserted_name = frame_name or frame.getName() or ""
        # LibreOffice changes an anchor type the target cannot hold, such as AT_FRAME outside a frame.
        real_anchor_type = anchor_type_index(frame.getPropertyValue("AnchorType"))
        result: dict[str, Any] = {"status": "ok", "frame_name": inserted_name, "anchor_type": real_anchor_type}
        if recorded:
            flag_tracked(result)
        elif replaced_pending:
            flag_untracked(
                result,
                doc,
                _(
                    "This text frame was inserted directly and is not tracked, but the text it replaced is "
                    "a tracked deletion pending the user's review, so that text is still in the document "
                    "(Ctrl+Z undoes both). Do not accept or reject it yourself. Tell the user."
                ),
                replaced_pending=True,
            )
        else:
            flag_untracked(
                result,
                doc,
                _("This text frame was inserted directly and is not tracked (Ctrl+Z undoes it). Tell the user."),
            )
        return result


class FrameDelete(ToolWriterTextFramesBase):
    """Delete a text frame by name."""

    name: str | None = "frame_delete"
    description: str = "Delete a text frame by name (see frame_list). A Word text box is removed together with its shape. A text box inside a group cannot be deleted alone."
    parameters: dict[str, Any] | None = {
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "Name of the text frame (from frame_list)."},
        },
        "required": ["name"],
    }
    is_mutation: bool | None = True

    def execute(self, ctx: Any, **kwargs: Any) -> dict[str, Any]:
        frame_name = kwargs["name"]
        frame = _frame_or_error(self, ctx, frame_name)
        if isinstance(frame, dict):
            return frame

        doc = ctx.doc
        owner = _text_box_owner(doc, frame_name)
        if owner is not None and isinstance(owner[0], str):
            return grouped_text_box_error(self, owner[0])

        def mutate() -> dict[str, Any] | None:
            if owner is not None:
                # SwFmDrawPage::remove (sw/source/core/unocore/unodraw.cxx) disposes the hidden
                # TextFrame and then the shape; removing the frame alone leaves an empty shape behind.
                doc.getDrawPage().remove(owner[1])
            else:
                # An AT_PAGE frame has no anchor range (sw/source/core/unocore/unoframe.cxx:SwXFrame::getSwAnchor),
                # and SwXText::removeTextContent (sw/source/core/unocore/unotext.cxx) only calls dispose().
                frame.dispose()
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

        result: dict[str, Any] = {"status": "ok", "frame_name": frame_name, "deleted": True}
        flag_untracked_delete(result, doc, "text frame")
        return result
