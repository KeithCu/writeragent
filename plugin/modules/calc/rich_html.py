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
"""Import HTML into a Calc cell via a hidden Writer document and transferable paste."""

from __future__ import annotations

import html as html_std
import logging
from typing import Any

from plugin.framework.errors import ToolExecutionError
from plugin.framework.uno_context import get_desktop
from plugin.modules.calc.address_utils import parse_address
from plugin.modules.calc.bridge import CalcBridge
from plugin.modules.writer import format_support

log = logging.getLogger("writeragent.calc")


def _controller_get_transferable(controller: Any) -> Any:
    """Return transferable for the current selection (Writer controller)."""
    if controller is None:
        raise ToolExecutionError("Controller is None")
    t = getattr(controller, "getTransferable", None)
    if callable(t):
        return t()
    try:
        from com.sun.star.datatransfer import XTransferableSupplier

        xs = controller.queryInterface(XTransferableSupplier)
        if xs is not None:
            return xs.getTransferable()
    except Exception:
        log.debug("getTransferable via queryInterface failed", exc_info=True)
    raise ToolExecutionError("Writer controller does not support getTransferable; cannot paste HTML into cell.")


def _controller_insert_transferable(controller: Any, transferable: Any) -> None:
    if controller is None:
        raise ToolExecutionError("Calc controller is None")
    ins = getattr(controller, "insertTransferable", None)
    if not callable(ins):
        raise ToolExecutionError("Calc controller does not support insertTransferable.")
    ins(transferable)


def insert_cell_html_rich(doc: Any, uno_ctx: Any, cell_address: str, html: str, *, config_svc: Any = None) -> None:
    """Replace one cell's text with rich content parsed from *html* (active sheet).

    *uno_ctx* is the UNO component context (e.g. ``ToolContext.ctx``).

    Imports HTML using the same StarWriter HTML filter as Writer, then pastes
    into the target cell. Images and embedded objects are not supported.
    """
    if not (html or "").strip():
        raise ToolExecutionError("HTML content is empty")

    bridge = CalcBridge(doc)
    col, row = parse_address(cell_address.strip())
    sheet = bridge.get_active_sheet()
    cell = bridge.get_cell(sheet, col, row)

    content = html_std.unescape(html)
    prepared = format_support._ensure_html_linebreaks(content)

    temp_doc = None
    try:
        desktop = get_desktop(uno_ctx)
        hidden = format_support._create_property_value("Hidden", True)
        temp_doc = desktop.loadComponentFromURL("private:factory/swriter", "_default", 0, (hidden,))
        if temp_doc is None or not hasattr(temp_doc, "getText"):
            raise ToolExecutionError("Could not create temporary Writer document")

        text = temp_doc.getText()
        cursor = text.createTextCursor()
        cursor.gotoStart(False)
        format_support._insert_starwriter_html_at_cursor(temp_doc, cursor, prepared, config_svc=config_svc)

        # Hidden Writer docs must not use getViewCursor() — it can crash the
        # process (no real view). Select the whole body with a text cursor and
        # XSelectionSupplier.select, then getTransferable().
        w_ctrl = temp_doc.getCurrentController()
        body = temp_doc.getText()
        sel = body.createTextCursor()
        sel.gotoStart(False)
        sel.gotoEnd(True)
        w_ctrl.select(sel)
        transferable = _controller_get_transferable(w_ctrl)

        cell.getText().setString("")

        c_ctrl = doc.getCurrentController()
        c_ctrl.select(cell)
        _controller_insert_transferable(c_ctrl, transferable)
    except ToolExecutionError:
        raise
    except Exception as e:
        log.debug("insert_cell_html_rich failed", exc_info=True)
        raise ToolExecutionError(f"Failed to insert HTML into cell: {e}") from e
    finally:
        if temp_doc is not None:
            try:
                temp_doc.close(True)
            except Exception:
                log.debug("temp Writer close failed", exc_info=True)
