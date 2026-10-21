# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Calc shape drawing tools, bridging Draw's implementations."""

import logging
from .base import ToolCalcShapeBase
from plugin.doc.visual_helpers import SHAPE_TOOL_UNO_SERVICES
from plugin.draw.shapes import UpsertShape as DrawUpsertShape
from plugin.draw.shapes import DeleteShape as DrawDeleteShape
from plugin.draw.shapes import GetDrawSummary as DrawGetDrawSummary
from plugin.draw.shapes import ConnectShapes as DrawConnectShapes
from plugin.draw.shapes import GroupShapes as DrawGroupShapes

log = logging.getLogger("writeragent.calc")

_CALC_DRAW_SHAPE_DOCS = list(SHAPE_TOOL_UNO_SERVICES)


class _CalcShapeTool(ToolCalcShapeBase):
    uno_services: list[str] | None = _CALC_DRAW_SHAPE_DOCS  # type: ignore[assignment]
    doc_types: list[str] | None = ["writer", "calc", "draw", "impress"]


class UpsertShape(DrawUpsertShape, _CalcShapeTool):
    name: str | None = "shape_upsert"
    tier: str = "specialized"


class DeleteShape(DrawDeleteShape, _CalcShapeTool):
    name: str | None = "shape_delete"


class GetDrawSummary(DrawGetDrawSummary, _CalcShapeTool):
    name: str | None = "shape_summary"


class ConnectShapes(DrawConnectShapes, _CalcShapeTool):
    name: str | None = "shape_connect"


class GroupShapes(DrawGroupShapes, _CalcShapeTool):
    name: str | None = "shape_group"
