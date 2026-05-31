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
from typing import Any, cast

from plugin.chatbot.listeners import BaseWindowListener
from plugin.chatbot.rich_text import ChatTheme, strip_legacy_ai_label

log = logging.getLogger(__name__)

_CONTROL_INIT_STARTED: set[int] = set()
RICH_CONTROL_NAME = "response_rich"
CHAT_FONT_NAME = "Liberation Sans"
CHAT_FONT_HEIGHT = 10.0
CHAT_FONT_WEIGHT = 100.0
# Dialog units (AppFont) — inset RichTextControl inside the response placeholder so glyphs are not clipped.
RICH_CONTROL_EDGE_INSET = 8
# Writer paragraph margins (1/100 mm) — horizontal padding inside the EditEngine text area.
CHAT_PARA_SIDE_MARGIN = 250
# History reload: batch hidden-Writer + copy cycles to avoid per-message UI repaint.
HISTORY_RENDER_BATCH_CHARS = 16384
# LO awt KeyCode values (Linux/GTK) for RichTextControl scroll fallbacks when no VCL scrollbar is exposed.
_RICH_KEY_END = 769
_RICH_KEY_PAGE_DOWN = 771
# Invisible tail insert triggers EditEngine viewport follow (gotoEnd alone does not after bulk copy).
_NUDGE_SCROLL_SENTINEL = "\u200b"


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

    def nudge_view_to_end(self) -> None:
        """Scroll the control view to the very end."""
        nudge_rich_control_view_to_end(self.control, ctx=self.ctx, style_window=self.style_window)

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


def _is_automatic_char_color(color) -> bool:
    """True for LO automatic / unset character colors (COL_AUTO)."""
    if color is None:
        return True
    if not isinstance(color, int):
        return True
    return color < 0 or color == 0xFFFFFFFF


def _layout_right_edge_for_rich_control(root_window, placeholder_ctrl) -> int:
    """Right edge for the transcript: Clear button (same row as Send/Stop), capped by panel width."""
    ps = placeholder_ctrl.getPosSize()
    right = int(ps.X) + int(ps.Width)
    try:
        if root_window is not None and hasattr(root_window, "getControl"):
            clear_ctrl = root_window.getControl("clear")
            if clear_ctrl is not None:
                cr = clear_ctrl.getPosSize()
                clear_right = int(cr.X) + int(cr.Width)
                if clear_right > int(ps.X):
                    right = clear_right
    except Exception as e:
        log.debug("_layout_right_edge_for_rich_control: %s", e)
    try:
        if root_window is not None:
            root_w = int(root_window.getPosSize().Width)
            if root_w > 0:
                right = min(right, root_w - 4)
    except Exception:
        pass
    return right


def sidebar_content_right_edge(root_window, placeholder_ctrl) -> int:
    """Right edge (exclusive) for chat content; query field uses the same clamp as RichTextControl."""
    return _layout_right_edge_for_rich_control(root_window, placeholder_ctrl) - RICH_CONTROL_EDGE_INSET


def _content_bounds_for_rich_control(root_window, placeholder_ctrl):
    """Return (x, y, width, height) for the rich control inside the response area."""
    ps = placeholder_ctrl.getPosSize()
    inset = RICH_CONTROL_EDGE_INSET
    x = int(ps.X) + inset
    y = int(ps.Y) + inset
    right = _layout_right_edge_for_rich_control(root_window, placeholder_ctrl)
    w = max(20, right - x - inset)
    h = max(20, int(ps.Height) - 2 * inset)
    return x, y, w, h


def sync_rich_control_bounds(rich_control, root_window, placeholder_ctrl) -> bool:
    """Position the rich control over the response area without exceeding the button row width."""
    if rich_control is None or placeholder_ctrl is None:
        return False
    try:
        bx, by, bw, bh = _content_bounds_for_rich_control(root_window, placeholder_ctrl)
        cur = rich_control.getPosSize()
        if int(cur.X) == bx and int(cur.Y) == by and int(cur.Width) == bw and int(cur.Height) == bh:
            return False
        rich_control.setPosSize(bx, by, bw, bh, 15)
        return True
    except Exception as e:
        log.debug("sync_rich_control_bounds failed: %s", e)
        return False


def _apply_sidebar_para_margins(cursor) -> None:
    """Keep chat text off the RichTextControl edges (EditEngine has no CSS padding)."""
    for name, val in (
        ("ParaLeftMargin", CHAT_PARA_SIDE_MARGIN),
        ("ParaRightMargin", CHAT_PARA_SIDE_MARGIN),
        ("ParaFirstLineIndent", 0),
    ):
        try:
            setattr(cursor, name, val)
        except Exception:
            pass


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
            char_props = (
                ("CharFontName", CHAT_FONT_NAME),
                ("CharFontNameAsian", CHAT_FONT_NAME),
                ("CharFontNameComplex", CHAT_FONT_NAME),
                ("CharHeight", CHAT_FONT_HEIGHT),
                ("CharWeight", CHAT_FONT_WEIGHT),
                ("CharPosture", 0),
                ("CharBackColor", theme.bg_color),
            )
            cursor = model.createTextCursor()
            cursor.gotoStart(False)
            text_len = len(model.Text or "")
            if text_len > 0:
                cursor.gotoEnd(True)
            for name, val in char_props:
                try:
                    setattr(cursor, name, val)
                except Exception:
                    pass
            _apply_sidebar_para_margins(cursor)
            cursor.gotoEnd(False)
            if hasattr(control, "setSelection"):
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
                    if hasattr(parent_window, "addControl"):
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


def _try_dialog_embedded_rich_control(root_window, placeholder_ctrl):
    """Insert field model via UnoControlDialogModel.createInstance (has PositionX)."""
    try:
        dlg_model = root_window.getModel()
        if dlg_model is None:
            log.debug("create_sidebar_rich_text_control: dialog-embedded skipped (no model)")
            return None
        if not hasattr(dlg_model, "createInstance"):
            log.debug("create_sidebar_rich_text_control: dialog-embedded skipped (no createInstance)")
            return None

        bx, by, bw, bh = _content_bounds_for_rich_control(root_window, placeholder_ctrl)
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
    """Creates a form TextField with RichText=true over the chat response placeholder."""

    def __init__(self, ctx, root_window, placeholder_ctrl, on_ready_callback):
        self.ctx = ctx
        self.root_window = root_window
        self.placeholder_ctrl = placeholder_ctrl
        self.on_ready_callback = on_ready_callback
        self.rich_control = None
        self.initialized = False
        self._disposed = False
        self._syncing_bounds = False

    def disposing(self, Source):
        self._disposed = True
        self.rich_control = None

    def on_window_shown(self, rEvent):
        if self._disposed or self.initialized:
            return
        parent_id = id(self.root_window)
        if parent_id in _CONTROL_INIT_STARTED:
            return
        peer = self.root_window.getPeer()
        if not peer:
            return
        self.initialized = True
        _CONTROL_INIT_STARTED.add(parent_id)
        from plugin.framework.queue_executor import post_to_main_thread

        post_to_main_thread(self._deferred_init)

    def on_window_resized(self, rEvent):
        """Keep the rich control over the response placeholder; do not scroll or focus here.

        Panel resize already moves the hidden ``response`` placeholder; we mirror it with inset.
        Calling ``setFocus`` / scroll during resize re-entered ``windowResized`` and could crash LO.
        """
        if self._disposed or self._syncing_bounds or not self.rich_control or not self.placeholder_ctrl:
            return
        if self._syncing_bounds:
            return
        self._syncing_bounds = True
        try:
            sync_rich_control_bounds(self.rich_control, self.root_window, self.placeholder_ctrl)
        finally:
            self._syncing_bounds = False

    def _deferred_init(self):
        if self._disposed:
            return
        try:
            control = create_sidebar_rich_text_control(self.ctx, self.root_window, self.placeholder_ctrl)
            if not control:
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
            sync_rich_control_bounds(control, self.root_window, self.placeholder_ctrl)
            self.on_ready_callback(control)
        except Exception:
            log.exception("RichTextControlListener deferred init failed")


def create_sidebar_rich_text_control(ctx, root_window, placeholder_ctrl):
    """Create a form RichText TextField peer positioned over the response placeholder."""
    try:
        smgr = ctx.getServiceManager()
        toolkit = smgr.createInstanceWithContext("com.sun.star.awt.Toolkit", ctx)
        if toolkit is None:
            log.error("create_sidebar_rich_text_control: toolkit unavailable")
            return None

        ps = placeholder_ctrl.getPosSize()
        log.info(
            "create_sidebar_rich_text_control: placeholder at x=%s y=%s w=%s h=%s",
            ps.X,
            ps.Y,
            ps.Width,
            ps.Height,
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

        control = _try_dialog_embedded_rich_control(root_window, placeholder_ctrl)
        if control is None:
            control = _create_rich_control_peer(smgr, ctx, toolkit, field_model, root_window)
        if control is None:
            log.error("create_sidebar_rich_text_control: could not create RichText control peer")
            return None

        bx, by, bw, bh = _content_bounds_for_rich_control(root_window, placeholder_ctrl)
        control.setPosSize(bx, by, bw, bh, 15)
        control.setVisible(True)
        _apply_rich_control_style_defaults(control, style_window=root_window)
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
            _nudge_rich_view_to_end_inner(control, ctx)

    try:
        _preserve_focus_window(ctx, _do_append)
    except Exception:
        log.exception("append_text_chunk (rich control) failed")


def clear_control(control):
    """Clear all text from the rich control."""
    if not control:
        return
    try:
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


def _nudge_rich_view_to_end_inner(control, ctx=None) -> None:
    """Move RichText viewport to end via tail insert (caller may already hold focus preserve).

    Bulk history copy leaves the viewport at the top: ``gotoEnd`` alone does not scroll
    EditEngine when focus stays on ``query``. A zero-width tail insert (then removed)
    matches the streaming append path that actually moves the caret/view.
    """
    try:
        model = control.getModel()
        if model is None or not hasattr(model, "createTextCursor"):
            return
        cursor = model.createTextCursor()
        cursor.gotoEnd(False)
        _apply_sidebar_para_margins(cursor)
        len_before = len(model.Text or "")
        _insert_string_at_rich_cursor(model, cursor, _NUDGE_SCROLL_SENTINEL, None)
        tail = model.createTextCursor()
        tail.gotoEnd(False)
        if hasattr(tail, "goLeft"):
            tail.goLeft(len(_NUDGE_SCROLL_SENTINEL), True)
            if hasattr(tail, "setString"):
                tail.setString("")
        text_len = len(model.Text or "")
        rounds = 3
        for _ in range(rounds):
            _process_idle(ctx)
        log.debug(
            "nudge_rich_control_view_to_end: len=%d rounds=%d sentinel_removed=%s",
            text_len,
            rounds,
            text_len == len_before,
        )
    except Exception:
        log.exception("nudge_rich_control_view_to_end failed")


def nudge_rich_control_view_to_end(control, ctx=None, style_window=None) -> None:
    """Scroll the read-only response control without stealing focus from the query field.

    Uses a zero-width tail ``insertString`` (removed immediately) under ``_preserve_focus_window``
    — same mechanism as streaming appends. ``gotoEnd`` + idle alone does not move the viewport
    after bulk history copy; VCL scrollbars are not exposed on this control.
    """
    if not control:
        return
    _preserve_focus_window(ctx, lambda: _nudge_rich_view_to_end_inner(control, ctx))


def _preserve_focus_window(ctx, fn) -> None:
    """Run *fn* and restore whichever sidebar control had focus (usually ``query``)."""
    saved = None
    try:
        from plugin.framework.uno_context import get_toolkit

        tk = get_toolkit(ctx)
        if tk is not None and hasattr(tk, "getFocusWindow"):
            saved = tk.getFocusWindow()
    except Exception as e:
        log.debug("_preserve_focus_window capture: %s", e)
    try:
        fn()
    finally:
        if saved is not None:
            try:
                if hasattr(saved, "setFocus"):
                    saved.setFocus()
            except Exception as e:
                log.debug("_preserve_focus_window restore: %s", e)


def _process_idle(ctx):
    try:
        from plugin.framework.uno_context import get_toolkit

        tk = get_toolkit(ctx)
        if tk and hasattr(tk, "processEventsToIdle"):
            tk.processEventsToIdle()
    except Exception:
        pass
