# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Calc shape drawing tools, bridging Draw's implementations."""

from .base import ToolCalcShapeBase
from plugin.draw.shapes import UpsertShape as DrawUpsertShape
from plugin.draw.shapes import DeleteShape as DrawDeleteShape
from plugin.draw.shapes import GetDrawSummary as DrawGetDrawSummary
from plugin.draw.shapes import ConnectShapes as DrawConnectShapes
from plugin.draw.shapes import GroupShapes as DrawGroupShapes


class UpsertShape(DrawUpsertShape, ToolCalcShapeBase):
    name: str | None = "shape_upsert"
    tier: str = "specialized"


class DeleteShape(DrawDeleteShape, ToolCalcShapeBase):
    name: str | None = "shape_delete"


class GetDrawSummary(DrawGetDrawSummary, ToolCalcShapeBase):
    name: str | None = "shape_summary"


class ConnectShapes(DrawConnectShapes, ToolCalcShapeBase):
    name: str | None = "shape_connect"


class GroupShapes(DrawGroupShapes, ToolCalcShapeBase):
    name: str | None = "shape_group"
