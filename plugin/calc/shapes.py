# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Calc shape drawing tools, bridging Draw's implementations."""

import logging
from .base import ToolCalcShapeBase
from plugin.draw.shapes import UpsertShape as DrawUpsertShape
from plugin.draw.shapes import DeleteShape as DrawDeleteShape
from plugin.draw.shapes import GetDrawSummary as DrawGetDrawSummary
from plugin.draw.shapes import ConnectShapes as DrawConnectShapes
from plugin.draw.shapes import GroupShapes as DrawGroupShapes

log = logging.getLogger("writeragent.calc")

_CALC_DRAW_SHAPE_DOCS = [
    "com.sun.star.sheet.SpreadsheetDocument",
    "com.sun.star.drawing.DrawingDocument",
    "com.sun.star.presentation.PresentationDocument",
    "com.sun.star.text.TextDocument"
]

class UpsertShape(DrawUpsertShape, ToolCalcShapeBase):
    name = "upsert_shape"
    uno_services = _CALC_DRAW_SHAPE_DOCS
    tier = "specialized"

class DeleteShape(DrawDeleteShape, ToolCalcShapeBase):
    name = "delete_shape"
    uno_services = _CALC_DRAW_SHAPE_DOCS

class GetDrawSummary(DrawGetDrawSummary, ToolCalcShapeBase):
    name = "get_draw_summary"
    uno_services = _CALC_DRAW_SHAPE_DOCS

class ConnectShapes(DrawConnectShapes, ToolCalcShapeBase):
    name = "shapes_connect"
    uno_services = _CALC_DRAW_SHAPE_DOCS

class GroupShapes(DrawGroupShapes, ToolCalcShapeBase):
    name = "shapes_group"
    uno_services = _CALC_DRAW_SHAPE_DOCS
