# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Calc shape tools: the Draw shape tools registered again for Calc.

Each class is ``class X(DrawX, ToolCalcShapeBase): pass``. Draw's class
comes first in the MRO, so name, schema, execute, domain and uno_services
(every document type) are Draw's. The Calc base adds one thing:
``ToolRegistry.register`` keeps the union of ``required_core_tools`` for
tools that share a name, so registering these classes puts Calc's
``get_sheet_summary`` and ``read_cell_range`` into the shape sub-agent's
tool list. The "shapes" row of the Calc delegate catalog comes from
``ToolCalcShapeBase`` in ``base.py``.
"""

from .base import ToolCalcShapeBase
from plugin.draw.shapes import UpsertShape as DrawUpsertShape
from plugin.draw.shapes import DeleteShape as DrawDeleteShape
from plugin.draw.shapes import GetDrawSummary as DrawGetDrawSummary
from plugin.draw.shapes import ConnectShapes as DrawConnectShapes
from plugin.draw.shapes import GroupShapes as DrawGroupShapes
from plugin.draw.shapes import AlignShapes as DrawAlignShapes
from plugin.draw.shapes import DistributeShapes as DrawDistributeShapes
from plugin.draw.shapes import CreateDiagram as DrawCreateDiagram


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


class AlignShapes(DrawAlignShapes, ToolCalcShapeBase):
    pass


class DistributeShapes(DrawDistributeShapes, ToolCalcShapeBase):
    pass


class CreateDiagram(DrawCreateDiagram, ToolCalcShapeBase):
    pass
