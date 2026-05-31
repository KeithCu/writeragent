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
"""Hidden Writer HTML import and formatted insert/paste into the sidebar RichTextControl.

Pipeline: create_hidden_html_writer → append_rich_text (HTML filter) → direct portion copy
into the control (preferred), then transferable / SystemClipboard / Ctrl+V fallbacks.

The direct-copy path walks Writer paragraphs/portions and inserts via insertString on the
form TextField model. EditEngine paste does not preserve Writer NumberingRules, so list
bullets and ordered numbers are reconstructed manually — see _list_prefix_for_paragraph.
"""

from __future__ import annotations

import html
import logging
from typing import Any, cast

from plugin.chatbot.rich_text import ChatTheme, _HTML_TAG_RE, append_rich_text, strip_legacy_ai_label
from plugin.chatbot.rich_text_control import (
    CHAT_FONT_HEIGHT,
    CHAT_FONT_NAME,
    CHAT_FONT_WEIGHT,
    HISTORY_RENDER_BATCH_CHARS,
    _apply_sidebar_para_margins,
    _insert_string_at_rich_cursor,
    _is_automatic_char_color,
    _preserve_focus_window,
    _process_idle,
    get_control_text_length,
    nudge_rich_control_view_to_end,
)

log = logging.getLogger(__name__)

_SERIF_FONT_MARKERS = ("serif", "times", "roman", "courier", "mono")


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


def _role_color_for_text(text: str, user_color: int, assistant_color: int, default_role: str = "assistant") -> int:
    stripped = (text or "").lstrip()
    if stripped.startswith("You:"):
        return user_color
    if stripped.startswith("Assistant:"):
        return assistant_color
    return user_color if default_role == "user" else assistant_color


def _resolve_portion_char_color(src_portion, txt, user_color: int, assistant_color: int, default_role: str = "assistant") -> int:
    raw = getattr(src_portion, "CharColor", None)
    if isinstance(raw, int) and not _is_automatic_char_color(raw):
        return raw
    return _role_color_for_text(txt, user_color, assistant_color, default_role)


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
    """Bullet or number prefix for a Writer list paragraph.

    RichTextControl's EditEngine does not preserve Writer NumberingRules on insertString
    paste/copy, so ordered lists and bullets would disappear without manual prefix text.
    We read NumberingRules from the hidden Writer doc and emit literal prefix strings
    (e.g. ``• `` or ``1. ``) before each list paragraph's portions.
    """
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
    """Theme fill color for the control (matches sidebar dialog chrome)."""
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

    theme = ChatTheme.resolve(style_window=style_window)
    return theme.bg_color


def _apply_cursor_char_props(dest_cursor, src_portion, char_color=None, bg_color=None) -> None:
    """Copy character formatting from a Writer text portion onto a RichText cursor."""
    for prop in (
        "CharWeight",
        "CharPosture",
        "CharUnderline",
        "CharHeight",
        "CharFontName",
        "CharUnderlineColor",
    ):
        try:
            setattr(dest_cursor, prop, getattr(src_portion, prop))
        except Exception:
            pass
    resolved = char_color
    if resolved is None:
        raw = getattr(src_portion, "CharColor", None)
        if not _is_automatic_char_color(raw):
            resolved = raw
    if resolved is not None and not _is_automatic_char_color(resolved):
        try:
            dest_cursor.CharColor = resolved
        except Exception:
            pass
    if bg_color is not None:
        try:
            dest_cursor.CharBackColor = bg_color
        except Exception:
            pass


def iter_history_message_batches(items, batch_chars=HISTORY_RENDER_BATCH_CHARS):
    """Yield batches of (role, content) tuples, each batch at most *batch_chars* total content length.

    Never splits a single message; an oversized message becomes its own batch.
    """
    batch: list[tuple[str, str]] = []
    size = 0
    for role, content in items:
        content_len = len(content or "")
        if batch and size + content_len > batch_chars:
            yield batch
            batch = []
            size = 0
        batch.append((role, content))
        size += content_len
    if batch:
        yield batch


def session_history_items(session, greeting=""):
    """Build (role, content) pairs for session history display (skips system messages)."""
    items: list[tuple[str, str]] = []
    if greeting:
        items.append(("assistant", greeting))
    for msg in session.messages:
        role = msg.get("role", "")
        content = msg.get("content", "")
        if role == "user":
            items.append(("user", content))
        elif role == "assistant":
            if content:
                items.append(("assistant", content))
            elif msg.get("tool_calls"):
                items.append(("assistant", "[Thinking...]"))
    return items


def _copy_formatted_from_hidden_doc_to_control(
    src_doc,
    control,
    ctx,
    role: str = "assistant",
    style_window=None,
    auto_scroll: bool = True,
) -> bool:
    """Copy formatted Writer body text into the sidebar RichText control (safe — no clipboard/frame paste)."""
    model = control.getModel()
    if model is None or not hasattr(model, "createTextCursor"):
        log.error("_copy_formatted_from_hidden_doc_to_control: model has no createTextCursor")
        return False

    inserted = False

    def _do_copy() -> None:
        nonlocal inserted
        try:
            if auto_scroll:
                _process_idle(ctx)
            theme = ChatTheme.resolve(style_window=style_window)
            default_color = _role_color_for_text("", theme.user_color, theme.assistant_color, role)

            dest_cursor = model.createTextCursor()
            dest_cursor.gotoEnd(False)
            _apply_sidebar_para_margins(dest_cursor)
            fill_color = _rich_control_bg_color(model, style_window=style_window)

            src_text = src_doc.getText()
            para_enum = src_text.createEnumeration()
            first_para = True
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
                        _insert_string_at_rich_cursor(model, dest_cursor, line_prefix, default_color)
                        dest_cursor.gotoEnd(False)
                        prefix_inserted = True
                    portion_color = _resolve_portion_char_color(portion, txt, theme.user_color, theme.assistant_color, role)
                    _apply_cursor_char_props(dest_cursor, portion, char_color=portion_color, bg_color=fill_color)
                    _normalize_portion_font(portion)
                    _apply_cursor_char_props(dest_cursor, portion, char_color=portion_color, bg_color=fill_color)
                    _insert_string_at_rich_cursor(model, dest_cursor, txt, portion_color)
                    dest_cursor.gotoEnd(False)
                    inserted = True
                if line_prefix and not prefix_inserted:
                    _insert_string_at_rich_cursor(model, dest_cursor, line_prefix, default_color)
                    inserted = True

            if inserted:
                if auto_scroll:
                    nudge_rich_control_view_to_end(control, ctx=ctx, style_window=style_window)
                log.info(
                    "_copy_formatted_from_hidden_doc_to_control: ok control_len=%d",
                    get_control_text_length(control),
                )
        except Exception:
            log.exception("_copy_formatted_from_hidden_doc_to_control failed")
            inserted = False

    if ctx is not None:
        _preserve_focus_window(ctx, _do_copy)
    else:
        _do_copy()
    return inserted


def _append_hidden_doc_to_control(doc, control, ctx, style_window=None, auto_scroll=True) -> bool:
    """Copy hidden Writer content into the sidebar control (formatted copy, then transferable fallback)."""
    if _copy_formatted_from_hidden_doc_to_control(
        doc, control, ctx, role="assistant", style_window=style_window, auto_scroll=auto_scroll,
    ):
        return True
    transferable = _transferable_from_hidden_doc(doc)
    if transferable is None:
        return False
    log.debug("_append_hidden_doc_to_control: falling back to transferable insert")
    return insert_transferable_into_rich_control(control, transferable, ctx, style_window=style_window)


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
        nudge_rich_control_view_to_end(control, ctx=ctx, style_window=style_window)

        for target_name, target in (("control", control), ("model", model)):
            if _try_insert_transferable_on_target(target_name, target, transferable, ctx):
                nudge_rich_control_view_to_end(control, ctx=ctx, style_window=style_window)
                if get_control_text_length(control) > len_before:
                    result = True
                    return

        if model is not None and hasattr(model, "createTextCursor"):
            try:
                cursor = model.createTextCursor()
                cursor.gotoEnd(False)
                if _try_insert_transferable_on_target("model cursor", cursor, transferable, ctx):
                    nudge_rich_control_view_to_end(control, ctx=ctx, style_window=style_window)
                    if get_control_text_length(control) > len_before:
                        result = True
                        return
            except Exception as e:
                log.debug("insert_transferable_into_rich_control cursor path failed: %s", e)

        if _set_system_clipboard(ctx, transferable):
            nudge_rich_control_view_to_end(control, ctx=ctx, style_window=style_window)
            _process_idle(ctx)
            if _try_paste_via_key_event(ctx, control):
                nudge_rich_control_view_to_end(control, ctx=ctx, style_window=style_window)
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


def append_rich_messages_via_clipboard(
    ctx,
    control,
    items,
    style_window=None,
    batch_chars=HISTORY_RENDER_BATCH_CHARS,
):
    """Render many chat messages with minimal UI updates (batched hidden Writer + copy)."""
    if not control or not items:
        return
    any_inserted = False
    batches = list(iter_history_message_batches(items, batch_chars))
    for batch in batches:
        doc = None
        try:
            doc = create_hidden_html_writer(ctx)
            if doc is None:
                log.error("append_rich_messages_via_clipboard: hidden Writer unavailable")
                return
            _configure_hidden_writer_for_chat(doc)
            for role, content in batch:
                append_rich_text(doc, content, role=role, style_window=style_window)
            log.debug(
                "append_rich_messages_via_clipboard: hidden doc ready messages=%d total_chars=%d",
                len(batch),
                sum(len(c or "") for _, c in batch),
            )
            if _append_hidden_doc_to_control(doc, control, ctx, style_window=style_window, auto_scroll=False):
                any_inserted = True
                nudge_rich_control_view_to_end(control, ctx=ctx, style_window=style_window)
            else:
                log.error("append_rich_messages_via_clipboard: batch insert into control failed")
        except Exception:
            log.exception("append_rich_messages_via_clipboard batch failed")
        finally:
            if doc is not None:
                try:
                    doc.close(True)
                except Exception:
                    pass
    if any_inserted and items[-1][0] == "user":
        _ensure_trailing_line_break(control)


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


def append_rich_text_via_clipboard(
    ctx,
    control,
    text,
    role="assistant",
    style_window=None,
    auto_scroll=True,
    on_after_insert=None,
):
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
        append_rich_text(doc, text, role=role, style_window=style_window)
        log.debug("append_rich_text_via_clipboard: hidden doc ready len=%d role=%s", len(text), role)
        inserted = False
        if _copy_formatted_from_hidden_doc_to_control(
            doc, control, ctx, role=role, style_window=style_window, auto_scroll=auto_scroll,
        ):
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
            if callable(on_after_insert):
                try:
                    on_after_insert(get_control_text_length(control))
                except Exception:
                    log.exception("append_rich_text_via_clipboard: on_after_insert failed")
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
