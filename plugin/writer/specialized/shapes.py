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

from ..specialized_base import ToolWriterShapeBase
from plugin.draw.shapes import UpsertShape as DrawUpsertShape
from plugin.draw.shapes import DeleteShape as DrawDeleteShape
from plugin.draw.shapes import GetDrawSummary as DrawGetDrawSummary
from plugin.draw.shapes import ConnectShapes as DrawConnectShapes
from plugin.draw.shapes import GroupShapes as DrawGroupShapes
from plugin.draw.shapes import AlignShapes as DrawAlignShapes
from plugin.draw.shapes import DistributeShapes as DrawDistributeShapes
from plugin.draw.shapes import CreateDiagram as DrawCreateDiagram


class UpsertShape(DrawUpsertShape, ToolWriterShapeBase):
    name: str | None = "shape_upsert"
    # Specialized for all document types (use delegate_to_specialized_*_toolset(domain=shapes)).
    # Inherits the union uno_services from the Draw base class; shared tool names require
    # the Writer class to accept every service that shares those names.
    tier: str = "specialized"


class DeleteShape(DrawDeleteShape, ToolWriterShapeBase):
    name: str | None = "shape_delete"


class GetDrawSummary(DrawGetDrawSummary, ToolWriterShapeBase):
    name: str | None = "shape_summary"


class ConnectShapes(DrawConnectShapes, ToolWriterShapeBase):
    name: str | None = "shape_connect"


class GroupShapes(DrawGroupShapes, ToolWriterShapeBase):
    name: str | None = "shape_group"


class AlignShapes(DrawAlignShapes, ToolWriterShapeBase):
    name: str | None = "shape_align"


class DistributeShapes(DrawDistributeShapes, ToolWriterShapeBase):
    name: str | None = "shape_distribute"


class CreateDiagram(DrawCreateDiagram, ToolWriterShapeBase):
    name: str | None = "shape_diagram"


