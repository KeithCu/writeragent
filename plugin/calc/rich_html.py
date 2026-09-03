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
import sys
from typing import Any

from plugin.framework.errors import ToolExecutionError
from plugin.framework.uno_context import get_desktop
from plugin.calc.address_utils import parse_address
from plugin.calc.bridge import CalcBridge
import plugin.writer.format as format_support

log = logging.getLogger("writeragent.calc")

# Same target testing_runner uses for the Windows keeper. Do not change it
# here — GHA 33699746211 hung after 542 switched _default → _blank; we do
# not know which UNO call blocked. These lines exist so the next dispatch
# names the last step (file log never reached the Actions log).
_HTML_WRITER_TARGET = "_blank"


def _step(msg: str) -> None:
    """log.info plus stderr flush so GHA names the last call on a hang."""
    log.info(msg)
    print(msg, file=sys.stderr, flush=True)


def _writer_runtime_uid(doc: Any) -> str:
    """Best-effort RuntimeUID so we can say keeper vs new frame without changing load."""
    try:
        uid = getattr(doc, "RuntimeUID", None)
        if uid:
            return str(uid)
    except Exception:
        pass
    return "-"


def _desktop_writer_uids(desktop: Any) -> list[str]:
    """RuntimeUIDs of open Writer docs. Read-only; must not load or close anything."""
    uids: list[str] = []
    try:
        enum = desktop.getComponents().createEnumeration()
    except Exception:
        return uids
    # ``is True``: MagicMock.hasMoreElements() is truthy and would loop.
    n = 0
    while n < 64:
        try:
            if enum.hasMoreElements() is not True:
                break
            component = enum.nextElement()
        except Exception:
            break
        n += 1
        try:
            if component.supportsService("com.sun.star.text.TextDocument"):
                uids.append(_writer_runtime_uid(component))
        except Exception:
            continue
    return uids


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
        hidden = format_support.create_property_value("Hidden", True)
        # "_blank" (same target as testing_runner's Windows keeper) opens a
        # new frame. "_default" can reuse that hidden keeper and deadlock
        # headless Windows (GHA 33667530529 hung in test_insert_cell_html).
        # Do not import chatbot.create_hidden_html_writer — this module ships
        # in LibrePy without chatbot.
        # Print target first so a hang inside getComponents still names _blank.
        _step(
            "insert_cell_html_rich: loadComponentFromURL start "
            "target=%s hidden=True" % _HTML_WRITER_TARGET
        )
        writers_before = _desktop_writer_uids(desktop)
        _step(
            "insert_cell_html_rich: writers_open=%s uids=%s"
            % (len(writers_before), writers_before)
        )
        temp_doc = desktop.loadComponentFromURL(
            "private:factory/swriter", _HTML_WRITER_TARGET, 0, (hidden,)
        )
        temp_uid = _writer_runtime_uid(temp_doc) if temp_doc is not None else "-"
        reused_existing = bool(temp_uid != "-" and temp_uid in writers_before)
        _step(
            "insert_cell_html_rich: loadComponentFromURL done "
            "target=%s temp_uid=%s reused_existing=%s"
            % (_HTML_WRITER_TARGET, temp_uid, reused_existing)
        )
        if temp_doc is None or not hasattr(temp_doc, "getText"):
            raise ToolExecutionError("Could not create temporary Writer document")

        text = temp_doc.getText()
        cursor = text.createTextCursor()
        cursor.gotoStart(False)
        _step("insert_cell_html_rich: HTML insert start")
        format_support._insert_starwriter_html_at_cursor(temp_doc, cursor, prepared, config_svc=config_svc)
        _step("insert_cell_html_rich: HTML insert done")

        # Hidden Writer docs must not use getViewCursor() — it can crash the
        # process (no real view). Select the whole body with a text cursor and
        # XSelectionSupplier.select, then getTransferable().
        w_ctrl = temp_doc.getCurrentController()
        body = temp_doc.getText()
        sel = body.createTextCursor()
        sel.gotoStart(False)
        sel.gotoEnd(True)
        w_ctrl.select(sel)
        _step("insert_cell_html_rich: getTransferable start")
        transferable = _controller_get_transferable(w_ctrl)
        _step("insert_cell_html_rich: getTransferable done")

        cell.getText().setString("")

        c_ctrl = doc.getCurrentController()
        _step("insert_cell_html_rich: select cell start")
        c_ctrl.select(cell)
        _step("insert_cell_html_rich: select cell done")
        _step("insert_cell_html_rich: insertTransferable start")
        _controller_insert_transferable(c_ctrl, transferable)
        _step("insert_cell_html_rich: insertTransferable done")
    except ToolExecutionError:
        raise
    except Exception as e:
        log.debug("insert_cell_html_rich failed", exc_info=True)
        raise ToolExecutionError(f"Failed to insert HTML into cell: {e}") from e
    finally:
        if temp_doc is not None:
            try:
                _step("insert_cell_html_rich: close start")
                temp_doc.close(True)
                _step("insert_cell_html_rich: close done")
            except Exception:
                log.debug("temp Writer close failed", exc_info=True)
