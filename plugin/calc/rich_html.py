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

import gc
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

# Not "_blank" and not "_default". testing_runner's Windows keeper loads
# Hidden Writer on "_blank" (542 used the same target and 33699746211 still
# hung). Linux UNO probe: a second "_blank" gets a new RuntimeUID (not
# document reuse) but Hidden frame names stay empty, so a Windows
# frame-manager collision on the shared "_blank" target is still the
# remaining hunch. CREATE|GLOBAL = FrameSearchFlag 8|55 — named target
# with flags 0 can search instead of creating.
_HTML_WRITER_TARGET = "_wa_calc_html"
_HTML_WRITER_SEARCH_FLAGS = 8 | 55


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


def _service_manager(uno_ctx: Any) -> Any:
    """ServiceManager from a UNO context. Best-effort; never raises."""
    getter = getattr(uno_ctx, "getServiceManager", None)
    if callable(getter):
        try:
            smgr = getter()
            if smgr is not None:
                return smgr
        except Exception:
            pass
    return getattr(uno_ctx, "ServiceManager", None)


def _system_clipboard(uno_ctx: Any) -> Any:
    """SystemClipboard or None. Must not import chatbot (LibrePy ships this module)."""
    smgr = _service_manager(uno_ctx)
    if smgr is None:
        return None
    try:
        return smgr.createInstanceWithContext(
            "com.sun.star.datatransfer.clipboard.SystemClipboard", uno_ctx
        )
    except Exception:
        return None


def _clipboard_snapshot(uno_ctx: Any) -> str:
    """Best-effort clipboard contents id for hang logs. Never raises."""
    try:
        clip = _system_clipboard(uno_ctx)
        if clip is None:
            return "no_clipboard"
        contents = clip.getContents()
        if contents is None:
            return "empty"
        return "id=%s type=%s" % (id(contents), type(contents).__name__)
    except Exception as exc:
        return "error:%s" % exc


def _release_clipboard_if_holds(uno_ctx: Any, transferable: Any) -> str:
    """If SystemClipboard still owns *transferable*, replace it before the Writer close.

    SwXTextView.getTransferable() builds a SwTransferable bound to that Writer
    shell (PrepareForCopy). On Windows the OLE clipboard thread can keep that
    transferable as owner after insertTransferable. Closing the Writer while it
    is still the OLE owner is the remaining mechanism that matches GHA
    33763078357 / 33731356375: sheet-level Calc calls still return, but the
    next SfxObjectShell call (isReadonly, then getDocumentProperties) blocks
    forever. Only touch the clipboard when it actually holds *our* transferable
    so a normal paste does not clobber an unrelated user copy.
    """
    try:
        clip = _system_clipboard(uno_ctx)
        if clip is None:
            return "no_clipboard"
        contents = clip.getContents()
        if contents is None:
            return "empty"
        same = contents is transferable
        try:
            equal = contents == transferable
        except Exception:
            equal = False
        if not (same or equal):
            return "not_ours contents_id=%s xfer_id=%s" % (id(contents), id(transferable))
        # None, None drops the previous owner (lostOwnership) without a new OLE source.
        clip.setContents(None, None)
        return "released contents_id=%s xfer_id=%s" % (id(contents), id(transferable))
    except Exception as exc:
        return "error:%s" % exc


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
    desktop = None
    close_temp = True
    temp_uid = "-"
    transferable: Any = None
    try:
        desktop = get_desktop(uno_ctx)
        hidden = format_support.create_property_value("Hidden", True)
        # Do not import chatbot.create_hidden_html_writer — this module ships
        # in LibrePy without chatbot.
        _step(
            "insert_cell_html_rich: loadComponentFromURL start "
            "target=%s hidden=True flags=%s" % (_HTML_WRITER_TARGET, _HTML_WRITER_SEARCH_FLAGS)
        )
        writers_before = _desktop_writer_uids(desktop)
        _step(
            "insert_cell_html_rich: writers_open=%s uids=%s"
            % (len(writers_before), writers_before)
        )
        temp_doc = desktop.loadComponentFromURL(
            "private:factory/swriter",
            _HTML_WRITER_TARGET,
            _HTML_WRITER_SEARCH_FLAGS,
            (hidden,),
        )
        temp_uid = _writer_runtime_uid(temp_doc) if temp_doc is not None else "-"
        reused_existing = bool(temp_uid != "-" and temp_uid in writers_before)
        # If Windows still handed back the keeper, do not close it.
        close_temp = not reused_existing
        _step(
            "insert_cell_html_rich: loadComponentFromURL done "
            "target=%s temp_uid=%s reused_existing=%s close_temp=%s"
            % (_HTML_WRITER_TARGET, temp_uid, reused_existing, close_temp)
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
        _step(
            "insert_cell_html_rich: getTransferable done "
            "xfer_id=%s clipboard=%s" % (id(transferable), _clipboard_snapshot(uno_ctx))
        )

        cell.getText().setString("")

        c_ctrl = doc.getCurrentController()
        _step("insert_cell_html_rich: select cell start")
        c_ctrl.select(cell)
        _step("insert_cell_html_rich: select cell done")
        _step("insert_cell_html_rich: insertTransferable start")
        _controller_insert_transferable(c_ctrl, transferable)
        _step(
            "insert_cell_html_rich: insertTransferable done clipboard=%s"
            % _clipboard_snapshot(uno_ctx)
        )
        # Drop SwTransferable *before* close: it holds the Writer shell
        # (SwXTextView.getTransferable / PrepareForCopy). GHA 33763078357 hung
        # the next SfxObjectShell call after this close while the Python proxy
        # was still alive. Release clipboard ownership first if we are the owner.
        _step("insert_cell_html_rich: clipboard release start")
        released = _release_clipboard_if_holds(uno_ctx, transferable)
        transferable = None
        gc.collect()
        _step(
            "insert_cell_html_rich: clipboard release done "
            "released=%s clipboard=%s" % (released, _clipboard_snapshot(uno_ctx))
        )
    except ToolExecutionError:
        raise
    except Exception as e:
        log.debug("insert_cell_html_rich failed", exc_info=True)
        raise ToolExecutionError(f"Failed to insert HTML into cell: {e}") from e
    finally:
        if transferable is not None:
            # Error path: still drop the proxy so close is not racing OLE.
            try:
                _release_clipboard_if_holds(uno_ctx, transferable)
            except Exception:
                pass
            transferable = None
            gc.collect()
        if temp_doc is not None and close_temp:
            try:
                _step("insert_cell_html_rich: close start")
                temp_doc.close(True)
                _step("insert_cell_html_rich: close done")
            except Exception:
                log.debug("temp Writer close failed", exc_info=True)
            leftover: list[str] = []
            if desktop is not None:
                leftover = _desktop_writer_uids(desktop)
            _step(
                "insert_cell_html_rich: writers_after_close=%s uids=%s clipboard=%s"
                % (len(leftover), leftover, _clipboard_snapshot(uno_ctx))
            )
            # close() can return before dispose on Windows. If the RuntimeUID
            # is still on the desktop, an explicit dispose names that leftover.
            if temp_uid != "-" and temp_uid in leftover:
                try:
                    _step("insert_cell_html_rich: dispose start (still open after close)")
                    disposer = getattr(temp_doc, "dispose", None)
                    if callable(disposer):
                        disposer()
                    _step("insert_cell_html_rich: dispose done")
                except Exception:
                    log.debug("temp Writer dispose failed", exc_info=True)
        elif temp_doc is not None:
            _step("insert_cell_html_rich: close skipped reused_existing=True")
