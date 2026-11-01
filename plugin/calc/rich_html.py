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

from plugin.framework.errors import ToolExecutionError, best_effort
from plugin.framework.scratch_writer import clear_writer_body
from plugin.framework.uno_context import get_desktop, get_service_manager
from plugin.calc.address_utils import parse_address
from plugin.calc.bridge import CalcBridge
import plugin.writer.format as format_support

log = logging.getLogger("writeragent.calc")

# Not "_blank" or "_default": a shared target collides with testing_runner's
# Windows Hidden-Writer keeper and hung CI. CREATE|GLOBAL = FrameSearchFlag
# 8|55; flags 0 can search instead of creating.
_HTML_WRITER_TARGET = "_wa_calc_html"
_HTML_WRITER_SEARCH_FLAGS = 8 | 55
# Only this module loads _wa_calc_html. Do not enumerate the desktop:
# getComponents can hang on Windows after a paste.
_keeper_loaded = False


def _step(name: str) -> str:
    """log.info plus stderr flush so GHA names the last call on a hang; returns *name* for the error."""
    msg = "insert_cell_html_rich: " + name
    log.info(msg)
    print(msg, file=sys.stderr, flush=True)
    return name


def _controller_get_transferable(controller: Any) -> Any:
    """Return transferable for the current selection (Writer controller)."""
    t = getattr(controller, "getTransferable", None)
    if callable(t):
        return t()
    from plugin.calc.calc_utils import query_interface

    # pyuno needs a UNO type here; the imported IDL class raises "Couldn't convert".
    xs = best_effort(
        lambda: query_interface(controller, "com.sun.star.datatransfer.XTransferableSupplier"),
        "getTransferable via queryInterface failed",
    )
    if xs is not None:
        return xs.getTransferable()
    raise ToolExecutionError("Writer controller does not support getTransferable; cannot paste HTML into cell.")


def _release_clipboard_if_holds(uno_ctx: Any, transferable: Any) -> None:
    """If SystemClipboard still owns *transferable*, replace it before closing the Writer.

    SwXTextView.getTransferable() builds a SwTransferable bound to that Writer
    shell (PrepareForCopy). On Windows the OLE clipboard can keep that owner
    after insertTransferable, and closing the Writer then hangs. Touch the
    clipboard only when it holds this object so a user copy is left alone.
    """
    try:
        # Do not import chatbot for the clipboard; LibrePy ships this module.
        smgr = get_service_manager(uno_ctx)
        if smgr is None:
            return
        clip = smgr.createInstanceWithContext("com.sun.star.datatransfer.clipboard.SystemClipboard", uno_ctx)
        if clip is None:
            return
        contents = clip.getContents()
        # Identity before ==: a thrown UNO == must not skip a real match.
        if contents is not None and (contents is transferable or contents == transferable):
            # None, None drops the previous owner (lostOwnership) without a new OLE source.
            clip.setContents(None, None)
    except Exception:
        return


def insert_cell_html_rich(doc: Any, uno_ctx: Any, cell_address: str, html: str) -> None:
    """Replace one cell's text with rich content parsed from *html* (active sheet).

    *uno_ctx* is the UNO component context (e.g. ``ToolContext.ctx``).

    Imports HTML using the same StarWriter HTML filter as Writer, then pastes
    into the target cell. Images and embedded objects are not supported.
    """
    global _keeper_loaded
    if not (html or "").strip():
        raise ToolExecutionError("HTML content is empty")

    col, row = parse_address(cell_address.strip())
    sheet = CalcBridge(doc).get_active_sheet()
    cell = sheet.getCellByPosition(col, row)

    content = html_std.unescape(html)
    prepared = format_support._ensure_html_linebreaks(content)

    step = "start"
    reused = _keeper_loaded
    temp_doc = None
    transferable: Any = None
    pasted = False
    try:
        desktop = get_desktop(uno_ctx)
        hidden = format_support.create_property_value("Hidden", True)
        # Do not import chatbot.create_hidden_html_writer — this module ships
        # in LibrePy without chatbot.
        step = _step("load hidden Writer")
        temp_doc = desktop.loadComponentFromURL("private:factory/swriter", _HTML_WRITER_TARGET, _HTML_WRITER_SEARCH_FLAGS, (hidden,))
        _keeper_loaded = temp_doc is not None
        # Never close the Windows keeper. After a successful paste we also
        # keep this Writer (see finally) — CREATE|GLOBAL reuses _wa_calc_html.
        if temp_doc is None or not hasattr(temp_doc, "getText"):
            raise ToolExecutionError("Could not create temporary Writer document")

        text = temp_doc.getText()
        # clear_writer_body disposes the template objects and then clears
        # the text, on a fresh load and on the reused keeper, before the
        # HTML is inserted. setString("") leaves a default-template
        # letterhead that is only a table, text frame, or drawing (the body
        # string is already empty). private:factory/swriter honours that
        # template, and a successful paste keeps this Writer (CREATE|GLOBAL
        # reuses _wa_calc_html), so the first rich-HTML paste copies the
        # letterhead into the cell and later pastes append onto it.
        step = _step("clear hidden Writer")
        if not clear_writer_body(temp_doc):
            raise ToolExecutionError("Could not clear the hidden Writer before pasting HTML into the cell.")
        cursor = text.createTextCursor()
        cursor.gotoStart(False)
        step = _step("insert HTML into hidden Writer")
        format_support._insert_starwriter_html_at_cursor(temp_doc, cursor, prepared)

        # Hidden Writer docs must not use getViewCursor() — it can crash the
        # process (no real view). Select the whole body with a text cursor and
        # XSelectionSupplier.select, then getTransferable().
        w_ctrl = temp_doc.getCurrentController()
        sel = text.createTextCursor()
        sel.gotoStart(False)
        sel.gotoEnd(True)
        w_ctrl.select(sel)
        step = _step("get transferable")
        transferable = _controller_get_transferable(w_ctrl)
        # Do not read SystemClipboard before the paste. getContents() attaches
        # the Windows OLE clipboard thread and a later desktop enum can block.
        # Release-if-ours below is the only clipboard touch.

        cell.getText().setString("")

        c_ctrl = doc.getCurrentController()
        step = _step("select cell")
        c_ctrl.select(cell)
        step = _step("insert transferable")
        ins = getattr(c_ctrl, "insertTransferable", None)
        if not callable(ins):
            raise ToolExecutionError("Calc controller does not support insertTransferable.")
        ins(transferable)
        # Drop SystemClipboard's owner before any close when it still holds
        # this Writer's SwTransferable. Do not gc.collect(): a forced GC
        # aborted in the SdrObject destructor after that release.
        step = _step("release clipboard")
        pasted = True
    except ToolExecutionError:
        raise
    except Exception as e:
        log.debug("insert_cell_html_rich failed", exc_info=True)
        raise ToolExecutionError(f"Failed to insert HTML into cell at {step!r}: {e}") from e
    finally:
        if transferable is not None:
            _release_clipboard_if_holds(uno_ctx, transferable)
        # Do not close after a successful paste. close() can return and then
        # desktop.getComponents() blocks while Calc holds the paste. Reuse
        # _wa_calc_html and clear the body on the next call.
        if temp_doc is not None and not reused and not pasted:
            step = _step("close hidden Writer")
            best_effort(lambda: temp_doc.close(True), "temp Writer close failed")
            _keeper_loaded = False
