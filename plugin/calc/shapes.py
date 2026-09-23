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

class UpsertShape(DrawUpsertShape, ToolCalcShapeBase):
    name: str | None = "shape_upsert"
    # ToolCalcSpecialBase still has a bare uno_services assign (inferred list).
    uno_services: list | None = _CALC_DRAW_SHAPE_DOCS  # type: ignore[assignment]
    doc_types: list[str] | None = ["writer", "calc", "draw", "impress"]
    tier: str = "specialized"

class DeleteShape(DrawDeleteShape, ToolCalcShapeBase):
    name: str | None = "shape_delete"
    uno_services: list | None = _CALC_DRAW_SHAPE_DOCS  # type: ignore[assignment]
    doc_types: list[str] | None = ["writer", "calc", "draw", "impress"]

class GetDrawSummary(DrawGetDrawSummary, ToolCalcShapeBase):
    name: str | None = "shape_summary"
    uno_services: list | None = _CALC_DRAW_SHAPE_DOCS  # type: ignore[assignment]
    doc_types: list[str] | None = ["writer", "calc", "draw", "impress"]

class ConnectShapes(DrawConnectShapes, ToolCalcShapeBase):
    name: str | None = "shape_connect"
    uno_services: list | None = _CALC_DRAW_SHAPE_DOCS  # type: ignore[assignment]
    doc_types: list[str] | None = ["writer", "calc", "draw", "impress"]

class GroupShapes(DrawGroupShapes, ToolCalcShapeBase):
    name: str | None = "shape_group"
    uno_services: list | None = _CALC_DRAW_SHAPE_DOCS  # type: ignore[assignment]
    doc_types: list[str] | None = ["writer", "calc", "draw", "impress"]
