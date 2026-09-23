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
"""Writer shape drawing tools, bridging Draw's implementations."""

from typing import Any

from ..specialized_base import ToolWriterShapeBase
from plugin.doc.visual_helpers import SHAPE_TOOL_UNO_SERVICES
from plugin.draw.shapes import UpsertShape as DrawUpsertShape
from plugin.draw.shapes import DeleteShape as DrawDeleteShape
from plugin.draw.shapes import GetDrawSummary as DrawGetDrawSummary
from plugin.draw.shapes import ConnectShapes as DrawConnectShapes
from plugin.draw.shapes import GroupShapes as DrawGroupShapes


# 1. Inherit from the Draw tool implementation.
# 2. Inherit from the specialized ToolWriterShapeBase to enforce Writer scoping.
# 3. Union services: same names as Draw tools; include Draw/Impress so registration
# order does not drop support for non-Writer documents.
_WRITER_DRAW_SHAPE_DOCS = list(SHAPE_TOOL_UNO_SERVICES)


class UpsertShape(DrawUpsertShape, ToolWriterShapeBase):
    name: str = "shape_upsert"
    uno_services: list[str] = _WRITER_DRAW_SHAPE_DOCS
    doc_types: list[str] = ["writer", "calc", "draw", "impress"]
    # Specialized for all document types (use delegate_to_specialized_*_toolset(domain=shapes)).
    tier: str = "specialized"


class DeleteShape(DrawDeleteShape, ToolWriterShapeBase):
    name: str = "shape_delete"
    uno_services: list[str] = _WRITER_DRAW_SHAPE_DOCS
    doc_types: list[str] = ["writer", "calc", "draw", "impress"]


class GetDrawSummary(DrawGetDrawSummary, ToolWriterShapeBase):
    name: str = "shape_summary"
    uno_services: list[str] = _WRITER_DRAW_SHAPE_DOCS
    doc_types: list[str] = ["writer", "calc", "draw", "impress"]


class ConnectShapes(DrawConnectShapes, ToolWriterShapeBase):
    name: str = "shape_connect"
    uno_services: list[str] = _WRITER_DRAW_SHAPE_DOCS
    doc_types: list[str] = ["writer", "calc", "draw", "impress"]


class GroupShapes(DrawGroupShapes, ToolWriterShapeBase):
    name: str = "shape_group"
    uno_services: list[str] = _WRITER_DRAW_SHAPE_DOCS
    doc_types: list[str] = ["writer", "calc", "draw", "impress"]


def replace_text_in_shape(shape: Any, old: str, new: str):
    """Replace the first occurrence of *old* with *new* inside a drawing shape's own text,
    preserving the formatting of the surrounding text via a text cursor. Returns True on success.

    Uses character-offset navigation (goRight) which aligns with getString() for single-paragraph
    shape text (the common case for callouts / text boxes); fully defensive on any UNO failure."""
    try:
        xtext = shape.getText()
        s = xtext.getString()
    except Exception:
        return False
    idx = s.find(old)
    if idx < 0:
        return False
    # goRight counts UTF-16 code units, while str offsets count code points. Convert so astral
    # characters (emoji, rare CJK) in the shape text don't misalign the selection / corrupt text.
    left_units = len(s[:idx].encode("utf-16-le")) // 2
    old_units = len(old.encode("utf-16-le")) // 2
    if old_units <= 0:
        return False
    try:
        cursor = xtext.createTextCursorByRange(xtext.getStart())
        if left_units:
            cursor.goRight(left_units, False)
        cursor.goRight(old_units, True)
        cursor.setString(new)
        return True
    except Exception:
        return False


