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
import threading
from typing import Any, cast

from plugin.chatbot.listeners import BaseWindowListener
from plugin.chatbot.rich_text import (
    CHAT_FONT_HEIGHT,
    CHAT_FONT_NAME,
    CHAT_FONT_WEIGHT,
    ChatTheme,
    apply_chat_char_props,
    apply_rich_control_para_margins,
    strip_legacy_ai_label,
)
from plugin.framework.uno_context import focus_preserved, process_events_to_idle

log = logging.getLogger(__name__)

_CONTROL_INIT_STARTED: set[int] = set()
_ENV_SNAPSHOT_LOGGED = False
RICH_CONTROL_NAME = "response_rich"
# Dialog units (AppFont) — inset RichTextControl inside the response placeholder so glyphs are not clipped.
RICH_CONTROL_EDGE_INSET = 8
# History reload: batch hidden-Writer + copy cycles to avoid per-message UI repaint.
HISTORY_RENDER_BATCH_CHARS = 16384
_NUDGE_SCROLL_SENTINEL = "\u200b"
# Very noisy live scroll tracing. Keep available for sidebar scroll investigations,
# but do not emit it by default even when log_level is DEBUG.
RICH_SCROLL_VERBOSE_DEBUG = False
_RICH_SCROLL_SEQ = 0


def log_rich_scroll(phase: str, *, control=None, reason: str | None = None, **extra) -> None:
    """Structured log for RichTextControl viewport scroll diagnostics."""
    if not RICH_SCROLL_VERBOSE_DEBUG:
        return
    global _RICH_SCROLL_SEQ
    _RICH_SCROLL_SEQ += 1
    parts = [f"[RICH-SCROLL] seq={_RICH_SCROLL_SEQ} phase={phase}"]
    if reason:
        parts.append(f"reason={reason}")
    if control is not None:
        try:
            parts.append(f"text_len={get_control_text_length(control)}")
        except Exception:
            pass
    parts.append(f"main={int(threading.current_thread() is threading.main_thread())}")
    for key, value in extra.items():
        parts.append(f"{key}={value}")
    log.debug(" ".join(parts))


def log_rich_control_context(ctx, phase: str, **extra) -> None:
    """Structured INFO log for RichTextControl lifecycle (includes one-time DE/env snapshot)."""
    global _ENV_SNAPSHOT_LOGGED
    parts = [f"[RICH-CONTROL] phase={phase}"]
    for key, value in extra.items():
        parts.append(f"{key}={value}")
    if not _ENV_SNAPSHOT_LOGGED:
        _ENV_SNAPSHOT_LOGGED = True
        env_bits: list[str] = []
        for env_key in ("XDG_SESSION_DESKTOP", "XDG_CURRENT_DESKTOP", "WAYLAND_DISPLAY", "DISPLAY", "DESKTOP_SESSION"):
            env_val = os.environ.get(env_key)
            if env_val:
                env_bits.append(f"{env_key.lower()}={env_val}")
        try:
            from plugin.framework.uno_context import get_toolkit

            toolkit = get_toolkit(ctx)
            if toolkit is not None and hasattr(toolkit, "getWorkArea"):
                work_area = toolkit.getWorkArea()
                env_bits.append("work_area=%sx%s" % (int(work_area.Width), int(work_area.Height)))
        except Exception as e:
            log.debug("log_rich_control_context work_area: %s", e)
        if env_bits:
            parts.append("env=" + " ".join(env_bits))
    log.info(" ".join(parts))


class RichTextChatWidget:
    """Widget wrapper that encapsulates a LibreOffice RichTextControl, its settings,

    focus-preservation, text copying from hidden Writer, and viewport scrolling.
    """

    def __init__(self, ctx, control, style_window=None):
        self.ctx = ctx
        self.control = control
        self.style_window = style_window
        self.model = control.getModel() if control else None

    def get_text_length(self) -> int:
        """Get the length of the text currently in the control."""
        return get_control_text_length(self.control)

    def clear(self) -> None:
        """Clear the control contents."""
        clear_control(self.control)

    def truncate(self, start_len: int | None) -> None:
        """Truncate text starting from the specified length index."""
        truncate_control_from(self.control, start_len)

    def nudge_view_to_end(self, reason: str = "widget") -> None:
        """Scroll the control view to the very end."""
        nudge_rich_control_view_to_end(self.control, ctx=self.ctx, style_window=self.style_window, reason=reason)

    def append_chunk(self, text: str, auto_scroll: bool = True) -> None:
        """Append a plain text chunk (e.g. streaming tokens) using theme colors."""
        append_text_chunk(self.control, text, auto_scroll=auto_scroll, style_window=self.style_window, ctx=self.ctx)

    def append_rich_message(
        self,
        text: str,
        role: str = "assistant",
        auto_scroll: bool = True,
        on_after_insert=None,
    ) -> None:
        """Append formatted HTML message via the hidden Writer paste pipeline."""
        from plugin.chatbot.rich_text_paste import append_rich_text_via_clipboard

        append_rich_text_via_clipboard(
            self.ctx,
            self.control,
            text,
            role=role,
            style_window=self.style_window,
            auto_scroll=auto_scroll,
            on_after_insert=on_after_insert,
        )

    def append_rich_messages_batch(
        self,
        items,
        batch_chars: int = HISTORY_RENDER_BATCH_CHARS,
    ) -> None:
        """Append a list of history messages in batches to minimize UI repaint iterations."""
        from plugin.chatbot.rich_text_paste import append_rich_messages_via_clipboard

        append_rich_messages_via_clipboard(
            self.ctx,
            self.control,
            items,
            style_window=self.style_window,
            batch_chars=batch_chars,
        )

    def apply_style_defaults(self) -> None:
        """Apply the standard chat sidebar margins, fonts, and colors to the control."""
        _apply_rich_control_style_defaults(self.control, style_window=self.style_window)

    def rerender_last_assistant_if_html(self, session, stream_start_len: int | None) -> None:
        """Replace the streamed assistant tail with formatted HTML when the message contains tags."""
        from plugin.chatbot.rich_text import _HTML_TAG_RE

        final_msg = None
        for msg in reversed(session.messages):
            if msg.get("role") == "assistant" and msg.get("content"):
                final_msg = msg
                break
        if not final_msg:
            return
        content = final_msg.get("content", "")
        if not _HTML_TAG_RE.search(content):
            return
        self.truncate(stream_start_len)
        # Rescroll after truncate: streamed tail removal shrinks content but leaves
        # the viewport at the pre-truncate position (visible jump upward).
        self.nudge_view_to_end(reason="rerender_truncate")
        self.append_rich_message(content, role="assistant")

    def append_user_message(self, text: str, on_after_insert=None) -> None:
        """Append a formatted user message and optionally record control length after insert."""
        self.append_rich_message(text, role="user", on_after_insert=on_after_insert)

    def append_assistant_stream_chunk(self, text: str, auto_scroll: bool = True) -> bool:
        """Append plain streaming assistant text; return False when legacy AI/status lines are skipped."""
        if skip_legacy_assistant_stream_chunk(text):
            return False
        self.append_chunk(text, auto_scroll=auto_scroll)
        return True

    def clear_and_greeting(self, greeting: str = "") -> None:
        """Clear the transcript and optionally show a formatted greeting."""
        self.clear()
        if greeting:
            self.append_rich_message(greeting, role="assistant")

    def render_session_history(self, session, greeting: str = "") -> None:
        """Reload session messages into the control (batched formatted paste)."""
        from plugin.chatbot.rich_text_paste import session_history_items

        self.clear()
        self.append_rich_messages_batch(session_history_items(session, greeting))


def _is_automatic_char_color(color) -> bool:
    """True for LO automatic / unset character colors (COL_AUTO)."""
    if color is None:
        return True
    if not isinstance(color, int):
        return True
    return color < 0 or color == 0xFFFFFFFF


def _clear_button_right_edge(root_window, fallback: int) -> int:
    """Right edge of the Clear button row (matches ``compute_chat_panel_layout`` content_right)."""
    if root_window is None or not hasattr(root_window, "getControl"):
        return fallback
    try:
        clear = root_window.getControl("clear")
        if clear is not None:
            cr = clear.getPosSize()
            return int(cr.X) + int(cr.Width)
    except Exception as e:
        log.debug("_clear_button_right_edge: %s", e)
    return fallback


def _rich_inner_width(px: int, pw: int, inset: int, root_window) -> int:
    """Inner width capped to the live Clear button row (same edge as query in ``panel_resize``).

    The hidden ``response`` placeholder stretches to the panel right margin; the visible
    RichTextControl must not — it aligns with Send/Stop/Clear like ``query`` and ``status``.
    """
    placeholder_right = px + pw - inset
    content_right = _clear_button_right_edge(root_window, placeholder_right)
    right = min(placeholder_right, content_right)
    return max(20, right - (px + inset))


def _content_bounds_for_rich_control(root_window, placeholder_ctrl, placeholder_rect=None):
    """Return (x, y, width, height) for the rich control inside the response area."""
    inset = RICH_CONTROL_EDGE_INSET

    if placeholder_rect is not None:
        px, py, pw, ph = placeholder_rect
    else:
        ps = placeholder_ctrl.getPosSize()
        px, py, pw, ph = int(ps.X), int(ps.Y), int(ps.Width), int(ps.Height)

    return (
        px + inset,
        py + inset,
        _rich_inner_width(px, pw, inset, root_window),
        max(20, ph - 2 * inset),
    )


def _apply_rich_control_geometry(rich_control, bx, by, bw, bh, *, update_dialog_model=False) -> bool:
    """Apply bounds to a sidebar RichTextControl view (and optionally its dialog model).

    Dialog-embedded controls accept PositionX/Y/Width/Height only at insert time; after
    ``insertByName`` subsequent model writes return -1 (see writeragent_debug.log). Prefer
    the peer creation path for controls that must resize; use ``update_dialog_model`` only
    on first insert.
    """
    changed = False
    if update_dialog_model:
        try:
            model = rich_control.getModel()
            if model is not None:
                for prop, val in (
                    ("PositionX", bx),
                    ("PositionY", by),
                    ("Width", bw),
                    ("Height", bh),
                ):
                    current = getattr(model, prop, None)
                    if current is None or int(current) != int(val):
                        if _set_model_property(model, prop, val):
                            changed = True
        except Exception as e:
            log.debug("_apply_rich_control_geometry model: %s", e)
    try:
        cur = rich_control.getPosSize()
        if int(cur.X) != bx or int(cur.Y) != by or int(cur.Width) != bw or int(cur.Height) != bh:
            rich_control.setPosSize(bx, by, bw, bh, 15)
            changed = True
    except Exception as e:
        log.debug("_apply_rich_control_geometry setPosSize: %s", e)
    return changed


def _rich_control_needs_bounds(rich_control, bx, by, bw, bh, tolerance=2) -> bool:
    try:
        cur = rich_control.getPosSize()
        return (
            abs(int(cur.X) - bx) > tolerance
            or abs(int(cur.Y) - by) > tolerance
            or abs(int(cur.Width) - bw) > tolerance
            or abs(int(cur.Height) - bh) > tolerance
        )
    except Exception:
        return True


def _reinsert_dialog_embedded_rich_control(root_window, placeholder_ctrl, placeholder_rect=None):
    """Remove and recreate dialog-embedded RichTextControl at new bounds (insert-time sizing only)."""
    try:
        dlg_model = root_window.getModel()
        if dlg_model is None or not hasattr(dlg_model, "hasByName"):
            return None
        if dlg_model.hasByName(RICH_CONTROL_NAME):
            dlg_model.removeByName(RICH_CONTROL_NAME)
    except Exception as e:
        log.debug("_reinsert_dialog_embedded_rich_control remove: %s", e)
        return None
    return _try_dialog_embedded_rich_control(root_window, placeholder_ctrl, placeholder_rect)


def sync_rich_control_bounds(rich_control, root_window, placeholder_ctrl, placeholder_rect=None, control_out=None) -> bool:
    """Position the rich control over the response area without exceeding the button row width.

    When ``control_out`` is a one-element list, it may be replaced after dialog reinsert.
    """
    if rich_control is None or placeholder_ctrl is None:
        return False
    try:
        bx, by, bw, bh = _content_bounds_for_rich_control(
            root_window, placeholder_ctrl, placeholder_rect=placeholder_rect,
        )
        bounds_changed = _apply_rich_control_geometry(rich_control, bx, by, bw, bh, update_dialog_model=False)
        if bounds_changed:
            log_rich_scroll("sync_bounds", control=rich_control, rect=f"{bx},{by},{bw},{bh}")
        if _rich_control_needs_bounds(rich_control, bx, by, bw, bh):
            # Dialog-embedded: model resize after insert fails (-1); reinsert when transcript empty.
            try:
                model = rich_control.getModel()
                text = getattr(model, "Text", "") or "" if model is not None else ""
            except Exception:
                text = "?"
            if not text or text == "\u200b":
                new_ctrl = _reinsert_dialog_embedded_rich_control(root_window, placeholder_ctrl, placeholder_rect)
                if new_ctrl is not None:
                    rich_control = new_ctrl
                    if control_out is not None:
                        control_out[0] = new_ctrl
                    new_ctrl.setVisible(True)
                    log.info(
                        "[RICH-CONTROL] reinserted dialog control at %dx%d@%d,%d",
                        bw,
                        bh,
                        bx,
                        by,
                    )
            else:
                log.warning(
                    "[RICH-CONTROL] bounds mismatch but transcript non-empty (h=%s want=%s); peer path preferred",
                    rich_control.getPosSize().Height if hasattr(rich_control, "getPosSize") else "?",
                    bh,
                )
        if placeholder_rect is not None:
            try:
                cur = rich_control.getPosSize()
                log.info(
                    "[RICH-CONTROL] sync bounds placeholder_rect=%s -> %dx%d@%d,%d",
                    placeholder_rect,
                    int(cur.Width),
                    int(cur.Height),
                    int(cur.X),
                    int(cur.Y),
                )
            except Exception:
                log.info(
                    "[RICH-CONTROL] sync bounds applied placeholder_rect=%s -> %dx%d@%d,%d",
                    placeholder_rect,
                    bw,
                    bh,
                    bx,
                    by,
                )
        return True
    except Exception as e:
        log.debug("sync_rich_control_bounds failed: %s", e)
        return False


def refresh_rich_control_peer_layout(ctx, rich_control) -> None:
    """Ask VCL to apply bounds already set on the peer (GTK may paint ~2 lines until focus)."""
    if rich_control is None:
        return
    log_rich_scroll("peer_refresh", control=rich_control)
    try:
        process_events_to_idle(ctx, rounds=2)
    except Exception as e:
        log.debug("refresh_rich_control_peer_layout idle: %s", e)
    try:
        cur = rich_control.getPosSize()
        rich_control.setPosSize(int(cur.X), int(cur.Y), int(cur.Width), int(cur.Height), 15)
    except Exception as e:
        log.debug("refresh_rich_control_peer_layout setPosSize: %s", e)
    try:
        peer = rich_control.getPeer() if hasattr(rich_control, "getPeer") else None
        if peer is not None and hasattr(peer, "invalidate"):
            peer.invalidate(0)
    except Exception as e:
        log.debug("refresh_rich_control_peer_layout invalidate: %s", e)


def _apply_sidebar_para_margins(cursor) -> None:
    """Keep chat text off the RichTextControl edges (EditEngine has no CSS padding)."""
    apply_rich_control_para_margins(cursor)


def _apply_rich_control_style_defaults_on_model(model, style_window=None) -> None:
    """Set default character properties on the form TextField model (before peer creation).

    Do not set model TextColor/CharColor here — control-level text color homogenizes every
    run and hides per-message user/assistant CharColor applied at insert time.
    """
    if model is None:
        return

    theme = ChatTheme.resolve(style_window=style_window)
    for name, val in (
        ("CharFontName", CHAT_FONT_NAME),
        ("CharFontNameAsian", CHAT_FONT_NAME),
        ("CharFontNameComplex", CHAT_FONT_NAME),
        ("CharHeight", CHAT_FONT_HEIGHT),
        ("CharWeight", CHAT_FONT_WEIGHT),
        ("CharPosture", 0),
    ):
        _set_model_property(model, name, val)
    for name, val in (
        ("BackgroundColor", theme.bg_color),
        ("CharBackColor", theme.bg_color),
        ("PaintTransparent", False),
        ("MultiLine", True),
        ("VScroll", True),
    ):
        _set_model_property(model, name, val)
    try:
        import uno

        fd = cast("Any", uno.createUnoStruct("com.sun.star.awt.FontDescriptor"))
        fd.Name = CHAT_FONT_NAME
        fd.Height = int(CHAT_FONT_HEIGHT)
        fd.Weight = int(CHAT_FONT_WEIGHT)
        _set_model_property(model, "FontDescriptor", fd)
    except Exception as e:
        log.debug("_apply_rich_control_style_defaults_on_model FontDescriptor failed: %s", e)


def _apply_rich_control_style_defaults(control, style_window=None):
    """Set sidebar chat typography on the RichText control at creation (before any content)."""
    model = control.getModel() if control is not None else None
    if model is None:
        return
    _apply_rich_control_style_defaults_on_model(model, style_window=style_window)
    theme = ChatTheme.resolve(style_window=style_window)
    _apply_control_surface_colors(control, theme.bg_color)

    if hasattr(model, "createTextCursor"):
        try:
            import uno
            cursor = model.createTextCursor()
            cursor.gotoStart(False)
            text_len = len(model.Text or "")
            if text_len > 0:
                cursor.gotoEnd(True)
            apply_chat_char_props(cursor, bg_color=theme.bg_color)
            _apply_sidebar_para_margins(cursor)
            cursor.gotoEnd(False)
            if control is not None and hasattr(control, "setSelection"):
                control.setSelection(uno.createUnoStruct("com.sun.star.awt.Selection", text_len, text_len))
        except Exception as e:
            log.debug("_apply_rich_control_style_defaults cursor failed: %s", e)
    log.info(
        "_apply_rich_control_style_defaults: font=%s %.1fpt",
        CHAT_FONT_NAME,
        CHAT_FONT_HEIGHT,
    )


def skip_legacy_assistant_stream_chunk(text: str) -> bool:
    """True for plain-sidebar ``AI:`` / status lines that RichTextControl replaces with ``Assistant:``."""
    if not text:
        return False
    stripped = text.strip()
    if stripped in ("AI:", "AI"):
        return True
    if stripped.startswith("AI:") and len(stripped) <= 12:
        return True
    if stripped.startswith("[Using chat model"):
        return True
    return not strip_legacy_ai_label(stripped).strip() and stripped.upper().startswith("AI:")


def _set_model_property(model, name, value) -> bool:
    """Set a control model property (attribute or XPropertySet).

    Form ``TextField`` models reject many awt edit properties (``BackgroundColor``, etc.);
    callers must treat False as unsupported, not fatal.
    """
    try:
        setattr(model, name, value)
        return True
    except Exception:
        pass
    if hasattr(model, "setPropertyValue"):
        try:
            model.setPropertyValue(name, value)
            return True
        except Exception as e:
            log.debug("_set_model_property %s failed: %s", name, e)
    return False


def _apply_control_surface_colors(control, bg_color) -> None:
    """Theme background on the VCL control (form model often has no BackgroundColor).

    Do not set TextColor here — control-level text color homogenizes CharColor runs.
    """
    if control is None:
        return
    for name, val in (
        ("BackgroundColor", bg_color),
        ("BackColor", bg_color),
    ):
        try:
            if hasattr(control, "setPropertyValue"):
                control.setPropertyValue(name, val)
                return
        except Exception as e:
            log.debug("_apply_control_surface_colors %s failed: %s", name, e)
    try:
        model = control.getModel()
        if model is not None and _set_model_property(model, "BackgroundColor", bg_color):
            return
    except Exception as e:
        log.debug("_apply_control_surface_colors model failed: %s", e)


def _create_rich_control_peer(smgr, ctx, toolkit, field_model, parent_window):
    """Create a VCL peer for a form RichText TextField model."""
    parent_peer = None
    if hasattr(parent_window, "getPeer"):
        try:
            parent_peer = parent_window.getPeer()
        except Exception as e:
            log.debug("create_sidebar_rich_text_control: parent getPeer failed: %s", e)

    peer_attempts: list[tuple[str, tuple[Any, Any]]] = []
    if toolkit is not None and parent_peer is not None:
        peer_attempts.append(("toolkit+peer", (toolkit, parent_peer)))
    if toolkit is not None and parent_window is not None:
        peer_attempts.append(("toolkit+window", (toolkit, parent_window)))
    if parent_peer is not None:
        peer_attempts.append(("peer+noid", (parent_peer, 0)))

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
            last_err = None
            for label, args in peer_attempts:
                try:
                    control.createPeer(args[0], args[1])
                    if parent_window is not None and hasattr(parent_window, "addControl"):
                        try:
                            parent_window.addControl(RICH_CONTROL_NAME, control)
                        except Exception as add_err:
                            log.debug("create_sidebar_rich_text_control: addControl failed: %s", add_err)
                    log.info("create_sidebar_rich_text_control: peer via %s (%s)", service, label)
                    return control
                except Exception as e:
                    last_err = e
                    log.debug("create_sidebar_rich_text_control: %s %s failed: %s", service, label, e)
            if last_err is not None:
                log.debug("create_sidebar_rich_text_control: %s all peer attempts failed", service)
        except Exception as e:
            log.debug("create_sidebar_rich_text_control: %s setup failed: %s", service, e)
    return None


def _try_dialog_embedded_rich_control(root_window, placeholder_ctrl, placeholder_rect=None):
    """Insert field model via UnoControlDialogModel.createInstance (has PositionX)."""
    try:
        dlg_model = root_window.getModel()
        if dlg_model is None:
            log.debug("create_sidebar_rich_text_control: dialog-embedded skipped (no model)")
            return None
        if not hasattr(dlg_model, "createInstance"):
            log.debug("create_sidebar_rich_text_control: dialog-embedded skipped (no createInstance)")
            return None

        bx, by, bw, bh = _content_bounds_for_rich_control(
            root_window, placeholder_ctrl, placeholder_rect=placeholder_rect,
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
            ("RichText", True),
            ("ReadOnly", True),
            ("Tabstop", False),
            ("MultiLine", True),
            ("VScroll", True),
        ):
            _set_model_property(embedded, prop, val)

        _apply_rich_control_style_defaults_on_model(embedded, style_window=root_window)

        if dlg_model.hasByName(RICH_CONTROL_NAME):
            dlg_model.removeByName(RICH_CONTROL_NAME)
        dlg_model.insertByName(RICH_CONTROL_NAME, embedded)

        control = root_window.getControl(RICH_CONTROL_NAME)
        if control is not None:
            log.info("create_sidebar_rich_text_control: dialog-embedded control via getControl")
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

    def __init__(self, ctx, root_window, placeholder_ctrl, on_ready_callback, placeholder_rect_fn=None):
        self.ctx = ctx
        self.root_window = root_window
        self.placeholder_ctrl = placeholder_ctrl
        self.on_ready_callback = on_ready_callback
        self._placeholder_rect_fn = placeholder_rect_fn
        self.rich_control = None
        self.initialized = False
        self._disposed = False

    def _resolved_placeholder_rect(self):
        if self._placeholder_rect_fn is not None:
            try:
                return self._placeholder_rect_fn()
            except Exception as e:
                log.debug("RichTextControlListener._resolved_placeholder_rect: %s", e)
        return None

    def _sync_bounds(self) -> None:
        if self._disposed or not self.rich_control or not self.placeholder_ctrl:
            return
        out = [self.rich_control]
        sync_rich_control_bounds(
            self.rich_control,
            self.root_window,
            self.placeholder_ctrl,
            placeholder_rect=self._resolved_placeholder_rect(),
            control_out=out,
        )
        self.rich_control = out[0]
        try:
            refresh_rich_control_peer_layout(self.ctx, self.rich_control)
        except Exception as e:
            log.debug("RichTextControlListener._sync_bounds refresh: %s", e)

    def disposing(self, Source):
        log_rich_control_context(self.ctx, "disposing", initialized=self.initialized, had_control=bool(self.rich_control))
        self._disposed = True
        self.rich_control = None

    def try_eager_init(self) -> None:
        """Create the RichText control as soon as the panel root window has a VCL peer.

        Historically init ran only from ``on_window_shown``. On GNOME (GTK/Wayland sidebar
        deck), ``ContainerWindowProvider`` often never delivers ``windowShown`` for our panel
        even though ``getPeer()`` is already valid when wiring finishes — so the plain
        ``response`` field stayed visible with no error. KDE usually fires ``windowShown``,
        so the listener alone was enough there. Wiring calls this immediately after
        ``addWindowListener`` so both desktops share the same path when the peer exists.
        """
        if self._disposed or self.initialized:
            log_rich_control_context(
                self.ctx,
                "eager_init",
                skipped=1,
                reason="disposed" if self._disposed else "initialized",
            )
            return
        peer = self._root_peer()
        log_rich_control_context(self.ctx, "eager_init", peer=int(bool(peer)))
        if peer:
            self._begin_deferred_init()

    def on_window_shown(self, rEvent):
        """Fallback init when the sidebar deck fires ``windowShown``.

        KDE commonly reaches init here; GNOME often does not emit this event for the chat
        panel (see ``try_eager_init``). Harmless when eager init already ran — guarded by
        ``initialized`` / ``_CONTROL_INIT_STARTED``.
        """
        if self._disposed:
            log_rich_control_context(self.ctx, "window_shown", skipped=1, reason="disposed")
            return
        if self.initialized:
            log_rich_control_context(self.ctx, "window_shown", skipped=1, reason="initialized")
            return
        parent_id = id(self.root_window)
        if parent_id in _CONTROL_INIT_STARTED:
            log.warning(
                "[RICH-CONTROL] phase=window_shown duplicate parent_id=%s init_started=1",
                parent_id,
            )
            return
        peer = self._root_peer()
        log_rich_control_context(
            self.ctx,
            "window_shown",
            peer=int(bool(peer)),
            initialized=int(self.initialized),
            init_started=int(parent_id in _CONTROL_INIT_STARTED),
        )
        if not peer:
            log.warning("[RICH-CONTROL] phase=window_shown peer=0 (eager_init should have run at wiring time)")
            return
        self._begin_deferred_init()

    def _root_peer(self):
        if self.root_window is None or not hasattr(self.root_window, "getPeer"):
            return None
        try:
            return self.root_window.getPeer()
        except Exception as e:
            log.debug("RichTextControlListener._root_peer failed: %s", e)
            return None

    def _begin_deferred_init(self) -> None:
        if self._disposed or self.initialized:
            return
        parent_id = id(self.root_window)
        if parent_id in _CONTROL_INIT_STARTED:
            log.warning("[RICH-CONTROL] phase=begin_init duplicate parent_id=%s", parent_id)
            return
        self.initialized = True
        _CONTROL_INIT_STARTED.add(parent_id)
        log_rich_control_context(self.ctx, "deferred_init", action="begin")
        from plugin.framework.queue_executor import post_to_main_thread

        post_to_main_thread(self._deferred_init)

    def _deferred_init(self):
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
                log_rich_control_context(self.ctx, "deferred_init", result="control_fail")
                log.error("RichTextControlListener: failed to create sidebar RichText control")
                try:
                    from plugin.chatbot.dialogs import set_control_text

                    if self.placeholder_ctrl:
                        set_control_text(
                            self.placeholder_ctrl,
                            "[RichTextControl init failed — see writeragent_debug.log]\n",
                        )
                except Exception:
                    pass
                return
            self.rich_control = control
            self._sync_bounds()
            visible = control.isVisible() if hasattr(control, "isVisible") else "?"
            try:
                pos = control.getPosSize()
                bounds = "%sx%s@%s,%s" % (int(pos.Width), int(pos.Height), int(pos.X), int(pos.Y))
            except Exception:
                bounds = "?"
            log_rich_control_context(
                self.ctx,
                "deferred_init",
                result="control_ok",
                visible=visible,
                bounds=bounds,
            )
            self.on_ready_callback(control)
        except Exception:
            log_rich_control_context(self.ctx, "deferred_init", result="exception")
            log.exception("RichTextControlListener deferred init failed")


def create_sidebar_rich_text_control(ctx, root_window, placeholder_ctrl, placeholder_rect=None):
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
        _set_model_property(field_model, "Name", RICH_CONTROL_NAME)
        _set_model_property(field_model, "RichText", True)
        _set_model_property(field_model, "ReadOnly", True)
        _apply_rich_control_style_defaults_on_model(field_model, root_window)

        rich_flag = None
        try:
            rich_flag = field_model.getPropertyValue("RichText") if hasattr(field_model, "getPropertyValue") else None
        except Exception:
            pass
        log.info(
            "create_sidebar_rich_text_control: field model impl=%s RichText=%s",
            impl,
            rich_flag,
        )

        bx, by, bw, bh = _content_bounds_for_rich_control(
            root_window, placeholder_ctrl, placeholder_rect=placeholder_rect,
        )

        # Peer controls resize via setPosSize on later relayout; dialog-embedded only at insert.
        control = _create_rich_control_peer(smgr, ctx, toolkit, field_model, root_window)
        if control is None:
            control = _try_dialog_embedded_rich_control(root_window, placeholder_ctrl, placeholder_rect)
        if control is None:
            log.error("create_sidebar_rich_text_control: could not create RichText control peer")
            return None

        _apply_rich_control_geometry(control, bx, by, bw, bh, update_dialog_model=False)
        control.setVisible(True)
        _apply_rich_control_style_defaults(control, style_window=root_window)
        try:
            cur = control.getPosSize()
            log.info(
                "create_sidebar_rich_text_control: ready visible=%s bounds=%dx%d@%d,%d",
                control.isVisible() if hasattr(control, "isVisible") else "?",
                int(cur.Width),
                int(cur.Height),
                int(cur.X),
                int(cur.Y),
            )
        except Exception:
            log.info(
                "create_sidebar_rich_text_control: ready control visible=%s",
                control.isVisible() if hasattr(control, "isVisible") else "?",
            )
        return control
    except Exception:
        log.exception("create_sidebar_rich_text_control failed")
        return None


def _apply_char_color_to_cursor_range(model, start, end, char_color) -> None:
    if char_color is None or _is_automatic_char_color(char_color):
        return
    try:
        sel = model.createTextCursor()
        sel.gotoRange(start, False)
        sel.gotoRange(end, True)
        sel.CharColor = char_color
    except Exception as e:
        log.debug("_apply_char_color_to_cursor_range failed: %s", e)


def _insert_string_at_rich_cursor(model, cursor, text, char_color=None) -> None:
    """Insert *text* at *cursor* on a form RichText model."""
    if not text:
        return
    start = None
    try:
        start = cursor.getStart()
    except Exception:
        pass
    if char_color is not None and not _is_automatic_char_color(char_color):
        try:
            cursor.CharColor = char_color
        except Exception:
            pass

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
        try:
            from com.sun.star.text import XText

            xtext = model.queryInterface(XText)
            if xtext is not None:
                xtext.insertString(cursor, text, False)
                inserted = True
        except Exception as e:
            log.debug("_insert_string_at_rich_cursor queryInterface failed: %s", e)
    if not inserted:
        raise RuntimeError("RichTextControl model has no insertString")

    # Post-insert pass: RichTextControl often ignores pre-insert cursor CharColor.
    if start is not None and char_color is not None and not _is_automatic_char_color(char_color):
        try:
            _apply_char_color_to_cursor_range(model, start, cursor.getStart(), char_color)
        except Exception:
            pass
    try:
        cursor.gotoEnd(False)
    except Exception:
        pass


def append_text_chunk(control, text, auto_scroll=True, style_window=None, ctx=None):
    """Append plain text during assistant streaming with theme assistant color."""
    if not control or not text:
        return
    log_rich_scroll("append_chunk", control=control, chunk_len=len(text), auto_scroll=int(auto_scroll))

    def _do_append() -> None:
        theme = ChatTheme.resolve(style_window=style_window)
        model = control.getModel()
        if model is None or not hasattr(model, "createTextCursor"):
            return
        cursor = model.createTextCursor()
        cursor.gotoEnd(False)
        _apply_sidebar_para_margins(cursor)
        cursor.CharBackColor = theme.bg_color
        _insert_string_at_rich_cursor(model, cursor, text, theme.assistant_color)
        if auto_scroll:
            _nudge_rich_view_to_end_inner(control, ctx, reason="append_chunk")

    try:
        with focus_preserved(ctx):
            _do_append()
    except Exception:
        log.exception("append_text_chunk (rich control) failed")


def clear_control(control):
    """Clear all text from the rich control."""
    if not control:
        return
    try:
        from plugin.calc.navigation import clear_cell_link_spans

        clear_cell_link_spans(control)
        model = control.getModel()
        if model is not None:
            model.Text = ""
    except Exception:
        log.exception("clear_control failed")


def get_control_text_length(control) -> int:
    try:
        model = control.getModel()
        if model is None:
            return 0
        return len(model.Text or "")
    except Exception:
        return 0


def truncate_control_from(control, start_len: int | None):
    """Remove trailing plain text from *start_len* onward without resetting earlier formatting.

    Assigning ``model.Text`` would flatten the whole control to unformatted plain text
    (e.g. user message color lost). Delete only the tail via a text cursor.
    """
    if control is None or start_len is None:
        return
    try:
        model = control.getModel()
        if model is None or not hasattr(model, "createTextCursor"):
            log.warning("truncate_control_from: no text cursor; skip truncate to preserve formatting")
            return
        text = model.Text or ""
        if start_len >= len(text):
            return
        cursor = model.createTextCursor()
        cursor.gotoStart(False)
        if not hasattr(cursor, "goRight"):
            log.warning("truncate_control_from: cursor.goRight unavailable; skip truncate")
            return
        cursor.goRight(int(start_len), False)
        cursor.gotoEnd(True)
        if hasattr(cursor, "setString"):
            cursor.setString("")
        else:
            log.warning("truncate_control_from: cursor.setString unavailable; skip truncate")
            return
        log.debug("truncate_control_from: removed tail from offset %d (len now %d)", start_len, len(model.Text or ""))
    except Exception:
        log.exception("truncate_control_from failed")


def _nudge_rich_view_to_end_inner(control, ctx=None, reason: str = "unspecified") -> None:
    """Move RichText viewport to the end via the EditEngine tail-follow path."""
    log_rich_scroll("nudge_begin", control=control, reason=reason)
    try:
        model = control.getModel()
        if model is None or not hasattr(model, "createTextCursor"):
            return
        if hasattr(control, "setFocus"):
            control.setFocus()
        cursor = model.createTextCursor()
        cursor.gotoEnd(False)
        _apply_sidebar_para_margins(cursor)
        len_before = len(model.Text or "")
        _insert_string_at_rich_cursor(model, cursor, _NUDGE_SCROLL_SENTINEL, None)
        process_events_to_idle(ctx)
        tail = model.createTextCursor()
        tail.gotoEnd(False)
        if hasattr(tail, "goLeft"):
            tail.goLeft(len(_NUDGE_SCROLL_SENTINEL), True)
            if hasattr(tail, "setString"):
                tail.setString("")
        text_len = len(model.Text or "")
        rounds = 2
        for _ in range(rounds):
            process_events_to_idle(ctx)
        sentinel_removed = text_len == len_before
        log.debug("nudge_rich_control_view_to_end: len=%d rounds=%d sentinel_removed=%s", text_len, rounds, sentinel_removed)
        log_rich_scroll(
            "nudge_done",
            control=control,
            reason=reason,
            method="tail_sentinel",
            rounds=rounds + 1,
            sentinel_removed=int(sentinel_removed),
        )
    except Exception:
        log.exception("nudge_rich_control_view_to_end failed")


def nudge_rich_control_view_to_end(control, ctx=None, style_window=None, reason: str = "unspecified") -> None:
    """Scroll the read-only response control without stealing focus from the query field.

    The RichTextControl does not expose a public viewport position API. LibreOffice
    does not reliably scroll this control for `setSelection`; a temporary tail
    marker uses the same EditEngine path as real text insertion, then removes it.
    """
    if not control:
        return
    with focus_preserved(ctx):
        _nudge_rich_view_to_end_inner(control, ctx, reason=reason)

