# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Insert matplotlib image payloads on Calc sheets (=PYTHON / chat tool)."""

from __future__ import annotations

import logging
import os
from typing import Any

from plugin.scripting.image_payload import write_image_payload_to_temp

log = logging.getLogger(__name__)


def insert_image_result_on_sheet(ctx: Any, payload: dict[str, Any]) -> None:
    """Write image payload bytes to a temp file and insert as a cell-anchored shape on the active sheet."""
    import uno
    from com.sun.star.awt import Size

    tmp_path = write_image_payload_to_temp(payload)
    file_url = uno.systemPathToFileUrl(os.path.abspath(tmp_path))
    smgr = ctx.ServiceManager
    desktop = smgr.createInstanceWithContext("com.sun.star.frame.Desktop", ctx)
    doc = desktop.getCurrentComponent()
    ctrl = doc.getCurrentController()
    sheet = ctrl.getActiveSheet()
    draw_page = sheet.DrawPage

    shape = doc.createInstance("com.sun.star.drawing.GraphicObjectShape")
    shape.setSize(Size(15000, 10000))
    draw_page.add(shape)
    shape.setPropertyValue("GraphicURL", file_url)

    # Anchor the image to the active cell so it moves/scales with the grid.
    try:
        from plugin.calc.calc_utils import get_cell_geometry

        selection = ctrl.getSelection()
        if selection is not None:
            addr = selection.getRangeAddress()
            cell = sheet.getCellByPosition(addr.StartColumn, addr.StartRow)
            # Bugfix: merged cells report sub-cell geometry via raw cell.Position/Size.
            # Use calc_utils merged-aware geometry so overlays land on the full merged area.
            cell_pos, cell_size = get_cell_geometry(sheet, cell)
            shape.setPropertyValue("Anchor", cell)
            shape.setPropertyValue("ResizeWithCell", True)
            if hasattr(shape, "setPosition"):
                shape.setPosition(cell_pos)
            if hasattr(shape, "setSize"):
                shape.setSize(cell_size)
    except Exception:
        log.debug("insert_image_result_on_sheet: could not anchor to cell", exc_info=True)
