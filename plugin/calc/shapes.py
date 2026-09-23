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
    name: str = "shape_upsert"  # type: ignore[assignment]  # pyright: ignore[reportIncompatibleVariableOverride]
    uno_services: list[str] = _CALC_DRAW_SHAPE_DOCS
    doc_types: list[str] = ["writer", "calc", "draw", "impress"]  # type: ignore[assignment]  # pyright: ignore[reportIncompatibleVariableOverride]
    tier: str = "specialized"

class DeleteShape(DrawDeleteShape, ToolCalcShapeBase):
    name: str = "shape_delete"  # type: ignore[assignment]  # pyright: ignore[reportIncompatibleVariableOverride]
    uno_services: list[str] = _CALC_DRAW_SHAPE_DOCS
    doc_types: list[str] = ["writer", "calc", "draw", "impress"]  # type: ignore[assignment]  # pyright: ignore[reportIncompatibleVariableOverride]

class GetDrawSummary(DrawGetDrawSummary, ToolCalcShapeBase):
    name: str = "shape_summary"  # type: ignore[assignment]  # pyright: ignore[reportIncompatibleVariableOverride]
    uno_services: list[str] = _CALC_DRAW_SHAPE_DOCS
    doc_types: list[str] = ["writer", "calc", "draw", "impress"]  # type: ignore[assignment]  # pyright: ignore[reportIncompatibleVariableOverride]

class ConnectShapes(DrawConnectShapes, ToolCalcShapeBase):
    name: str = "shape_connect"  # type: ignore[assignment]  # pyright: ignore[reportIncompatibleVariableOverride]
    uno_services: list[str] = _CALC_DRAW_SHAPE_DOCS
    doc_types: list[str] = ["writer", "calc", "draw", "impress"]  # type: ignore[assignment]  # pyright: ignore[reportIncompatibleVariableOverride]

class GroupShapes(DrawGroupShapes, ToolCalcShapeBase):
    name: str = "shape_group"  # type: ignore[assignment]  # pyright: ignore[reportIncompatibleVariableOverride]
    uno_services: list[str] = _CALC_DRAW_SHAPE_DOCS
    doc_types: list[str] = ["writer", "calc", "draw", "impress"]  # type: ignore[assignment]  # pyright: ignore[reportIncompatibleVariableOverride]
