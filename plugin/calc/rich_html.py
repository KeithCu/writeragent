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

from plugin.framework.errors import ToolExecutionError, best_effort, uno_probe
from plugin.framework.scratch_writer import clear_writer_body
from plugin.framework.uno_context import get_desktop, get_service_manager
from plugin.calc.address_utils import parse_address
from plugin.calc.bridge import CalcBridge
import plugin.writer.format as format_support

log = logging.getLogger("writeragent.calc")

# Not "_blank" and not "_default". testing_runner's Windows keeper loads
# Hidden Writer on "_blank" (542 used the same target and 33699746211 still
# hung). Linux UNO probe: a second "_blank" gets a new RuntimeUID (not
# document reuse) but Hidden frame names stay empty, so a Windows
# frame-manager collision on the shared "_blank" target is still the
# remaining hunch. CREATE|GLOBAL = FrameSearchFlag 8|55 — named target
# with flags 0 can search instead of creating.
# Low-priority research (B2-A18): a Writer-free HTML->cell insert (parse the
# fragment and write text portions directly) would delete this hidden Writer,
# its clipboard lifecycle, and the template-clear problem. Not planned.
_HTML_WRITER_TARGET = "_wa_calc_html"
_HTML_WRITER_SEARCH_FLAGS = 8 | 55
# Only this module loads _wa_calc_html, so process state says whether the load
# below handed back the keeper. Do not enumerate the desktop (getComponents
# can hang on Windows after a paste): the UID bookkeeping was deleted for that.
_keeper_loaded = False


def _step(name: str) -> str:
    """log.info plus stderr flush so GHA names the last call on a hang; returns *name* for the error."""
    msg = "insert_cell_html_rich: " + name
    log.info(msg)
    print(msg, file=sys.stderr, flush=True)
    return name


def _controller_get_transferable(controller: Any) -> Any:
    """Return transferable for the current selection (Writer controller)."""
    if controller is None:
        raise ToolExecutionError("Controller is None")
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


def _controller_insert_transferable(controller: Any, transferable: Any) -> None:
    if controller is None:
        raise ToolExecutionError("Calc controller is None")
    ins = getattr(controller, "insertTransferable", None)
    if not callable(ins):
        raise ToolExecutionError("Calc controller does not support insertTransferable.")
    ins(transferable)


def _system_clipboard(uno_ctx: Any) -> Any:
    """SystemClipboard or None. Must not import chatbot (LibrePy ships this module)."""
    smgr = get_service_manager(uno_ctx)
    if smgr is None:
        return None
    return uno_probe(lambda: smgr.createInstanceWithContext("com.sun.star.datatransfer.clipboard.SystemClipboard", uno_ctx), None)


def _release_clipboard_if_holds(uno_ctx: Any, transferable: Any) -> str:
    """If SystemClipboard still owns *transferable*, replace it before closing the Writer.

    SwXTextView.getTransferable() builds a SwTransferable bound to that Writer
    shell (PrepareForCopy). On Windows the OLE clipboard thread can keep that
    transferable as owner after insertTransferable. Only touch the clipboard when
    it actually holds *our* transferable so a normal paste does not clobber an
    unrelated user copy. GHA 33771766524: insertTransferable often leaves
    SystemClipboard empty (release returns ``empty``); the hang was closing the
    source Writer while Calc still held the paste — see ``insert_cell_html_rich``.
    """
    try:
        clip = _system_clipboard(uno_ctx)
        if clip is None:
            return "no_clipboard"
        contents = clip.getContents()
        if contents is None:
            return "empty"
        same = contents is transferable
        equal = uno_probe(lambda: contents == transferable, False)
        if not (same or equal):
            return "not_ours contents_id=%s xfer_id=%s" % (id(contents), id(transferable))
        # None, None drops the previous owner (lostOwnership) without a new OLE source.
        clip.setContents(None, None)
        return "released contents_id=%s xfer_id=%s" % (id(contents), id(transferable))
    except Exception as exc:
        return "error:%s" % exc


def insert_cell_html_rich(doc: Any, uno_ctx: Any, cell_address: str, html: str) -> None:
    """Replace one cell's text with rich content parsed from *html* (active sheet).

    *uno_ctx* is the UNO component context (e.g. ``ToolContext.ctx``).

    Imports HTML using the same StarWriter HTML filter as Writer, then pastes
    into the target cell. Images and embedded objects are not supported.
    """
    global _keeper_loaded
    if not (html or "").strip():
        raise ToolExecutionError("HTML content is empty")

    bridge = CalcBridge(doc)
    col, row = parse_address(cell_address.strip())
    sheet = bridge.get_active_sheet()
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
        # Do not snapshot SystemClipboard here. GHA 33771766524: getContents()
        # was empty on every call, but those queries still attach the Windows
        # OLE clipboard thread (CMtaOleClipboard) and the next desktop enum
        # then blocked. Release-if-ours below is the only clipboard touch.

        cell.getText().setString("")

        c_ctrl = doc.getCurrentController()
        step = _step("select cell")
        c_ctrl.select(cell)
        step = _step("insert transferable")
        _controller_insert_transferable(c_ctrl, transferable)
        # SwXTextView.getTransferable builds a SwTransferable bound to this
        # Writer (PrepareForCopy). Keep the #572 rule: if SystemClipboard
        # still owns that object, drop it before any close. Do not gc.collect()
        # — GHA 33771769928 aborted in freeUnoInterfaceProxy → SdrObject dtor
        # → SfxItemPool::unregisterItemSet after #572 forced a full GC.
        step = _step("release clipboard")
        _release_clipboard_if_holds(uno_ctx, transferable)
        transferable = None
        pasted = True
    except ToolExecutionError:
        raise
    except Exception as e:
        log.debug("insert_cell_html_rich failed", exc_info=True)
        raise ToolExecutionError(f"Failed to insert HTML into cell at {step!r}: {e}") from e
    finally:
        if transferable is not None:
            _release_clipboard_if_holds(uno_ctx, transferable)
            transferable = None
        # Windows (GHA 33771766524, 33772063173): after a live paste,
        # temp_doc.close() returns and an immediate writers_after_close enum
        # can still return — but ~450ms later desktop.getComponents() blocks
        # forever while Calc holds the paste. Do not close on success; reuse
        # _wa_calc_html (CREATE|GLOBAL) and clear the body on the next call.
        # Keep #572: release SystemClipboard only when it owns our transferable.
        if temp_doc is not None and not reused and not pasted:
            step = _step("close hidden Writer")
            best_effort(lambda: temp_doc.close(True), "temp Writer close failed")
            _keeper_loaded = False
