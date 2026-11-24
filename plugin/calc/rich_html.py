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

from plugin.doc.compound_undo import UndoStepOrRollback
from plugin.framework.errors import ToolExecutionError, best_effort, reraise_if_disposed
from plugin.framework.i18n import _
from plugin.framework.scratch_writer import (
    clear_writer_body,
    is_scratch_document,
    new_blank_writer,
)
from plugin.framework.uno_context import get_service_manager
from plugin.calc.bridge import CalcBridge
from plugin.calc.calc_utils import FORMULA, query_interface
import plugin.writer.html_import as html_import

log = logging.getLogger(__name__)

# Kept Writers are never closed, because closing after a paste can hang on Windows:
# close() can return and desktop.getComponents() then blocks while Calc holds the paste,
# so deferred close is not an option.
# They are reused via this list because a named frame target cannot find them:
# LoadEnv::impl_loadContent (framework/source/loadenv/loadenv.cxx) creates an unnamed frame.
# A list is used because SwHTMLParser::Show (sw/source/filter/html/swhtml.cxx) can
# dispatch events and start a nested paste. Main thread only.
# _GUARD_TEXT holds one character in the first paragraph that is never pasted; everything
# after it is deleted on reuse and joins into that clean guard paragraph, whereas
# setString("") keeps the first pasted paragraph's attributes.
_GUARD_TEXT = "x"
_PARAGRAPH_BREAK = 0  # com.sun.star.text.ControlCharacter.PARAGRAPH_BREAK
_idle_writers: list[Any] = []


def _take_hidden_writer(uno_ctx: Any) -> Any:
    while _idle_writers:
        doc = _idle_writers.pop()
        if (
            is_scratch_document(doc)
            and clear_writer_body(doc, keep_chars=len(_GUARD_TEXT))
            and doc.getText().getString() == _GUARD_TEXT
        ):
            return doc
        log.debug("insert_cell_html_rich: dropping an unusable hidden Writer")
    doc = new_blank_writer(uno_ctx)
    if doc is None:
        raise ToolExecutionError("Could not create temporary Writer document")
    doc.getText().setString(_GUARD_TEXT)
    return doc


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
    # pyuno needs a UNO type here; the imported IDL class raises "Couldn't convert".
    xs = best_effort(
        lambda: query_interface(controller, "com.sun.star.datatransfer.XTransferableSupplier"),
        "getTransferable via queryInterface failed",
    )
    if xs is not None:
        return xs.getTransferable()
    raise ToolExecutionError("Writer controller does not support getTransferable; cannot paste HTML into cell.")


def _release_clipboard_if_holds(uno_ctx: Any, transferable: Any) -> None:
    """If SystemClipboard still owns *transferable*, replace it after the paste.

    SwXTextView.getTransferable() builds a SwTransferable bound to that Writer
    shell (PrepareForCopy). The clipboard must not keep this Writer's
    SwTransferable as owner after the paste (on Windows the OLE clipboard can
    retain that owner after insertTransferable). Touch the clipboard only when
    it holds this object so a user copy is left alone.
    """
    try:
        # Do not import chatbot for the clipboard; LibrePy ships this module.
        smgr: Any = get_service_manager(uno_ctx)
        clip = smgr.createInstanceWithContext("com.sun.star.datatransfer.clipboard.SystemClipboard", uno_ctx)
        contents = clip.getContents()
        # Identity before ==: a thrown UNO == must not skip a real match.
        if contents is not None and (contents is transferable or contents == transferable):
            # None, None drops the previous owner (lostOwnership) without a new OLE source.
            clip.setContents(None, None)
    except Exception:
        return


def insert_cell_html_rich(doc: Any, uno_ctx: Any, cell_address: str, html: str) -> str:
    """Replace one cell's content with rich text parsed from *html*.

    *cell_address* may include a sheet prefix (resolved via CalcBridge).

    *uno_ctx* is the UNO component context (e.g. ``ToolContext.ctx``).

    Imports HTML using the same StarWriter HTML filter as Writer, then pastes
    into the target cell. Images and embedded objects are not supported.

    Returns the cell's prior content (formula text when it held a formula,
    otherwise its display string; ``""`` when empty).
    """
    if not (html or "").strip():
        raise ToolExecutionError("HTML content is empty")

    # Cell addresses use the bridge so sheet prefixes resolve (same as other cell tools).
    cell = CalcBridge(doc).get_cell_by_address(cell_address.strip())
    replaced = str(cell.getFormula()) if cell.getType() == FORMULA else str(cell.getString())

    content = html_std.unescape(html)
    prepared = html_import.ensure_html_linebreaks(content)

    step = "start"
    temp_doc = None
    transferable: Any = None
    try:
        step = _step("load hidden Writer")
        temp_doc = _take_hidden_writer(uno_ctx)

        text = temp_doc.getText()
        cursor = text.createTextCursor()
        cursor.gotoEnd(False)
        text.insertControlCharacter(cursor, _PARAGRAPH_BREAK, False)
        step = _step("insert HTML into hidden Writer")
        html_import.insert_html_fragment_at_cursor(cursor, prepared, wrap=False, advance=True)

        # Hidden Writer docs must not use getViewCursor() — it can crash the
        # process (no real view). Select after the guard paragraph with a text
        # cursor and XSelectionSupplier.select, then getTransferable().
        w_ctrl = temp_doc.getCurrentController()
        sel = text.createTextCursor()
        sel.gotoStart(False)
        sel.goRight(len(_GUARD_TEXT) + 1, False)
        sel.gotoEnd(True)
        w_ctrl.select(sel)
        step = _step("get transferable")
        transferable = _controller_get_transferable(w_ctrl)
        # Do not read SystemClipboard before the paste. getContents() attaches
        # the Windows OLE clipboard thread and a later desktop enum can block.
        # Release-if-ours below is the only clipboard touch.

        # Group the clear and the paste into one Ctrl+Z step; if the paste raises,
        # UndoStepOrRollback undoes the clear so the cell keeps its previous text.
        with UndoStepOrRollback(doc, _("WriterAgent: Insert HTML")):
            cell.getText().setString("")

            c_ctrl = doc.getCurrentController()
            step = _step("select cell")
            c_ctrl.select(cell)
            step = _step("insert transferable")
            ins = getattr(c_ctrl, "insertTransferable", None)
            if not callable(ins):
                raise ToolExecutionError("Calc controller does not support insertTransferable.")
            ins(transferable)
        # The clipboard must not keep this Writer's SwTransferable as owner after the paste.
        # Do not gc.collect() here: a forced collection after that release can abort
        # LibreOffice in the SdrObject destructor.
        step = _step("release clipboard")
    except ToolExecutionError:
        raise
    except Exception as e:
        reraise_if_disposed(e)
        log.debug("insert_cell_html_rich failed", exc_info=True)
        raise ToolExecutionError(f"Failed to insert HTML into cell at {step!r}: {e}") from e
    finally:
        if transferable is not None:
            _release_clipboard_if_holds(uno_ctx, transferable)
        if temp_doc is not None:
            _idle_writers.append(temp_doc)
    return replaced
