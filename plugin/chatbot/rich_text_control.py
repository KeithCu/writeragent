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
"""Experimental RichTextControl sidebar chat via hidden Writer HTML + clipboard paste."""

from __future__ import annotations

import html
import logging
from typing import Any, cast

from plugin.chatbot.listeners import BaseWindowListener
from plugin.chatbot.rich_text import _HTML_TAG_RE, append_rich_text, strip_legacy_ai_label

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
_SERIF_FONT_MARKERS = ("serif", "times", "roman", "courier", "mono")


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
    """Set default character properties on the form TextField model (before peer creation)."""
    from plugin.chatbot.rich_text import get_theme_colors

    if model is None:
        return
    bg_color, _user_color, assistant_color = get_theme_colors(style_window=style_window)
    for name, val in (
        ("CharFontName", CHAT_FONT_NAME),
        ("CharFontNameAsian", CHAT_FONT_NAME),
        ("CharFontNameComplex", CHAT_FONT_NAME),
        ("CharHeight", CHAT_FONT_HEIGHT),
        ("CharWeight", CHAT_FONT_WEIGHT),
        ("CharPosture", 0),
        ("CharColor", assistant_color),
    ):
        _set_model_property(model, name, val)
    for name, val in (
        ("BackgroundColor", bg_color),
        ("TextColor", assistant_color),
        ("CharBackColor", bg_color),
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
    from plugin.chatbot.rich_text import get_theme_colors

    model = control.getModel() if control is not None else None
    if model is None:
        return
    _apply_rich_control_style_defaults_on_model(model, style_window=style_window)
    bg_color, _, assistant_color = get_theme_colors(style_window=style_window)
    _apply_control_surface_colors(control, bg_color, assistant_color)

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
                ("CharColor", assistant_color),
                ("CharBackColor", bg_color),
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


def _normalize_portion_font(portion) -> None:
    """Clamp hidden-doc portions to sidebar sans 10pt (HTML import often uses serif headings)."""
    try:
        font = getattr(portion, "CharFontName", "") or ""
        if not font or any(marker in font.lower() for marker in _SERIF_FONT_MARKERS):
            portion.CharFontName = CHAT_FONT_NAME
            portion.CharFontNameAsian = CHAT_FONT_NAME
            portion.CharFontNameComplex = CHAT_FONT_NAME
        height = getattr(portion, "CharHeight", 0.0) or 0.0
        if height <= 0 or height > CHAT_FONT_HEIGHT + 1.0:
            portion.CharHeight = CHAT_FONT_HEIGHT
        weight = getattr(portion, "CharWeight", 0.0) or 0.0
        if weight <= 0:
            portion.CharWeight = CHAT_FONT_WEIGHT
    except Exception:
        pass


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


def _apply_control_surface_colors(control, bg_color, text_color) -> None:
    """Theme fill on the VCL control (form model often has no BackgroundColor)."""
    if control is None:
        return
    for name, val in (
        ("BackgroundColor", bg_color),
        ("TextColor", text_color),
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
        if model is not None:
            for name, val in (("BackgroundColor", bg_color), ("TextColor", text_color)):
                if _set_model_property(model, name, val):
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


def build_message_html(text: str, role: str = "assistant") -> str:
    """Wrap chat message body as HTML with a bold role prefix."""
    if not text or not text.strip():
        return ""
    label = "You:" if role == "user" else "Assistant:"
    if _HTML_TAG_RE.search(text):
        body = text
    else:
        body = "<p>%s</p>" % html.escape(text)
    return "<p><strong>%s</strong></p>%s" % (label, body)


def _configure_hidden_writer_for_chat(doc) -> None:
    """Apply sidebar chat defaults (Liberation Sans 10pt, zero margins, no spellcheck)."""
    try:
        import uno
        from typing import cast

        style_families = doc.getStyleFamilies()
        if style_families.hasByName("ParagraphStyles"):
            para_styles = style_families.getByName("ParagraphStyles")
            if para_styles.hasByName("Standard"):
                std_para = para_styles.getByName("Standard")
                std_para.ParaLeftMargin = 0
                std_para.ParaRightMargin = 0
                std_para.ParaFirstLineIndent = 0
                std_para.ParaTopMargin = 0
                std_para.ParaBottomMargin = 200
                std_para.CharFontName = CHAT_FONT_NAME
                std_para.CharFontNameAsian = CHAT_FONT_NAME
                std_para.CharFontNameComplex = CHAT_FONT_NAME
                no_lang = cast("Any", uno.createUnoStruct("com.sun.star.lang.Locale"))
                no_lang.Language = "zxx"
                no_lang.Country = ""
                std_para.CharLocale = no_lang
                std_para.CharLocaleAsian = no_lang
                std_para.CharLocaleComplex = no_lang
        text = doc.getText()
        cursor = text.createTextCursor()
        cursor.gotoStart(False)
        cursor.gotoEnd(True)
        cursor.CharHeight = CHAT_FONT_HEIGHT
    except Exception as e:
        log.debug("_configure_hidden_writer_for_chat failed: %s", e)


def create_hidden_html_writer(ctx):
    """Load a hidden Writer document for HTML import + clipboard copy."""
    try:
        import uno
        from plugin.framework.uno_context import get_desktop

        desktop = get_desktop(ctx)
        if desktop is None:
            return None
        hidden = uno.createUnoStruct("com.sun.star.beans.PropertyValue", Name="Hidden", Value=True)
        doc = desktop.loadComponentFromURL("private:factory/swriter", "_blank", 0, (hidden,))
        return doc
    except Exception:
        log.exception("create_hidden_html_writer failed")
        return None


def _transferable_from_hidden_doc(doc):
    """Build a transferable from all content in a hidden Writer document."""
    controller = doc.getCurrentController()
    if controller is None:
        return None
    body = doc.getText()
    sel = body.createTextCursor()
    sel.gotoStart(False)
    sel.gotoEnd(True)
    controller.select(sel)
    get_tf = getattr(controller, "getTransferable", None)
    if callable(get_tf):
        return get_tf()
    try:
        from com.sun.star.datatransfer import XTransferableSupplier

        supplier = controller.queryInterface(XTransferableSupplier)
        if supplier is not None:
            return supplier.getTransferable()
    except Exception as e:
        log.debug("_transferable_from_hidden_doc queryInterface failed: %s", e)
    return None


def _log_transferable_flavors(transferable) -> list[str]:
    """Log MIME types available on a Writer transferable (diagnostics)."""
    mimes: list[str] = []
    try:
        for flavor in transferable.getTransferDataFlavors():
            mime = getattr(flavor, "MimeType", "") or "?"
            mimes.append(mime)
    except Exception as e:
        log.debug("_log_transferable_flavors failed: %s", e)
    log.debug("insert_transferable_into_rich_control: transferable flavors=%s", mimes)
    return mimes


def _log_insert_transferable_targets(control, model) -> None:
    """Log whether insertTransferable exists on control/model (diagnostics)."""
    for target_name, target in (("control", control), ("model", model)):
        if target is None:
            log.debug("insert_transferable_into_rich_control: %s is None", target_name)
            continue
        ins = getattr(target, "insertTransferable", None)
        log.debug(
            "insert_transferable_into_rich_control: %s insertTransferable=%s",
            target_name,
            "callable" if callable(ins) else type(ins).__name__,
        )
    if model is not None:
        log.debug(
            "insert_transferable_into_rich_control: model.createTextCursor=%s",
            hasattr(model, "createTextCursor"),
        )


def _set_system_clipboard(ctx, transferable) -> bool:
    """Put *transferable* on LO's in-process SystemClipboard."""
    try:
        smgr = ctx.getServiceManager()
        clip = smgr.createInstanceWithContext("com.sun.star.datatransfer.clipboard.SystemClipboard", ctx)
        if clip is None:
            log.error("_set_system_clipboard: SystemClipboard unavailable")
            return False
        clip.setContents(transferable, None)
        return True
    except Exception:
        log.exception("_set_system_clipboard failed")
        return False


def _is_ordered_numbering_type(num_type) -> bool:
    """True when Writer numbering is numeric/alpha, not a bullet glyph."""
    if num_type is None:
        return False
    try:
        # com.sun.star.style.NumberingType — ARABIC=4, ROMAN=2/3, CHARS=0/1; BULLET=6
        n = int(num_type)
        return n in (0, 1, 2, 3, 4, 5)
    except (TypeError, ValueError):
        return False


def _list_prefix_for_paragraph(para, order_counters: dict) -> str:
    """Bullet or number prefix for a Writer list paragraph (EditEngine paste has no NumberingRules)."""
    try:
        is_number = bool(para.getPropertyValue("NumberingIsNumber"))
    except Exception:
        is_number = False

    if not is_number:
        try:
            left = int(para.getPropertyValue("ParaLeftMargin") or 0)
            if left > 300:
                return "\u2022 "
        except Exception:
            pass
        return ""

    try:
        level = int(para.getPropertyValue("NumberingLevel") or 0)
        list_id = para.getPropertyValue("ListId")
    except Exception:
        level = 0
        list_id = None
    key = (list_id, level)
    indent = "  " * max(0, level)

    bullet_char = "\u2022"
    num_type = None
    try:
        rules = para.getPropertyValue("NumberingRules")
        if rules is not None:
            props = list(rules.getByIndex(level))
            for p in props:
                if p.Name == "BulletChar" and p.Value:
                    ch = p.Value
                    bullet_char = ch if isinstance(ch, str) else str(ch)
                if p.Name == "NumberingType":
                    num_type = p.Value
    except Exception:
        pass
    if num_type is None:
        try:
            num_type = para.getPropertyValue("NumberingType")
        except Exception:
            pass

    if _is_ordered_numbering_type(num_type):
        order_counters[key] = order_counters.get(key, 0) + 1
        return "%s%d. " % (indent, order_counters[key])

    ch = (bullet_char or "\u2022").strip()
    if ch and not ch.endswith(" "):
        ch = ch + " "
    return indent + ch


def _rich_control_bg_color(model, style_window=None) -> int:
    """Theme fill color for the control (matches embedded Writer sidebar)."""
    bg = getattr(model, "BackgroundColor", None)
    if isinstance(bg, int):
        return bg
    try:
        if hasattr(model, "getPropertyValue"):
            bg = model.getPropertyValue("BackgroundColor")
            if isinstance(bg, int):
                return bg
    except Exception:
        pass
    from plugin.chatbot.rich_text import get_theme_colors

    bg_color, _, _ = get_theme_colors(style_window=style_window)
    return bg_color


def _apply_cursor_char_props(dest_cursor, src_portion, bg_color=None) -> None:
    """Copy character formatting from a Writer text portion onto a RichText cursor."""
    for prop in (
        "CharWeight",
        "CharPosture",
        "CharColor",
        "CharUnderline",
        "CharHeight",
        "CharFontName",
        "CharUnderlineColor",
    ):
        try:
            setattr(dest_cursor, prop, getattr(src_portion, prop))
        except Exception:
            pass
    if bg_color is not None:
        try:
            dest_cursor.CharBackColor = bg_color
        except Exception:
            pass


def _insert_string_at_rich_cursor(model, cursor, text) -> None:
    """Insert *text* at *cursor* on a form RichText model."""
    if not text:
        return
    insert = getattr(model, "insertString", None)
    if callable(insert):
        insert(cursor, text, False)
        return
    get_text = getattr(model, "getText", None)
    if callable(get_text):
        text_obj = get_text()
        insert_fn = getattr(text_obj, "insertString", None)
        if callable(insert_fn):
            insert_fn(cursor, text, False)
            return
    try:
        from com.sun.star.text import XText

        xtext = model.queryInterface(XText)
        if xtext is not None:
            xtext.insertString(cursor, text, False)
            return
    except Exception as e:
        log.debug("_insert_string_at_rich_cursor queryInterface failed: %s", e)
    raise RuntimeError("RichTextControl model has no insertString")


def _copy_formatted_from_hidden_doc_to_control(src_doc, control, ctx) -> bool:
    """Copy formatted Writer body text into the sidebar RichText control (safe — no clipboard/frame paste)."""
    model = control.getModel()
    if model is None or not hasattr(model, "createTextCursor"):
        log.error("_copy_formatted_from_hidden_doc_to_control: model has no createTextCursor")
        return False
    try:
        _process_idle(ctx)

        dest_cursor = model.createTextCursor()
        dest_cursor.gotoEnd(False)
        _apply_sidebar_para_margins(dest_cursor)
        fill_color = _rich_control_bg_color(model)

        src_text = src_doc.getText()
        para_enum = src_text.createEnumeration()
        first_para = True
        inserted = False
        order_counters: dict = {}
        while para_enum.hasMoreElements():
            para = para_enum.nextElement()
            line_prefix = _list_prefix_for_paragraph(para, order_counters)
            if not first_para:
                _insert_string_at_rich_cursor(model, dest_cursor, "\n")
                dest_cursor.gotoEnd(False)
                _apply_sidebar_para_margins(dest_cursor)
            first_para = False
            prefix_inserted = not line_prefix

            portion_enum = para.createEnumeration()
            while portion_enum.hasMoreElements():
                portion = portion_enum.nextElement()
                txt = portion.getString()
                if not txt:
                    continue
                if line_prefix and not prefix_inserted:
                    _insert_string_at_rich_cursor(model, dest_cursor, line_prefix)
                    dest_cursor.gotoEnd(False)
                    prefix_inserted = True
                _apply_cursor_char_props(dest_cursor, portion, bg_color=fill_color)
                _normalize_portion_font(portion)
                _apply_cursor_char_props(dest_cursor, portion, bg_color=fill_color)
                _insert_string_at_rich_cursor(model, dest_cursor, txt)
                dest_cursor.gotoEnd(False)
                inserted = True
            if line_prefix and not prefix_inserted:
                _insert_string_at_rich_cursor(model, dest_cursor, line_prefix)
                inserted = True

        if inserted:
            scroll_rich_control_to_bottom(control, ctx=ctx, aggressive=True)
            log.info(
                "_copy_formatted_from_hidden_doc_to_control: ok control_len=%d",
                get_control_text_length(control),
            )
        return inserted
    except Exception:
        log.exception("_copy_formatted_from_hidden_doc_to_control failed")
        return False


def _try_paste_via_key_event(ctx, control) -> bool:
    """Simulate Ctrl+V on the focused RichText control peer."""
    try:
        import uno

        peer = control.getPeer() if hasattr(control, "getPeer") else None
        if peer is None:
            log.debug("_try_paste_via_key_event: no peer")
            return False
        key_pressed = getattr(peer, "keyPressed", None)
        key_released = getattr(peer, "keyReleased", None)
        if not callable(key_pressed) or not callable(key_released):
            log.debug("_try_paste_via_key_event: peer has no keyPressed/keyReleased")
            return False
        ev = cast("Any", uno.createUnoStruct("com.sun.star.awt.KeyEvent"))
        ev.Modifiers = 1  # MOD1 / Ctrl
        ev.KeyCode = 86  # V
        ev.KeyChar = "v"
        key_pressed(ev)
        key_released(ev)
        _process_idle(ctx)
        log.info("insert_transferable_into_rich_control: dispatched Ctrl+V on control peer")
        return True
    except Exception as e:
        log.debug("_try_paste_via_key_event failed: %s", e)
        return False


def _try_insert_transferable_on_target(target_name, target, transferable, ctx) -> bool:
    """Call insertTransferable when present; log and swallow errors."""
    if target is None:
        return False
    ins = getattr(target, "insertTransferable", None)
    if not callable(ins):
        return False
    try:
        ins(transferable)
        log.info("insert_transferable_into_rich_control: via %s.insertTransferable", target_name)
        _process_idle(ctx)
        return True
    except Exception as e:
        log.debug("insert_transferable_into_rich_control: %s.insertTransferable failed: %s", target_name, e)
        return False


def insert_transferable_into_rich_control(control, transferable, ctx, style_window=None):
    """Insert formatted content into the sidebar RichText control (not the document)."""
    if control is None or transferable is None:
        return False

    result = False

    def _try_paths() -> None:
        nonlocal result
        model = control.getModel()
        _log_transferable_flavors(transferable)
        _log_insert_transferable_targets(control, model)

        len_before = get_control_text_length(control)
        scroll_rich_control_to_bottom(control, ctx=ctx, aggressive=True)
        _process_idle(ctx)

        for target_name, target in (("control", control), ("model", model)):
            if _try_insert_transferable_on_target(target_name, target, transferable, ctx):
                scroll_rich_control_to_bottom(control, ctx=ctx, aggressive=True)
                if get_control_text_length(control) > len_before:
                    result = True
                    return

        if model is not None and hasattr(model, "createTextCursor"):
            try:
                cursor = model.createTextCursor()
                cursor.gotoEnd(False)
                if _try_insert_transferable_on_target("model cursor", cursor, transferable, ctx):
                    scroll_rich_control_to_bottom(control, ctx=ctx, aggressive=True)
                    if get_control_text_length(control) > len_before:
                        result = True
                        return
            except Exception as e:
                log.debug("insert_transferable_into_rich_control cursor path failed: %s", e)

        if _set_system_clipboard(ctx, transferable):
            scroll_rich_control_to_bottom(control, ctx=ctx, aggressive=True)
            _process_idle(ctx)
            if _try_paste_via_key_event(ctx, control):
                scroll_rich_control_to_bottom(control, ctx=ctx, aggressive=True)
                if get_control_text_length(control) > len_before:
                    result = True
                    return

        flavors = _log_transferable_flavors(transferable)
        log.error(
            "insert_transferable_into_rich_control: all rich insert paths failed (len_before=%d len_after=%d flavors=%s)",
            len_before,
            get_control_text_length(control),
            flavors,
        )

    try:
        _preserve_focus_window(ctx, _try_paths)
    except Exception:
        log.exception("insert_transferable_into_rich_control failed")
    return result


def append_rich_text_via_clipboard(ctx, control, text, role="assistant", style_window=None, auto_scroll=True):
    """Import HTML in a hidden Writer doc and copy formatted content into the RichText control."""
    if not control or not text or not text.strip():
        return
    if role == "assistant":
        text = strip_legacy_ai_label(text)
    _ensure_message_separator(control)
    doc = None
    try:
        doc = create_hidden_html_writer(ctx)
        if doc is None:
            log.error("append_rich_text_via_clipboard: hidden Writer unavailable")
            return
        _configure_hidden_writer_for_chat(doc)
        append_rich_text(doc, text, role=role, auto_scroll=False, style_window=style_window)
        log.debug("append_rich_text_via_clipboard: hidden doc ready len=%d role=%s", len(text), role)
        inserted = False
        if _copy_formatted_from_hidden_doc_to_control(doc, control, ctx):
            inserted = True
            log.info("append_rich_text_via_clipboard: insert ok control_len=%d", get_control_text_length(control))
        else:
            transferable = _transferable_from_hidden_doc(doc)
            if transferable is None:
                log.error("append_rich_text_via_clipboard: formatted copy and transferable both unavailable")
                return
            log.debug("append_rich_text_via_clipboard: falling back to transferable insert")
            if not insert_transferable_into_rich_control(control, transferable, ctx, style_window=style_window):
                log.error("append_rich_text_via_clipboard: rich insert into control failed")
                return
            inserted = True
            log.info("append_rich_text_via_clipboard: insert ok control_len=%d", get_control_text_length(control))
        if inserted and role == "user":
            _ensure_trailing_line_break(control)
        if inserted and auto_scroll:
            _preserve_focus_window(ctx, lambda: scroll_rich_control_to_bottom(control, ctx=ctx, aggressive=True))
        if inserted:
            return
    except Exception:
        log.exception("append_rich_text_via_clipboard failed")
    finally:
        if doc is not None:
            try:
                doc.close(True)
            except Exception:
                pass


def append_text_chunk(control, text, auto_scroll=True, style_window=None, ctx=None):
    """Append plain text during assistant streaming with theme assistant color."""
    if not control or not text:
        return

    def _do_append() -> None:
        from plugin.chatbot.rich_text import get_theme_colors

        model = control.getModel()
        if model is None or not hasattr(model, "createTextCursor"):
            return
        bg_color, _, assistant_color = get_theme_colors(style_window=style_window)
        cursor = model.createTextCursor()
        cursor.gotoEnd(False)
        _apply_sidebar_para_margins(cursor)
        cursor.CharColor = assistant_color
        cursor.CharBackColor = bg_color
        _insert_string_at_rich_cursor(model, cursor, text)
        if auto_scroll:
            scroll_rich_control_to_bottom(control, ctx=ctx, aggressive=True)

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


def _scroll_rich_peer_vertical_bar(control) -> bool:
    """Move VCL vertical scrollbar to max without touching control focus/selection."""
    peer = control.getPeer() if hasattr(control, "getPeer") else None
    if peer is None:
        return False
    get_windows = getattr(peer, "getWindows", None)
    if not callable(get_windows):
        return False
    try:
        from com.sun.star.awt import XScrollBar

        stack = list(get_windows())
        visited = 0
        while stack and visited < 64:
            window = stack.pop()
            visited += 1
            try:
                sb = window.queryInterface(XScrollBar)
                if sb is not None:
                    rng = sb.getScrollRange()
                    target = int(rng.Maximum) - int(getattr(rng, "VisibleSize", 0) or 0)
                    if target < int(rng.Minimum):
                        target = int(rng.Minimum)
                    sb.setScrollValue(target)
                    return True
            except Exception:
                pass
            child_get = getattr(window, "getWindows", None)
            if callable(child_get):
                try:
                    stack.extend(child_get())
                except Exception:
                    pass
    except Exception as e:
        log.debug("_scroll_rich_peer_vertical_bar failed: %s", e)
    return False


def scroll_rich_control_to_bottom(control, ctx=None, aggressive=False) -> None:
    """Scroll the read-only response control without stealing focus from the query field.

    Do not call ``setFocus`` or ``setSelection`` on the RichText control — both move
    keyboard focus away from ``query`` during streaming (plain ``response`` only used
    ``setSelection`` on a simple edit; RichTextControl behaves differently).
    """
    if not control:
        return
    del aggressive  # kept for call-site compatibility
    try:
        model = control.getModel()
        if model is not None and hasattr(model, "createTextCursor"):
            try:
                cursor = model.createTextCursor()
                cursor.gotoEnd(False)
            except Exception as e:
                log.debug("scroll_rich_control_to_bottom cursor.gotoEnd: %s", e)
        _scroll_rich_peer_vertical_bar(control)
        _process_idle(ctx)
        text_len = len(model.Text or "") if model is not None else 0
        log.debug("scroll_rich_control_to_bottom: len=%d", text_len)
    except Exception:
        log.exception("scroll_rich_control_to_bottom failed")


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


def _ensure_message_separator(control):
    """Insert paragraph breaks without assigning model.Text (preserves rich formatting)."""
    try:
        model = control.getModel()
        if model is None or not (model.Text or "").strip():
            return
        if not hasattr(model, "createTextCursor"):
            return
        cursor = model.createTextCursor()
        cursor.gotoEnd(False)
        _insert_string_at_rich_cursor(model, cursor, "\n\n")
    except Exception:
        pass


def _ensure_trailing_line_break(control) -> None:
    """Leave a blank line after the user message before assistant streaming.

    Formatted copy from Writer often has no trailing ``\\n``; a single ``\\n`` only moves to
    the next line. We want ``\\n\\n`` (same gap as ``_ensure_message_separator``).
    """
    try:
        model = control.getModel()
        if model is None or not (model.Text or "").strip():
            return
        if not hasattr(model, "createTextCursor"):
            return
        text = model.Text or ""
        if text.endswith("\n\n"):
            return
        suffix = "\n" if text.endswith("\n") else "\n\n"
        cursor = model.createTextCursor()
        cursor.gotoEnd(False)
        _insert_string_at_rich_cursor(model, cursor, suffix)
    except Exception:
        pass


def _process_idle(ctx):
    try:
        from plugin.framework.uno_context import get_toolkit

        tk = get_toolkit(ctx)
        if tk and hasattr(tk, "processEventsToIdle"):
            tk.processEventsToIdle()
    except Exception:
        pass
