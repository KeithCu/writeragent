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
    pass


class DeleteShape(DrawDeleteShape, ToolCalcShapeBase):
    pass


class GetDrawSummary(DrawGetDrawSummary, ToolCalcShapeBase):
    pass


class ConnectShapes(DrawConnectShapes, ToolCalcShapeBase):
    pass


class GroupShapes(DrawGroupShapes, ToolCalcShapeBase):
    pass
