# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
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
"""RichTextControl sidebar chat: control lifecycle, layout, streaming, and scroll."""

from __future__ import annotations

import logging
import os
import uno
from typing import TYPE_CHECKING, Any, cast, final

from plugin.framework.errors import best_effort
from plugin.framework.uno_listeners import BaseWindowListener, BaseFocusListener, BaseMouseListener
from plugin.doc.text_helpers import go_right
# rich_text already loads navigation at import.
from plugin.calc.navigation import (
    CELL_LINK_FONT_PREFIX,
    CELL_LINK_FONT_PROPERTY,
    cell_link_registry,
    mark_cell_link_range,
)
from plugin.chatbot.dialogs import set_prop, set_control_text
from plugin.chatbot.rich_text import (
    CHAT_FONT_HEIGHT,
    CHAT_FONT_NAME,
    CHAT_FONT_WEIGHT,
    ChatTheme,
    apply_chat_char_props,
    apply_rich_control_para_margins,
    link_plain_cell_refs,
)
from plugin.framework.uno_context import focus_preserved, process_events_to_idle

if TYPE_CHECKING:
    from collections.abc import Callable, Collection

    from plugin.chatbot.chat_session import ChatSession

log = logging.getLogger(__name__)


def debug_env_active() -> bool:
    """True in dev thread-guard builds and mock-sidebar soffice (``WRITERAGENT_TESTING=1`` via ``is_dev_build()``)."""
    from plugin.framework import thread_guard as tg

    if getattr(tg, "DEV_GUARD", False):
        return True
    from plugin.framework.dev_build import is_dev_build

    return is_dev_build()


_CONTROL_INIT_STARTED: set[int] = set()
_IN_SCROLL_TO_TAIL: set[int] = set()
# Focus trackers by control id. Stick-to-bottom skips SelectAll while the
# transcript has focus. See RichFocusTracker.
_FOCUS_TRACKERS: dict[int, "RichFocusTracker"] = {}
_ACC_ROLE_SCROLL_BAR = 50  # com.sun.star.accessibility.AccessibleRole.SCROLL_BAR
RICH_CONTROL_NAME = "response_rich"
# Dialog units (AppFont) — inset RichTextControl inside the response placeholder so glyphs are not clipped.
RICH_CONTROL_EDGE_INSET = 8
# Very noisy live scroll tracing. Keep available for sidebar scroll investigations,
# but do not emit it by default even when log_level is DEBUG.
RICH_SCROLL_VERBOSE_DEBUG = os.environ.get("WRITERAGENT_RICH_SCROLL_DEBUG") == "1"


def log_rich_scroll(phase: str, *, reason: str | None = None, **extra: Any) -> None:
    """Structured log for RichTextControl viewport scroll diagnostics."""
    if not RICH_SCROLL_VERBOSE_DEBUG:
        return
    parts = [f"[RICH-SCROLL] phase={phase}"]
    if reason:
        parts.append(f"reason={reason}")
    for key, value in extra.items():
        parts.append(f"{key}={value}")
    log.debug(" ".join(parts))


def log_rich_control_context(phase: str, **extra: Any) -> None:
    """Structured INFO log for RichTextControl lifecycle."""
    parts = [f"[RICH-CONTROL] phase={phase}"]
    for key, value in extra.items():
        parts.append(f"{key}={value}")
    log.info(" ".join(parts))


class RichTextChatWidget:
    """Wraps the sidebar RichTextControl: settings, focus preservation, copy from the hidden Writer, caret reveal."""

    ctx: Any
    control: Any
    style_window: Any
    query: Any
    restore_focus: Any
    cell_link_sheet_names: Callable[[], Collection[str]] | None

    def __init__(
        self,
        ctx: Any,
        control: Any,
        style_window: Any = None,
        query: Any = None,
        restore_focus: Any = None,
        *,
        cell_link_sheet_names: Callable[[], Collection[str]] | None = None,
    ) -> None:
        self.ctx = ctx
        self.control = control
        self.style_window = style_window
        self.query = query
        # Closes over this panel's frame session. Stream chunks must not
        # restore whichever Ask field was pinned for the process.
        self.restore_focus = restore_focus
        # Set only for Calc panels; returns the workbook's sheet names so
        # assistant rows get plain references linked at paint time. None
        # (Writer, Draw) paints rows as they are.
        self.cell_link_sheet_names = cell_link_sheet_names
        # Debug-only: test hooks read these through the SNAPSHOT
        # hook to prove streaming appends instead of repainting.
        self._debug_paint_session_count: int = 0
        self._debug_stream_session_count: int = 0
        # What the control shows, so a stream update can append only the new
        # text instead of wiping and refilling the transcript. Rows
        # [0, _formatted_rows) are a formatted paint that ends at control
        # offset _formatted_len. Later rows were appended as plain text while
        # streaming. _shown None means unknown, and the next update repaints.
        self._shown: list[tuple[str, str]] | None = None
        self._formatted_rows: int = 0
        self._formatted_len: int | None = None
        # Greeting row painted above the session rows (load, Clear). It is not
        # a session message, so every paint and diff adds it the same way.
        # The greeting is part of every diff. The first stream update
        # otherwise sees a changed prefix, wipes and refills at send time,
        # and the plain text appended after that clear draws from a stale
        # layout (turn 1 after load drops lines).
        self._greeting: str = ""

    def replace_control(self, new: Any) -> None:
        """Point this widget at a replaced RichText control and clear row caches."""
        if new is self.control:
            return
        if self.control is not None:
            cell_link_registry.clear(self.control)
        self.control = new
        self._forget_formatted_state()

    def clear(self) -> None:
        """Clear the control contents."""
        clear_control(self.control)
        self._set_formatted_state([])

    def _forget_formatted_state(self) -> None:
        self._shown = None
        self._formatted_rows = 0
        self._formatted_len = None

    def _set_formatted_state(self, items: list[tuple[str, str]]) -> None:
        """Record that the control shows *items*, all formatted.

        A control length that cannot be read records "unknown".
        Without that offset there is nowhere to cut the plain tail, so the
        next update falls back to a full repaint.
        """
        length = get_control_text_length(self.control)
        if length is None:
            self._forget_formatted_state()
            return
        self._shown = list(items)
        self._formatted_rows = len(items)
        self._formatted_len = length

    def _append_plain(self, rows: list[tuple[str, str]]) -> None:
        from plugin.chatbot.rich_text_paste import _plain_append_messages

        _plain_append_messages(
            self.control, rows, self.ctx, self.style_window,
            auto_scroll=True, restore_focus=self.restore_focus,
        )

    def reveal_caret(self, reason: str = "widget") -> None:
        """Ask EditView to show its caret (AUTOSCROLL); does not move the caret."""
        reveal_rich_control_caret(self.control, ctx=self.ctx, reason=reason, restore=self.query)

    def append_chunk(self, text: str) -> None:
        """Append a plain text chunk (e.g. streaming tokens) using theme colors."""
        # Text outside the formatted prefix (the Stop banner). Record it as a
        # plain tail row: the next update then cuts the tail at the prefix end
        # and writes it again, instead of wiping and refilling the whole
        # control (same stale-layout case as the greeting above).
        if self._shown is not None:
            self._shown = self._shown + [("assistant", text)]
        append_text_chunk(
            self.control,
            text,
            style_window=self.style_window,
            ctx=self.ctx,
            restore_focus=self.restore_focus,
        )

    def paint_session(self, session: ChatSession, greeting: str | None = None) -> None:
        """Draw the control from ``session.messages``. The hidden doc is that paint.

        Full repaint: load/switch, Stop, Clear, and an unknown control state.
        Streaming goes through ``stream_session`` instead.
        """
        self._debug_paint_session_count += 1
        from plugin.chatbot.rich_text_paste import session_history_items

        if greeting is not None:
            self._greeting = greeting
        if session is None:
            self.clear()
            return
        self._paint_items(session_history_items(session, self._greeting))

    def _with_cell_links(self, rows: list[tuple[str, str]]) -> list[tuple[str, str]]:
        """Auto-link plain cell references in assistant rows for Calc panels."""
        if self.cell_link_sheet_names is None:
            return rows
        names = self.cell_link_sheet_names()
        # Only the painted copy changes; _shown and session history keep the
        # model's text so stream diffs still compare against the list.
        return [(role, link_plain_cell_refs(content, names) if role == "assistant" else content) for role, content in rows]

    def _paint_items(self, items: list[tuple[str, str]]) -> None:
        """Replace the control with a formatted paint of *items*."""
        from plugin.chatbot.rich_text_paste import paint_message_items

        paint_message_items(
            self.ctx,
            self.control,
            self._with_cell_links(items),
            style_window=self.style_window,
            restore=self.query,
            restore_focus=self.restore_focus,
        )
        self._set_formatted_state(items)

    def _formatted_prefix_ok(self, items: list[tuple[str, str]]) -> bool:
        """True when *items* still start with the formatted rows the control shows."""
        shown = self._shown
        k = self._formatted_rows
        return shown is not None and len(items) >= k and items[:k] == shown[:k]

    def _repaint_prefix_then_plain_tail(self, items: list[tuple[str, str]]) -> None:
        """Full paint up to the last user row; this turn's later rows as plain text.

        Runs only when the control no longer matches the list (unknown state,
        or a change inside the formatted prefix). Load, Clear and Stop keep a
        matching prefix, so turn 1 and the turn after Stop do not come here.
        The open assistant row lands in the plain tail, so the next chunks
        take the append path again.
        """
        k = 0
        for idx, (role, _content) in enumerate(items):
            if role == "user":
                k = idx + 1
        if k:
            self._paint_items(items[:k])
        else:
            self.clear()
        tail = items[k:]
        if self._shown is None:
            # The control length could not be read, so there is no offset to
            # cut a plain tail at later. Paint every row instead.
            if tail:
                self._paint_items(items)
            return
        if tail:
            self._append_plain(tail)
        self._shown = list(items)

    def stream_session(self, session: ChatSession) -> None:
        """Show ``session.messages`` during a turn by appending, not repainting.

        Why not ``paint_session`` per batch: a wipe-and-refill about 3 times a
        second built 3 hidden Writer docs a second and left VCL drawing from a
        stale layout (blank transcript, growing gap below the last line).

        The formatted prefix (everything painted before this turn) is left as
        it is. A user row that opens the turn is appended formatted. Assistant
        rows are appended as plain text, and only the new characters are
        written. A row that changed in a way that is not an append (the
        committed message replacing the open row, think text dropped) cuts the
        plain tail at the end of the prefix and writes the tail again as plain
        text. Only a change inside the prefix, or an unknown control, repaints
        everything. ``rerender_last_assistant_if_html`` formats the tail at Ready.
        """
        self._debug_stream_session_count += 1
        from plugin.chatbot.rich_text_paste import (
            OPEN_TRANSCRIPT,
            append_formatted_row,
            session_history_items,
        )

        if session is None:
            self.clear()
            return
        items = session_history_items(session, self._greeting)
        if not self._formatted_prefix_ok(items):
            self._repaint_prefix_then_plain_tail(items)
            return
        k = self._formatted_rows
        old_tail = cast("list[tuple[str, str]]", self._shown)[k:]
        new_tail = items[k:]
        # User rows that open the turn join the formatted prefix. A failed
        # formatted copy rolls itself back; the row then goes in the plain tail.
        while not old_tail and new_tail and new_tail[0][0] == "user":
            if not append_formatted_row(
                self.ctx, self.control, new_tail[0][1], role="user", style_window=self.style_window,
                auto_scroll=True, restore=self.query, restore_focus=self.restore_focus,
            ):
                break
            length = get_control_text_length(self.control)
            if length is None:
                # No offset to cut the tail at any more: full repaint.
                self._paint_items(items)
                return
            k += 1
            self._formatted_rows = k
            self._formatted_len = length
            new_tail = items[k:]
        last = session.messages[-1] if session.messages else None
        open_content = (
            last.get("content")
            if last and last.get(OPEN_TRANSCRIPT)
            else None
        )
        if not self._append_to_plain_tail(old_tail, new_tail, open_content=open_content):
            # Not an append: cut the plain tail at the end of the prefix and
            # write it again. The prefix is not cleared.
            rollback_rich_insert(self.control, self._formatted_len)
            self._append_plain(new_tail)
        self._shown = list(items)

    def _append_to_plain_tail(
        self,
        old_tail: list[tuple[str, str]],
        new_tail: list[tuple[str, str]],
        open_content: str | None = None,
    ) -> bool:
        """Write what *new_tail* adds to the plain rows *old_tail* already shown.

        False, with nothing written, when *new_tail* is not *old_tail* plus
        appended text and rows.
        """
        from plugin.chatbot.rich_text_paste import plain_fallback_text

        m = len(old_tail)
        if m:
            if m > len(new_tail) or old_tail[:-1] != new_tail[: m - 1]:
                return False
            old_role, old_content = old_tail[-1]
            new_role, new_content = new_tail[m - 1]
            if old_role != new_role:
                return False
            is_open = (
                open_content is not None
                and new_content == open_content
            )
            old_s = old_content if is_open else plain_fallback_text(old_content)
            new_s = new_content if is_open else plain_fallback_text(new_content)
            if not new_s.startswith(old_s):
                return False
            if not old_s.strip():
                # _plain_append_messages skips an empty row, so this one was
                # never written. Write it whole now.
                self._append_plain([(new_role, new_content)])
            elif len(new_s) > len(old_s):
                theme = ChatTheme.resolve(style_window=self.style_window)
                append_text_chunk(
                    self.control, new_s[len(old_s):], style_window=self.style_window,
                    ctx=self.ctx,
                    char_color=theme.role_color(new_role),
                    restore_focus=self.restore_focus,
                )
        if len(new_tail) > m:
            self._append_plain(new_tail[m:])
        return True

    def rerender_last_assistant_if_html(self, session: ChatSession) -> bool:
        """Format this turn's rows once the reply is complete.

        True when the session has an assistant row and the control was drawn
        from the list. Callers that still hold a stripper leftover append it
        when this returns False.

        The list is already updated, so draw from the list.
        The cut is the end of the formatted prefix this widget recorded, so only the
        rows streamed as plain text this turn are replaced. When that prefix is
        unknown or no longer matches the list, the whole list is repainted.
        """
        from plugin.chatbot.rich_text_paste import (
            _ensure_message_separator,
            append_formatted_rows,
            session_history_items,
        )

        final_msg = next(
            (msg for msg in reversed(session.messages) if msg.get("role") == "assistant" and msg.get("content")),
            None,
        )
        if not final_msg:
            log.debug("rerender_last_assistant_if_html: no final assistant message — skip")
            return False
        if not str(final_msg.get("content")).strip():
            return False
        items = session_history_items(session, self._greeting)
        if not self._formatted_prefix_ok(items):
            log_rich_scroll("turn_end_full_paint", rows=len(items))
            self.paint_session(session)
            return True
        tail = items[self._formatted_rows:]
        log_rich_scroll("turn_end_tail", rows=len(items), tail_rows=len(tail), cut=self._formatted_len)
        # Cut the plain tail and append the same rows formatted. The
        # transcript above the cut is not wiped and refilled.
        rollback_rich_insert(self.control, self._formatted_len)
        if tail:
            _ensure_message_separator(self.control)
            append_formatted_rows(
                self.ctx, self.control, self._with_cell_links(tail), style_window=self.style_window,
                restore=self.query, restore_focus=self.restore_focus,
            )
        # A long cut (two turns after a Stop: partial answer, banner, the next
        # turn) then a formatted refill leaves the same stale layout as a full
        # repaint: an 80px gap under the reply at Ready. One reformat per turn,
        # outside the stream drain, so it settles before the next send.
        force_rich_full_reformat(self.control)
        _scroll_rich_to_tail(self.control, restore_focus=self.restore_focus)
        self._set_formatted_state(items)
        return True

    def clear_and_greeting(self, greeting: str = "") -> None:
        """Replace the transcript with the greeting. The old HTML is not edited."""
        self._greeting = greeting or ""
        if not greeting:
            self.clear()
            return
        # Recorded as the formatted prefix, so the first send after Clear
        # appends its rows under the greeting instead of repainting.
        self._paint_items([("assistant", greeting)])


def is_automatic_char_color(color: Any) -> bool:
    """True for LO automatic / unset character colors (COL_AUTO)."""
    if not isinstance(color, int):
        return True
    return color < 0 or color == 0xFFFFFFFF


def _content_bounds_for_rich_control(placeholder_ctrl: Any, placeholder_rect: Any = None) -> tuple[int, int, int, int]:
    """Return (x, y, width, height) for the rich control inside the response area."""
    inset = RICH_CONTROL_EDGE_INSET

    if placeholder_rect is not None:
        px, py, pw, ph = placeholder_rect
    else:
        ps = placeholder_ctrl.getPosSize()
        px, py, pw, ph = ps.X, ps.Y, ps.Width, ps.Height

    return (
        px + inset,
        py + inset,
        max(20, pw - 2 * inset),
        max(20, ph - 2 * inset),
    )


def _apply_rich_control_geometry(rich_control: Any, bx: int, by: int, bw: int, bh: int) -> bool:
    """Apply bounds via setPosSize.

    Dialog-embedded controls accept PositionX/Y/Width/Height only at insert time;
    after ``insertByName`` subsequent model writes return -1.
    Resize uses this peer path. Insert-time model geometry stays in
    ``_try_dialog_embedded_rich_control``.
    """
    changed = False
    try:
        cur = rich_control.getPosSize()
        if cur.X != bx or cur.Y != by or cur.Width != bw or cur.Height != bh:
            rich_control.setPosSize(bx, by, bw, bh, 15)
            changed = True
    except Exception as e:
        log.debug("_apply_rich_control_geometry setPosSize: %s", e)
    return changed


def _rich_control_needs_bounds(rich_control: Any, bx: int, by: int, bw: int, bh: int) -> bool:
    cur = rich_control.getPosSize()
    return (
        abs(cur.X - bx) > 2
        or abs(cur.Y - by) > 2
        or abs(cur.Width - bw) > 2
        or abs(cur.Height - bh) > 2
    )


def _invalidate_peer(control: Any) -> None:
    peer = control.getPeer() if hasattr(control, "getPeer") else None
    if peer is not None and hasattr(peer, "invalidate"):
        peer.invalidate(0)


def _model_text(control: Any) -> str:
    return getattr(control.getModel(), "Text", "") or ""


def sync_rich_control_bounds(rich_control: Any, root_window: Any, placeholder_ctrl: Any, placeholder_rect: Any = None, restore_focus: Any = None) -> Any:
    """Position the rich control over the response area without exceeding the button row width."""
    if rich_control is None or placeholder_ctrl is None:
        return rich_control
    try:
        bx, by, bw, bh = _content_bounds_for_rich_control(
            placeholder_ctrl, placeholder_rect=placeholder_rect,
        )
        bounds_changed = _apply_rich_control_geometry(rich_control, bx, by, bw, bh)
        if bounds_changed:
            log_rich_scroll("sync_bounds", rect=f"{bx},{by},{bw},{bh}")
            # Stock RichTextControlImpl::layoutWindow (forms/source/richtext/richtextimplcontrol.cxx)
            # calls SetVisArea(Point()), so every setPosSize jumps to the top; the local patch
            # d3cb23173e07 keeps the old top-left. Stock: Hidden SelectAll, no reveal_caret.
            if _model_text(rich_control):
                _invalidate_peer(rich_control)
                _scroll_rich_to_tail(rich_control, restore_focus=restore_focus)
        needs_bounds = _rich_control_needs_bounds(rich_control, bx, by, bw, bh)
        if needs_bounds:
            # Dialog-embedded: model resize after insert fails (-1); reinsert when transcript empty.
            if not _model_text(rich_control):
                new_ctrl = _try_dialog_embedded_rich_control(root_window, placeholder_ctrl, placeholder_rect)
                if new_ctrl is not None:
                    rich_control = new_ctrl
                    new_ctrl.setVisible(True)
                    log_rich_scroll(
                        "reinsert_dialog_control",
                        w=bw,
                        h=bh,
                        x=bx,
                        y=by,
                    )
            else:
                log.warning(
                    "[RICH-CONTROL] bounds mismatch but transcript non-empty (h=%s want=%s); peer path preferred",
                    rich_control.getPosSize().Height,
                    bh,
                )
        # Gate here so retail skips the getPosSize UNO read.
        if RICH_SCROLL_VERBOSE_DEBUG and placeholder_rect is not None:
            cur = rich_control.getPosSize()
            log_rich_scroll(
                "sync_bounds_final",
                placeholder_rect=placeholder_rect,
                w=cur.Width,
                h=cur.Height,
                x=cur.X,
                y=cur.Y,
            )
        return rich_control
    except Exception as e:
        log.debug("sync_rich_control_bounds failed: %s", e)
        return rich_control


def refresh_rich_control_peer_layout(ctx: Any, rich_control: Any) -> None:
    """Ask VCL to apply bounds already set on the peer (GTK may paint ~2 lines until focus)."""
    if rich_control is None:
        return
    log_rich_scroll("peer_refresh")
    process_events_to_idle(ctx, rounds=2)
    cur = rich_control.getPosSize()
    rich_control.setPosSize(cur.X, cur.Y, cur.Width, cur.Height, 15)
    _invalidate_peer(rich_control)


def force_rich_full_reformat(control: Any) -> None:
    """Make the RichTextControl EditEngine lay out the whole control again.

    After clear_control and a bulk refill, the EditEngine draws a layout
    that disagrees with its own text height. Lines go missing and a gap
    below the last line grows with the transcript, until the sidebar goes
    blank in long sessions. invalidate() and hide/show do not fix it. A
    paper-width change does: resizing the peer reaches
    EditEngine::SetPaperSize (editeng/source/editeng/editeng.cxx), which
    reformats the whole document when the width changes. Narrow by 1px,
    then restore.
    """
    ps = control.getPosSize()
    if ps.Width <= 2:
        return
    try:
        control.setPosSize(ps.X, ps.Y, ps.Width - 1, ps.Height, 4)  # PosSize.WIDTH
    finally:
        control.setPosSize(ps.X, ps.Y, ps.Width, ps.Height, 4)


def _apply_rich_control_style_defaults_on_model(
    model: Any,
    *,
    theme: ChatTheme,
) -> None:
    """Set default character properties on the form TextField model (before peer creation).

    Do not set model TextColor/CharColor here — control-level text color homogenizes every
    run and hides per-message user/assistant CharColor applied at insert time.
    """
    apply_chat_char_props(model, bg_color=theme.bg_color)
    for name, val in (
        ("BackgroundColor", theme.bg_color),
        ("PaintTransparent", False),
        ("RichText", True),
        ("ReadOnly", True),
        ("Tabstop", False),
        ("MultiLine", True),
        ("VScroll", True),
        ("HScroll", False),
        ("AutoHScroll", False),
        ("HardLineBreaks", False),
        # If a 1-char tail selection is left (collapse missed), hide it when
        # Ask/instruct has focus. Harmless if selection is collapsed.
        ("HideInactiveSelection", True),
    ):
        set_prop(model, name, val)

    fd = cast("Any", uno.createUnoStruct("com.sun.star.awt.FontDescriptor"))
    fd.Name = CHAT_FONT_NAME
    fd.Height = int(CHAT_FONT_HEIGHT)
    fd.Weight = int(CHAT_FONT_WEIGHT)
    set_prop(model, "FontDescriptor", fd)


def _apply_rich_control_style_defaults(control: Any, style_window: Any = None) -> None:
    """Set sidebar chat typography on the RichText control at creation (before any content)."""
    model = control.getModel()
    if model is None:
        return
    theme = ChatTheme.resolve(style_window=style_window)
    _apply_rich_control_style_defaults_on_model(model, theme=theme)
    _apply_control_surface_colors(control, theme.bg_color)

    def _apply_cursor() -> None:
        cursor = model.createTextCursor()
        cursor.gotoStart(False)
        text_len = len(model.Text or "")
        if text_len > 0:
            cursor.gotoEnd(True)
        apply_chat_char_props(cursor, bg_color=theme.bg_color)
        apply_rich_control_para_margins(cursor)
        cursor.gotoEnd(False)
        control.setSelection(uno.createUnoStruct("com.sun.star.awt.Selection", text_len, text_len))

    best_effort(_apply_cursor, "_apply_rich_control_style_defaults cursor")


def _apply_control_surface_colors(control: Any, bg_color: int) -> None:
    """Theme background on the VCL control (form model often has no BackgroundColor).

    Do not set TextColor here — control-level text color homogenizes CharColor runs.
    """
    # We stop at the first property that sets, not set both, because
    # BackgroundColor and BackColor are alternative names for one color.
    for name in (
        "BackgroundColor",
        "BackColor",
    ):
        try:
            control.setPropertyValue(name, bg_color)
            return
        except Exception as e:
            log.debug("_apply_control_surface_colors %s failed: %s", name, e)


def _disable_rich_hscroll(control: Any) -> None:
    """Clear WB_HSCROLL on the VCL window so ensureScrollbars disposes m_pHScroll.

    Model HScroll=False is not enough after createPeer: getWinBits already ran.
    ORichTextPeer::setProperty("HScroll") (forms/source/richtext/richtextcontrol.cxx)
    calls adjustTwoStateWinBit + StyleChanged which calls ensureScrollbars().
    """
    model = control.getModel()
    if model is not None:
        for name in ("HScroll", "HardLineBreaks"):
            best_effort(lambda: model.setPropertyValue(name, False), f"_disable_rich_hscroll model.{name}")
    peer = best_effort(lambda: control.getPeer(), "_disable_rich_hscroll getPeer")
    if peer is not None:
        best_effort(lambda: peer.setProperty("HScroll", False), "_disable_rich_hscroll peer.setProperty(HScroll)")


def _create_rich_control_peer(smgr: Any, ctx: Any, toolkit: Any, field_model: Any, parent_window: Any) -> Any | None:
    """Create a VCL peer for a form RichText TextField model."""
    parent_peer = None
    if hasattr(parent_window, "getPeer"):
        parent_peer = parent_window.getPeer()

    for service in (
        "com.sun.star.form.control.RichTextControl",
        "com.sun.star.form.control.TextField",
        "com.sun.star.awt.UnoControlEdit",
    ):
        try:
            control = smgr.createInstanceWithContext(service, ctx)
            if control is None:
                continue
            control.setModel(field_model)
            for label, parent in (("toolkit+peer", parent_peer), ("toolkit+window", parent_window)):
                if parent is None:
                    continue
                try:
                    control.createPeer(toolkit, parent)
                    try:
                        parent_window.addControl(RICH_CONTROL_NAME, control)
                    except Exception as add_err:
                        log.debug("create_sidebar_rich_text_control: addControl failed: %s", add_err)
                    log.info("create_sidebar_rich_text_control: peer via %s (%s)", service, label)
                    _disable_rich_hscroll(control)
                    return control
                except Exception as e:
                    log.debug("create_sidebar_rich_text_control: %s %s failed: %s", service, label, e)
        except Exception as e:
            log.debug("create_sidebar_rich_text_control: %s setup failed: %s", service, e)
    return None


def _try_dialog_embedded_rich_control(root_window: Any, placeholder_ctrl: Any, placeholder_rect: Any = None) -> Any | None:
    """Insert field model via UnoControlDialogModel.createInstance (has PositionX)."""
    try:
        dlg_model = root_window.getModel()
        bx, by, bw, bh = _content_bounds_for_rich_control(
            placeholder_ctrl, placeholder_rect=placeholder_rect,
        )
        embedded = dlg_model.createInstance("com.sun.star.form.component.TextField")
        if embedded is None:
            log.debug("create_sidebar_rich_text_control: dialog createInstance(TextField) returned None")
            return None

        embedded.Name = RICH_CONTROL_NAME
        for prop, val in (
            ("PositionX", bx),
            ("PositionY", by),
            ("Width", bw),
            ("Height", bh),
        ):
            set_prop(embedded, prop, val)

        _apply_rich_control_style_defaults_on_model(embedded, theme=ChatTheme.resolve(style_window=root_window))

        if dlg_model.hasByName(RICH_CONTROL_NAME):
            dlg_model.removeByName(RICH_CONTROL_NAME)
        dlg_model.insertByName(RICH_CONTROL_NAME, embedded)

        control = root_window.getControl(RICH_CONTROL_NAME)
        if control is not None:
            log.info("create_sidebar_rich_text_control: dialog-embedded control via getControl")
            _disable_rich_hscroll(control)
            return control
        log.debug("create_sidebar_rich_text_control: insertByName ok but getControl returned None")
    except Exception as e:
        log.debug("create_sidebar_rich_text_control: dialog-embedded path failed: %s", e)
    return None


class RichTextControlListener(BaseWindowListener):
    """Creates a form TextField with RichText=true over the chat response placeholder.

    Init is triggered two ways: ``try_eager_init()`` right after wiring (required on GNOME),
    and ``on_window_shown`` when the sidebar deck fires ``windowShown`` (typical on KDE).
    Resize is handled by ``_PanelResizeListener`` (``last_response_rect`` + ``sync_rich_control_bounds``).
    """

    ctx: Any
    root_window: Any
    placeholder_ctrl: Any
    on_ready_callback: Any
    _placeholder_rect_fn: Any
    restore_focus: Any
    rich_control: Any
    initialized: bool
    _disposed: bool

    def __init__(self, ctx: Any, root_window: Any, placeholder_ctrl: Any, on_ready_callback: Any, placeholder_rect_fn: Any = None, restore_focus: Any = None) -> None:
        self.ctx = ctx
        self.root_window = root_window
        self.placeholder_ctrl = placeholder_ctrl
        self.on_ready_callback = on_ready_callback
        self._placeholder_rect_fn = placeholder_rect_fn
        self.restore_focus = restore_focus
        self.rich_control = None
        self.initialized = False
        self._disposed = False
        self.on_replaced: Any = None

    def _resolved_placeholder_rect(self) -> Any | None:
        if self._placeholder_rect_fn is not None:
            try:
                return self._placeholder_rect_fn()
            except Exception as e:
                log.debug("RichTextControlListener._resolved_placeholder_rect: %s", e)
        return None

    def _sync_bounds(self) -> None:
        if self._disposed or not self.rich_control or not self.placeholder_ctrl:
            return
        prev_ctrl = self.rich_control
        self.rich_control = sync_rich_control_bounds(
            self.rich_control,
            self.root_window,
            self.placeholder_ctrl,
            placeholder_rect=self._resolved_placeholder_rect(),
            restore_focus=self.restore_focus,
        )
        if self.rich_control is not prev_ctrl and self.on_replaced is not None:
            self.on_replaced(self.rich_control)
        refresh_rich_control_peer_layout(self.ctx, self.rich_control)

    def disposing(self, Source: Any) -> None:
        log_rich_control_context("disposing", initialized=self.initialized, had_control=bool(self.rich_control))
        self._disposed = True
        if self.rich_control is not None:
            cell_link_registry.clear(self.rich_control)
        self.rich_control = None
        # CPython can reuse id() after GC; leaving the parent id in this set
        # makes a later panel skip rich-control init and show the plain field.
        if self.root_window is not None:
            _CONTROL_INIT_STARTED.discard(id(self.root_window))

    def try_eager_init(self) -> None:
        """Create the RichText control as soon as the panel root window has a VCL peer.

        On GNOME (GTK/Wayland sidebar deck), ``ContainerWindowProvider`` often
        never delivers ``windowShown`` for this panel although ``getPeer()``
        is valid when wiring finishes; without this call the plain ``response``
        field stays visible with no error. KDE usually fires ``windowShown``.
        Wiring calls this right after ``addWindowListener`` so both desktops
        take the same path when the peer exists.
        """
        if self._disposed or self.initialized:
            log_rich_control_context(
                "eager_init",
                skipped=1,
                reason="disposed" if self._disposed else "initialized",
            )
            return
        peer = self._root_peer()
        log_rich_control_context("eager_init", peer=int(bool(peer)))
        if peer:
            self._begin_deferred_init()

    def on_window_shown(self, rEvent: Any) -> None:
        """Fallback init when the sidebar deck fires ``windowShown``.

        KDE commonly reaches init here; GNOME often does not emit this event for the chat
        panel (see ``try_eager_init``). Harmless when eager init already ran — guarded by
        ``initialized`` / ``_CONTROL_INIT_STARTED``.
        """
        if self._disposed:
            log_rich_control_context("window_shown", skipped=1, reason="disposed")
            return
        if self.initialized:
            log_rich_control_context("window_shown", skipped=1, reason="initialized")
            return
        peer = self._root_peer()
        log_rich_control_context(
            "window_shown",
            peer=int(bool(peer)),
            initialized=int(self.initialized),
            init_started=int(id(self.root_window) in _CONTROL_INIT_STARTED),
        )
        if not peer:
            log.warning("[RICH-CONTROL] phase=window_shown peer=0 (eager_init should have run at wiring time)")
            return
        self._begin_deferred_init()

    def _root_peer(self) -> Any | None:
        if self.root_window is None or not hasattr(self.root_window, "getPeer"):
            return None
        return self.root_window.getPeer()

    def _begin_deferred_init(self) -> None:
        if self._disposed or self.initialized:
            return
        parent_id = id(self.root_window)
        if parent_id in _CONTROL_INIT_STARTED:
            log.warning("[RICH-CONTROL] phase=begin_init duplicate parent_id=%s", parent_id)
            return
        self.initialized = True
        _CONTROL_INIT_STARTED.add(parent_id)
        log_rich_control_context("deferred_init", action="begin")
        from plugin.framework.queue_executor import main

        main.post(self._deferred_init)

    def _deferred_init(self) -> None:
        if self._disposed:
            return
        try:
            control = create_sidebar_rich_text_control(
                self.ctx,
                self.root_window,
                self.placeholder_ctrl,
                placeholder_rect=self._resolved_placeholder_rect(),
            )
            if not control:
                self.initialized = False
                _CONTROL_INIT_STARTED.discard(id(self.root_window))
                log_rich_control_context("deferred_init", result="control_fail")
                log.error("RichTextControlListener: failed to create sidebar RichText control")

                if self.placeholder_ctrl:
                    set_control_text(
                        self.placeholder_ctrl,
                        "[RichTextControl init failed — see writeragent_debug.log]\n",
                    )
                return
            self.rich_control = control
            self._sync_bounds()
            visible = control.isVisible() if hasattr(control, "isVisible") else "?"
            pos = control.getPosSize()
            bounds = "%sx%s@%s,%s" % (pos.Width, pos.Height, pos.X, pos.Y)
            log_rich_control_context(
                "deferred_init",
                result="control_ok",
                visible=visible,
                bounds=bounds,
            )
            self.on_ready_callback(self.rich_control)
        except Exception:
            self.initialized = False
            _CONTROL_INIT_STARTED.discard(id(self.root_window))
            log_rich_control_context("deferred_init", result="exception")
            log.exception("RichTextControlListener deferred init failed")


def create_sidebar_rich_text_control(ctx: Any, root_window: Any, placeholder_ctrl: Any, placeholder_rect: Any = None) -> Any | None:
    """Create a form RichText TextField peer positioned over the response placeholder."""
    try:
        smgr = ctx.getServiceManager()
        toolkit = smgr.createInstanceWithContext("com.sun.star.awt.Toolkit", ctx)
        if toolkit is None:
            log.error("create_sidebar_rich_text_control: toolkit unavailable")
            return None

        ps = placeholder_ctrl.getPosSize()
        log.info(
            "create_sidebar_rich_text_control: placeholder at x=%s y=%s w=%s h=%s layout_rect=%s",
            ps.X,
            ps.Y,
            ps.Width,
            ps.Height,
            placeholder_rect,
        )

        field_model = smgr.createInstanceWithContext("com.sun.star.form.component.TextField", ctx)
        if field_model is None:
            log.error("create_sidebar_rich_text_control: could not create TextField model")
            return None

        impl = field_model.getImplementationName() if hasattr(field_model, "getImplementationName") else "?"
        set_prop(field_model, "Name", RICH_CONTROL_NAME)
        _apply_rich_control_style_defaults_on_model(field_model, theme=ChatTheme.resolve(style_window=root_window))

        rich_flag = field_model.getPropertyValue("RichText") if hasattr(field_model, "getPropertyValue") else None
        log.info(
            "create_sidebar_rich_text_control: field model impl=%s RichText=%s",
            impl,
            rich_flag,
        )

        bx, by, bw, bh = _content_bounds_for_rich_control(
            placeholder_ctrl, placeholder_rect=placeholder_rect,
        )

        # Peer controls resize via setPosSize on later relayout; dialog-embedded only at insert.
        control = _create_rich_control_peer(smgr, ctx, toolkit, field_model, root_window)
        if control is None:
            control = _try_dialog_embedded_rich_control(root_window, placeholder_ctrl, placeholder_rect)
        if control is None:
            log.error("create_sidebar_rich_text_control: could not create RichText control peer")
            return None

        _apply_rich_control_geometry(control, bx, by, bw, bh)
        control.setVisible(True)
        _apply_rich_control_style_defaults(control, style_window=root_window)
        cur = control.getPosSize()
        log.info(
            "create_sidebar_rich_text_control: ready visible=%s bounds=%dx%d@%d,%d",
            control.isVisible() if hasattr(control, "isVisible") else "?",
            cur.Width,
            cur.Height,
            cur.X,
            cur.Y,
        )
        return control
    except Exception:
        log.exception("create_sidebar_rich_text_control failed")
        return None


def _follows_cell_link(model: Any, pos: Any) -> bool:
    """True when the character just before *pos* is a tagged cell link."""

    def _check() -> bool:
        sel = model.createTextCursor()
        sel.gotoRange(pos, False)
        if not sel.goLeft(1, True):
            return False
        value = sel.getPropertyValue(CELL_LINK_FONT_PROPERTY)
        return isinstance(value, str) and value.startswith(CELL_LINK_FONT_PREFIX)

    return best_effort(_check, "_follows_cell_link", False)


def _insert_string_at_rich_cursor(
    model: Any, cursor: Any, text: str, char_color: Any = None, *, bold: bool | None = None, underline: bool | None = None
) -> None:
    """Insert *text* at *cursor* on a form RichText model.

    *bold* / *underline* are tri-state. True paints the inserted range after
    insert, False forces normal on that range, None leaves weight/underline
    alone (HTML portion copy sets them on *cursor* first). Do not set
    CharWeight/CharUnderline on *cursor* before insert: EditEngine treats
    those as sticky insert attributes, so a table header would paint every
    following row.
    """
    if not text:
        return
    start = cursor.getStart()
    follows_link = _follows_cell_link(model, start)
    if not is_automatic_char_color(char_color):
        cursor.CharColor = char_color

    inserted = False
    insert = getattr(model, "insertString", None)
    if callable(insert):
        insert(cursor, text, False)
        inserted = True
    elif callable(getattr(model, "getText", None)):
        text_obj = model.getText()
        insert_fn = getattr(text_obj, "insertString", None)
        if callable(insert_fn):
            insert_fn(cursor, text, False)
            inserted = True
    if not inserted:
        # pyuno needs a UNO type here; the imported IDL class raises "Couldn't convert".
        xtext = model.queryInterface(uno.getTypeByName("com.sun.star.text.XText"))
        if xtext is not None:
            xtext.insertString(cursor, text, False)
            inserted = True
    if not inserted:
        raise RuntimeError("RichTextControl model has no insertString")

    # Post-insert pass: RichTextControl often ignores pre-insert cursor Char* props.
    if not is_automatic_char_color(char_color) or bold is not None or underline is not None:
        end = cursor.getStart()

        def _apply() -> None:
            sel = model.createTextCursor()
            sel.gotoRange(start, False)
            sel.gotoRange(end, True)
            if not is_automatic_char_color(char_color):
                sel.CharColor = char_color
            if bold is True:
                sel.CharWeight = 150.0
            elif bold is False:
                sel.CharWeight = CHAT_FONT_WEIGHT
            if underline is True:
                sel.CharUnderline = 1
            elif underline is False:
                sel.CharUnderline = 0

        best_effort(_apply, "_insert_string_at_rich_cursor")
    if follows_link:
        # EditEngine stretches a character attribute that ends at the insert
        # point over the new text, so a cell link tag would spread past the link.
        end_pos = cursor.getStart()

        def _untag() -> None:
            sel = model.createTextCursor()
            sel.gotoRange(start, False)
            sel.gotoRange(end_pos, True)
            mark_cell_link_range(sel, None)

        best_effort(_untag, "_insert_string_at_rich_cursor: clear cell link tag")
    if bold is not None or underline is not None:
        cursor.CharWeight = CHAT_FONT_WEIGHT
        cursor.CharUnderline = 0
    cursor.gotoEnd(False)


def _dispatch_rich_uno(control: Any, command: str) -> bool:
    """queryDispatch *command* on the rich peer, never the Writer frame."""
    try:
        peer = control.getPeer() if hasattr(control, "getPeer") else None
        if peer is None or not hasattr(peer, "queryDispatch"):
            return False
        # We set only URL.Complete, not URLTransformer.parseStrict, because
        # ORichTextPeer::queryDispatch and OSelectAllDispatcher (forms/source/richtext/richtextcontrol.cxx)
        # match on URL.Complete.
        url = cast("Any", uno.createUnoStruct("com.sun.star.util.URL"))
        url.Complete = command
        disp = peer.queryDispatch(url, "", 0)
        if disp is None:
            return False
        disp.dispatch(url, ())
        log_rich_scroll("dispatch", command=command)
        return True
    except Exception as e:
        log.debug("_dispatch_rich_uno %s: %s", command, e)
        return False


def _scroll_rich_to_tail(control: Any, restore_focus: Callable[[], None] | None = None) -> None:
    """SelectAll for stick-to-bottom, then collapse it, keeping EESelectionMode::Hidden.

    OSelectAllDispatcher: EditView.SetSelection(All()) then ShowCursor, which
    scrolls to the end. DrawSelectionXOR is a no-op while Hidden (impedit.cxx).
    Restore query first so the viewport is Hidden, then SelectAll, then restore
    again. Do not setFocus or reveal_caret around this (GetFocus + Std + All()
    is the flash).

    The All() selection must not outlive this call. A press inside a selection
    is a drag-and-drop start in the SelectionEngine, so the user's first click
    or drag in the transcript does nothing (or drags the whole transcript)
    instead of starting a selection, and any later GetFocus paints it.
    ``_select_all_collapsed`` collapses it without focus.

    Skipped while the transcript has focus: the user is clicking, selecting or
    copying there. SelectAll would replace their selection, and scrolling
    under the mouse is wrong anyway. Stick-to-bottom resumes on the next call
    after focus leaves (Ask, document). This also keeps SelectAll off the
    focused Std-mode path (DrawSelectionXOR -> OutputDevice::Invert).

    Re-entrant no-op: setPosSize during a sidebar drag can nest through
    layoutWindow into another restick, which hangs.
    Do not process_events_to_idle here.
    """
    if control is None:
        return
    ctrl_id = id(control)
    if ctrl_id in _IN_SCROLL_TO_TAIL:
        return
    tracker = _FOCUS_TRACKERS.get(ctrl_id)
    if tracker is not None and tracker.has_focus:
        log_rich_scroll("skip_select_all", reason="transcript_focused")
        return
    _IN_SCROLL_TO_TAIL.add(ctrl_id)
    try:
        if restore_focus is not None:
            restore_focus()
        _select_all_collapsed(control)
        if restore_focus is not None:
            restore_focus()
    finally:
        _IN_SCROLL_TO_TAIL.discard(ctrl_id)


def _select_all_collapsed(control: Any) -> bool:
    """Dispatch SelectAll for its scroll, then collapse the selection with no focus.

    There is no UNO selection setter (the peer is a VCLXWindow, not an
    XTextComponent), and keys only reach the focus window. EditEngine does
    collapse a view selection whose end paragraph is deleted
    (ImpEditEngine::UpdateSelections, editeng/source/editeng/impedit2.cxx:
    caret at the start of the paragraph now at that position). So: append an
    empty paragraph with a tiny fixed line height, SelectAll (the end PaM lands
    in it; ShowCursor scrolls to the end), then remove the paragraph break.
    Net text is unchanged. The caret ends at the start of the last real
    paragraph, where the view already is.
    """
    model = control.getModel() if hasattr(control, "getModel") else None
    padded = _append_scroll_pad(model)
    try:
        return _dispatch_rich_uno(control, ".uno:SelectAll")
    finally:
        if padded:
            _remove_scroll_pad(model)


def _append_scroll_pad(model: Any) -> bool:
    """Append an empty paragraph about 0 px high at the end of *model*."""
    if model is None or not hasattr(model, "createTextCursor"):
        return False
    try:
        cursor = model.createTextCursor()
        cursor.gotoEnd(False)
        brk = uno.getConstantByName("com.sun.star.text.ControlCharacter.PARAGRAPH_BREAK")
        model.insertControlCharacter(cursor, brk, False)
    except Exception as e:
        log.debug("_append_scroll_pad: %s", e)
        return False

    def _flatten() -> None:
        # Paragraph attributes only: a char attribute here would become an
        # empty attribute at the end of the last paragraph after the merge
        # and stick to the next streamed text.
        pad = model.createTextCursor()
        pad.gotoEnd(False)
        spacing = cast("Any", uno.createUnoStruct("com.sun.star.style.LineSpacing"))
        spacing.Mode = uno.getConstantByName("com.sun.star.style.LineSpacingMode.FIX")
        spacing.Height = 1
        pad.setPropertyValue("ParaLineSpacing", spacing)
        pad.setPropertyValue("ParaTopMargin", 0)
        pad.setPropertyValue("ParaBottomMargin", 0)

    best_effort(_flatten, "_append_scroll_pad line height")
    return True


def _remove_scroll_pad(model: Any) -> None:
    """Remove the paragraph break added by ``_append_scroll_pad``."""
    try:
        cursor = model.createTextCursor()
        cursor.gotoEnd(False)
        cursor.goLeft(1, True)
        cursor.setString("")
    except Exception:
        log.exception("_remove_scroll_pad failed")


def _rich_vscroll_accessible(control: Any) -> Any | None:
    """XAccessibleValue of the transcript's vertical scroll bar, or None."""

    def _find(obj: Any, depth: int) -> Any | None:
        ctx = obj.getAccessibleContext() if hasattr(obj, "getAccessibleContext") else None
        if ctx is None:
            return None
        if ctx.getAccessibleRole() == _ACC_ROLE_SCROLL_BAR and hasattr(ctx, "setCurrentValue"):
            return ctx
        if depth >= 6:
            return None
        for i in range(ctx.getAccessibleChildCount()):
            found = _find(ctx.getAccessibleChild(i), depth + 1)
            if found is not None:
                return found
        return None

    try:
        return _find(control, 0)
    except Exception as e:
        log.debug("_rich_vscroll_accessible: %s", e)
        return None


def collapse_rich_selection_keep_view(control: Any) -> None:
    """Collapse the transcript selection without moving the view.

    Called when the transcript loses focus. HideInactiveSelection hides the
    user's selection, but EditView keeps it, so the next press inside it is a
    drag-and-drop start and the first click does not start a new selection.
    ``_select_all_collapsed`` scrolls to the end (SelectAll ShowCursor), so put
    the scroll bar back through its accessible value (VCLXScrollBar::setValue
    scrolls the view). The PRIMARY selection keeps the old text.
    """
    if control is None:
        return
    ctrl_id = id(control)
    if ctrl_id in _IN_SCROLL_TO_TAIL:
        return
    _IN_SCROLL_TO_TAIL.add(ctrl_id)
    try:
        bar = _rich_vscroll_accessible(control)
        value = bar.getCurrentValue() if bar is not None else None
        _select_all_collapsed(control)
        if bar is not None and value is not None:
            bar.setCurrentValue(value)
        log_rich_scroll("collapse_stale_selection", restored=value)
    finally:
        _IN_SCROLL_TO_TAIL.discard(ctrl_id)


@final
class RichFocusTracker:
    """Track whether the transcript viewport has focus.

    ``_scroll_rich_to_tail`` skips SelectAll while it does. *on_press* runs on
    mousePressed (the sidebar passes the frame session's
    ``note_user_left_query`` so stream chunks stop pulling focus back to Ask
    while the user selects in the transcript).
    """

    def __init__(self, control: Any, on_press: Callable[[], None] | None = None) -> None:
        self.control: Any = control
        self.on_press: Callable[[], None] | None = on_press
        self.has_focus: bool = False

    def focus_gained(self) -> None:
        self.has_focus = True

    def focus_lost(self) -> None:
        """Collapse the user's selection, now hidden, so the next press starts a new one."""
        self.has_focus = False
        best_effort(lambda: collapse_rich_selection_keep_view(self.control), "rich collapse on focus lost")

    def mouse_pressed(self) -> None:
        if self.on_press is not None:
            best_effort(self.on_press, "rich transcript press")

    def disposed(self) -> None:
        self.has_focus = False
        if _FOCUS_TRACKERS.get(id(self.control)) is self:
            del _FOCUS_TRACKERS[id(self.control)]


def attach_rich_focus_tracker(control: Any, on_press: Callable[[], None] | None = None) -> RichFocusTracker | None:
    """Register focus/mouse listeners on the transcript. See RichFocusTracker."""
    if control is None:
        return None
    if not hasattr(control, "addFocusListener") or not hasattr(control, "addMouseListener"):
        return None
    existing = _FOCUS_TRACKERS.get(id(control))
    if existing is not None and existing.control is control:
        return existing

    state = RichFocusTracker(control, on_press)

    class _Focus(BaseFocusListener):
        def on_disposing(self, Source: Any) -> None:  # noqa: N803 -- UNO signature
            state.disposed()

        def on_focus_gained(self, e: Any) -> None:
            state.focus_gained()

        def on_focus_lost(self, e: Any) -> None:
            state.focus_lost()

    class _Mouse(BaseMouseListener):
        def on_mouse_pressed(self, e: Any) -> None:
            state.mouse_pressed()

    try:
        control.addFocusListener(_Focus())
        control.addMouseListener(_Mouse())
    except Exception as e:
        log.debug("attach_rich_focus_tracker failed: %s", e)
        return None
    _FOCUS_TRACKERS[id(control)] = state
    return state


def append_text_chunk(control: Any, text: str, style_window: Any = None, ctx: Any = None, char_color: int | None = None, restore_focus: Any = None) -> None:
    """Append plain text during assistant streaming with theme assistant color.

    *char_color* overrides the assistant tint for the plain history fallback,
    which paints user and assistant rows in their own theme colors.
    """
    if not control or not text:
        return
    log_rich_scroll("append_chunk", chunk_len=len(text))

    try:
        theme = ChatTheme.resolve(style_window=style_window)
        model = control.getModel()
        if model is None or not hasattr(model, "createTextCursor"):
            return
        # Do not setFocus on stream. setFocus + focus_preserved(query) steals
        # document typing: stock Toolkit has no getFocusWindow
        # (PyUNO hasattr lies). SelectAll on the rich peer scrolls without stealing focus.
        cursor = model.createTextCursor()
        cursor.gotoEnd(False)
        apply_rich_control_para_margins(cursor)
        cursor.CharBackColor = theme.bg_color
        color = theme.assistant_color if char_color is None else char_color
        _insert_string_at_rich_cursor(model, cursor, text, color)
        # _scroll_rich_to_tail already restores before and after SelectAll.
        # A third restore here repeats the second with nothing in between,
        # and each one is a GrabFocus on this frame.
        # Restore goes through *restore_focus*, which closes over the frame session.
        _scroll_rich_to_tail(control, restore_focus=restore_focus)
        # SelectAll scrolls synchronously (EditView::SetSelection formats via CheckIdleFormatter
        # and calls ShowCursor, editeng/source/editeng/editview.cxx). The pump only brings
        # the repaint forward when no drain owns the loop; under a drain the slice returns to
        # VCL and the paint happens there. A forced pump here would nest an unbounded
        # processEventsToIdle inside the drain slice and Stop cannot unwind it.
        process_events_to_idle(ctx)
    except Exception:
        log.exception("append_text_chunk (rich control) failed")


def clear_control(control: Any) -> None:
    """Clear all text from the rich control."""
    if not control:
        return
    try:
        cell_link_registry.clear(control)
        if hasattr(control, "setText"):
            control.setText("")
        model = control.getModel()
        if model is not None:
            if hasattr(model, "createTextCursor"):
                cursor = model.createTextCursor()
                cursor.gotoStart(False)
                cursor.gotoEnd(True)
                if hasattr(cursor, "setString"):
                    cursor.setString("")
            model.Text = ""
    except Exception:
        log.exception("clear_control failed")


def get_control_text_length(control: Any) -> int | None:
    try:
        model = control.getModel()
        if model is None:
            return None
        return len((model.Text or "").encode("utf-16-le")) // 2
    except Exception:
        # None means the length is unknown. Rollback and cell-link spans
        # treat 0 as the start of the control, so a failed length read must
        # not return 0 or it deletes the transcript.
        log.exception("get_control_text_length failed")
        return None


def rollback_rich_insert(control: Any, before: int | None) -> None:
    """Undo a separator or partial copy that did not finish as a full insert.

    The separator (and any partial copy) must not stay in the control
    when the formatted insert returns false, and cell-link spans recorded
    for that tail must not resolve clicks into deleted text.
    A length of None means the control length could not be read. Truncate
    treats 0 as "delete from the start", so an unknown length must not roll
    back.
    """
    if control is None or before is None:
        return
    try:
        text_len = get_control_text_length(control)
        if text_len is not None and before < text_len:
            model = control.getModel()
            cursor = model.createTextCursor()
            cursor.gotoStart(False)
            go_right(cursor, before, False)
            cursor.gotoEnd(True)
            cursor.setString("")
    except Exception:
        log.exception("rollback_rich_insert failed")
    cell_link_registry.drop_from(control, before)


def reveal_rich_control_caret(control: Any, ctx: Any = None, reason: str = "unspecified", *, restore: Any = None) -> None:
    """Focus the control so EditView ShowCursor can run, then restore the query field.

    Does not insert dummy text. A second UNO insert at the end is the same path
    as the real append and does not move the view caret.
    """
    if not control:
        return
    log.debug("reveal_rich_control_caret reason=%s", reason)
    log_rich_scroll("reveal_caret", reason=reason)

    with focus_preserved(ctx, restore):
        model = control.getModel() if hasattr(control, "getModel") else None
        was_readonly = None
        if model is not None:
            was_readonly = getattr(model, "ReadOnly", None)
            if was_readonly:
                set_prop(model, "ReadOnly", False)
        try:
            if hasattr(control, "setFocus"):
                control.setFocus()
            # Under a drain owner or active pump the pump is skipped; the caret reveal is best-effort.
            process_events_to_idle(ctx)
        except Exception:
            log.exception("reveal_rich_control_caret failed")
        finally:
            if was_readonly:
                set_prop(model, "ReadOnly", True)
