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

The direct-copy path walks Writer body enumeration (paragraphs and TextTables) and inserts
via insertString on the form TextField model. RichTextControl is EditEngine — no table grid —
so TextTables are flattened to tab-separated rows with ParaTabStops at the max
column width (EditEngine has no table grid). EditEngine paste does not preserve Writer
NumberingRules, so list bullets and ordered numbers are reconstructed manually — see
_list_prefix_for_paragraph.
"""

from __future__ import annotations

import html
import logging

from plugin.chatbot.rich_text import (
    CHAT_FONT_HEIGHT,
    CHAT_FONT_NAME,
    CHAT_FONT_WEIGHT,
    ChatTheme,
    configure_hidden_writer_for_chat,
    _HTML_TAG_RE,
    append_rich_text,
    strip_legacy_ai_label,
)
from plugin.chatbot.rich_text_control import (
    HISTORY_RENDER_BATCH_CHARS,
    _apply_sidebar_para_margins,
    _insert_string_at_rich_cursor,
    _is_automatic_char_color,
    get_control_text_length,
    _scroll_rich_to_tail,
    log_rich_scroll,
)
from plugin.calc.navigation import (
    extract_cell_links_from_html,
    normalize_cell_address,
    portion_cell_href,
    portion_looks_like_cell_link,
    register_cell_link_span,
)
from plugin.framework.uno_context import focus_preserved, process_events_to_idle

log = logging.getLogger(__name__)

# Rich-sidebar formatted-insert fallback diagnostics use WARNING so release builds
# (default log_level=WARN) capture direct-copy failures and clipboard fallbacks.

_SERIF_FONT_MARKERS = ("serif", "times", "roman", "courier", "mono")


def build_message_html(text: str, role: str = "assistant") -> str:
    """Wrap chat message body as HTML with a bold role prefix."""
    if not text or not text.strip():
        return ""
    from plugin.calc.navigation import render_calc_cell_refs

    text = render_calc_cell_refs(text)
    label = "You:" if role == "user" else "Assistant:"
    if _HTML_TAG_RE.search(text):
        body = text
    else:
        body = "<p>%s</p>" % html.escape(text)
    return "<p><strong>%s</strong></p>%s" % (label, body)


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


def _is_writer_text_table(element) -> bool:
    """True for a Writer XTextTable in body enumeration (not a paragraph)."""
    try:
        # ``is True``: MagicMock.supportsService() is truthy but not the bool True.
        return element.supportsService("com.sun.star.text.TextTable") is True
    except Exception:
        return False


def _text_table_cell_rows(table) -> list[list[str]]:
    """Cell strings for a Writer TextTable, row-major."""
    n_rows = int(table.getRows().getCount())
    n_cols = int(table.getColumns().getCount())
    rows: list[list[str]] = []
    for row_idx in range(n_rows):
        cells: list[str] = []
        for col_idx in range(n_cols):
            try:
                cells.append(table.getCellByPosition(col_idx, row_idx).getString() or "")
            except Exception:
                cells.append("")
        rows.append(cells)
    return rows


def _flatten_text_table_rows(table) -> list[str]:
    """Cell strings as tab-separated rows. EditEngine cannot host a Writer table."""
    return ["\t".join(row) for row in _text_table_cell_rows(table)]


# RichTextControl is EditEngine: ParaTabStops Position is twips (1/20 pt), not
# Writer's 1/100 mm (EE_PARA_TABS has no CONVERT_TWIPS). Column width is the
# longest cell in Liberation Sans ems (ASCII advances from the Regular TTF)
# plus a 0.3em gap — font-relative, so HiDPI and 96dpi match. Header row is
# bold (~1.07x). Fallback 0.55em for non-ASCII.
_TWIPS_PER_PT = 20
_TAB_GAP_EM = 0.30
_BOLD_EM = 1.07
_FALLBACK_EM = 0.55
# ASCII 32-126, advance / em (Liberation Sans Regular, UPM 2048).
_LIBERATION_SANS_EM = (
    0.2778, 0.2778, 0.3550, 0.5562, 0.5562, 0.8892, 0.6670, 0.1909, 0.3330, 0.3330,
    0.3892, 0.5840, 0.2778, 0.3330, 0.2778, 0.2778, 0.5562, 0.5562, 0.5562, 0.5562,
    0.5562, 0.5562, 0.5562, 0.5562, 0.5562, 0.5562, 0.2778, 0.2778, 0.5840, 0.5840,
    0.5840, 0.5562, 1.0151, 0.6670, 0.6670, 0.7222, 0.7222, 0.6670, 0.6108, 0.7778,
    0.7222, 0.2778, 0.5000, 0.6670, 0.5562, 0.8330, 0.7222, 0.7778, 0.6670, 0.7778,
    0.7222, 0.6670, 0.6108, 0.7222, 0.6670, 0.9438, 0.6670, 0.6670, 0.6108, 0.2778,
    0.2778, 0.2778, 0.4692, 0.5562, 0.3330, 0.5562, 0.5562, 0.5000, 0.5562, 0.5562,
    0.2778, 0.5562, 0.5562, 0.2222, 0.2222, 0.5000, 0.2222, 0.8330, 0.5562, 0.5562,
    0.5562, 0.5562, 0.3330, 0.5000, 0.2778, 0.5562, 0.5000, 0.7222, 0.5000, 0.5000,
    0.5000, 0.3340, 0.2598, 0.3340, 0.5840,
)


def _cell_width_em(text: str, *, bold: bool = False) -> float:
    adv = 0.0
    for ch in text or "":
        o = ord(ch)
        if 32 <= o <= 126:
            adv += _LIBERATION_SANS_EM[o - 32]
        else:
            adv += _FALLBACK_EM
    if bold:
        adv *= _BOLD_EM
    return adv


def _max_column_chars(rows: list[list[str]]) -> list[int]:
    n_cols = max((len(row) for row in rows), default=0)
    widths = [0] * n_cols
    for row in rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(cell or ""))
    return widths


def _max_column_ems(rows: list[list[str]]) -> list[float]:
    n_cols = max((len(row) for row in rows), default=0)
    widths = [0.0] * n_cols
    for r_i, row in enumerate(rows):
        bold = r_i == 0
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], _cell_width_em(cell or "", bold=bold))
    return widths


def _column_width_twips(width_em: float) -> int:
    return max(1, int(round((max(0.0, float(width_em)) + _TAB_GAP_EM) * CHAT_FONT_HEIGHT * _TWIPS_PER_PT)))


def _tab_stop_positions_twips(rows: list[list[str]]) -> tuple[int, ...]:
    """Stop after each column except the last, in twips (EditEngine ParaTabStops)."""
    max_ems = _max_column_ems(rows)
    if len(max_ems) < 2:
        return ()
    acc = 0
    stops: list[int] = []
    for width_em in max_ems[:-1]:
        acc += _column_width_twips(width_em)
        stops.append(acc)
    return tuple(stops)


# Above the first table row and below the last. Same 1/100 mm unit as
# CHAT_PARA_SIDE_MARGIN (EditEngine para margins are METRIC_ITEM, not pixels).
# 0.85em of the 10pt chat font ≈ 3mm — a breath, not 1.5mm of hairline.
_TABLE_V_PAD_EM = 0.85
_MM100_PER_PT = 35.28
_TABLE_V_PAD_MM100 = int(round(_TABLE_V_PAD_EM * CHAT_FONT_HEIGHT * _MM100_PER_PT))


def _apply_table_row_vpad(cursor, *, top: int = 0, bottom: int = 0) -> None:
    if cursor is None:
        return
    try:
        cursor.ParaTopMargin = int(top)
        cursor.ParaBottomMargin = int(bottom)
    except Exception as e:
        log.debug("_apply_table_row_vpad failed: %s", e)


def _apply_table_tab_stops(cursor, positions_twips) -> None:
    if cursor is None or not positions_twips:
        return
    try:
        import uno

        stops = []
        for pos in positions_twips:
            ts = uno.createUnoStruct("com.sun.star.style.TabStop")
            ts.Position = int(pos)
            ts.Alignment = 0  # com.sun.star.style.TabAlign.LEFT
            ts.DecimalChar = "."
            ts.FillChar = " "
            stops.append(ts)
        cursor.ParaTabStops = tuple(stops)
    except Exception as e:
        log.debug("_apply_table_tab_stops failed: %s", e)


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
    cell_link_targets: list[tuple[str, str]] | None = None,
) -> tuple[bool, str | None]:
    """Copy formatted Writer body text into the sidebar RichText control (safe — no clipboard/frame paste).

    Returns ``(ok, failure_reason)`` where *failure_reason* is a short tag when *ok* is false.
    """
    model = control.getModel()
    if model is None or not hasattr(model, "createTextCursor"):
        log.warning("_copy_formatted_from_hidden_doc_to_control: failed reason=model_no_createTextCursor role=%s", role)
        return False, "model_no_createTextCursor"

    inserted = False
    copy_failed_with_exception = False

    def _do_copy() -> None:
        nonlocal inserted, copy_failed_with_exception
        try:
            if auto_scroll:
                process_events_to_idle(ctx)
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
            pending_links = list(cell_link_targets or [])
            while para_enum.hasMoreElements():
                para = para_enum.nextElement()
                try:
                    if not first_para:
                        _insert_string_at_rich_cursor(model, dest_cursor, "\n")
                        dest_cursor.gotoEnd(False)
                        _apply_sidebar_para_margins(dest_cursor)
                    first_para = False

                    if _is_writer_text_table(para):
                        # HTML import creates a real TextTable; portion enum throws and used
                        # to abort the whole copy (truncate-then-fail blanked the stream tail).
                        cell_rows = _text_table_cell_rows(para)
                        tab_stops = _tab_stop_positions_twips(cell_rows)
                        for i, cells in enumerate(cell_rows):
                            if i:
                                _insert_string_at_rich_cursor(
                                    model, dest_cursor, "\n", bold=False, underline=False,
                                )
                                dest_cursor.gotoEnd(False)
                                _apply_sidebar_para_margins(dest_cursor)
                            _apply_table_tab_stops(dest_cursor, tab_stops)
                            last = i == len(cell_rows) - 1
                            _apply_table_row_vpad(
                                dest_cursor,
                                top=_TABLE_V_PAD_MM100 if i == 0 else 0,
                                bottom=_TABLE_V_PAD_MM100 if last else 0,
                            )
                            # First row stands in for <th>: no grid, so bold+underline.
                            # Body rows pass False so the inserted range is forced normal
                            # (EditEngine otherwise keeps the header run's attributes).
                            is_header = i == 0
                            _insert_string_at_rich_cursor(
                                model,
                                dest_cursor,
                                "\t".join(cells),
                                default_color,
                                bold=is_header,
                                underline=is_header,
                            )
                            dest_cursor.gotoEnd(False)
                            inserted = True
                        continue

                    line_prefix = _list_prefix_for_paragraph(para, order_counters)
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
                        portion_color = _resolve_portion_char_color(
                            portion, txt, theme.user_color, theme.assistant_color, role
                        )
                        _apply_cursor_char_props(dest_cursor, portion, char_color=portion_color, bg_color=fill_color)
                        _normalize_portion_font(portion)
                        _apply_cursor_char_props(dest_cursor, portion, char_color=portion_color, bg_color=fill_color)
                        before_len = get_control_text_length(control)
                        _insert_string_at_rich_cursor(model, dest_cursor, txt, portion_color)
                        after_len = get_control_text_length(control)
                        href = portion_cell_href(portion)
                        addr = normalize_cell_address(href) if href else None
                        if not addr and pending_links and pending_links[0][0] == txt:
                            addr = pending_links.pop(0)[1]
                        elif not addr and portion_looks_like_cell_link(portion, txt):
                            addr = normalize_cell_address(txt.strip())
                        if addr:
                            register_cell_link_span(control, before_len, after_len, addr)
                        dest_cursor.gotoEnd(False)
                        inserted = True
                    if line_prefix and not prefix_inserted:
                        _insert_string_at_rich_cursor(model, dest_cursor, line_prefix, default_color)
                        inserted = True
                except Exception:
                    log.exception(
                        "_copy_formatted_from_hidden_doc_to_control: skip element role=%s",
                        role,
                    )

            if inserted:
                if auto_scroll:
                    _scroll_rich_to_tail(control, ctx)
                log_rich_scroll("copy_done", control=control, role=role, auto_scroll=int(auto_scroll))
                log.info(
                    "_copy_formatted_from_hidden_doc_to_control: ok control_len=%d role=%s",
                    get_control_text_length(control),
                    role,
                )
        except Exception:
            log.exception("_copy_formatted_from_hidden_doc_to_control failed role=%s", role)
            copy_failed_with_exception = True

    if ctx is not None:
        with focus_preserved(ctx):
            _do_copy()
    else:
        _do_copy()
    if inserted:
        return True, None
    reason = "exception" if copy_failed_with_exception else "no_content_inserted"
    log.warning("_copy_formatted_from_hidden_doc_to_control: failed reason=%s role=%s", reason, role)
    return False, reason


def _append_hidden_doc_to_control(doc, control, ctx, style_window=None, auto_scroll=True, cell_link_targets=None) -> bool:
    """Copy hidden Writer content into the sidebar control via direct copy."""
    ok, _unused = _copy_formatted_from_hidden_doc_to_control(
        doc,
        control,
        ctx,
        role="assistant",
        style_window=style_window,
        auto_scroll=auto_scroll,
        cell_link_targets=cell_link_targets,
    )
    return ok





def append_rich_messages_via_clipboard(
    ctx,
    control,
    items,
    style_window=None,
    batch_chars=HISTORY_RENDER_BATCH_CHARS,
):
    """Render many chat messages with minimal UI updates (batched hidden Writer + direct copy)."""
    if not control or not items:
        return
    any_inserted = False
    batches = list(iter_history_message_batches(items, batch_chars))
    for batch in batches:
        doc = None
        try:
            doc = create_hidden_html_writer(ctx)
            if doc is None:
                log.warning("append_rich_messages_via_clipboard: hidden Writer unavailable")
                return
            configure_hidden_writer_for_chat(doc)
            batch_links: list[tuple[str, str]] = []
            for role, content in batch:
                from plugin.calc.navigation import extract_cell_links_from_html, render_calc_cell_refs

                rendered = render_calc_cell_refs(content) if content else content
                batch_links.extend(extract_cell_links_from_html(rendered or ""))
                append_rich_text(doc, content, role=role, style_window=style_window)
            log.debug(
                "append_rich_messages_via_clipboard: hidden doc ready messages=%d total_chars=%d",
                len(batch),
                sum(len(c or "") for _unused, c in batch),
            )
            if _append_hidden_doc_to_control(
                doc, control, ctx, style_window=style_window, auto_scroll=False, cell_link_targets=batch_links,
            ):
                any_inserted = True
                _scroll_rich_to_tail(control, ctx)
            else:
                log.warning(
                    "append_rich_messages_via_clipboard: batch insert into control failed messages=%d",
                    len(batch),
                )
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
        log_rich_scroll("separator", control=control)
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
        log_rich_scroll("trailing_break", control=control, suffix_len=len(suffix))
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
    """Import HTML in a hidden Writer doc and copy formatted content directly into the RichText control."""
    if not control or not text or not text.strip():
        return
    if role == "assistant":
        text = strip_legacy_ai_label(text)
    from plugin.calc.navigation import render_calc_cell_refs

    text = render_calc_cell_refs(text)
    cell_link_targets = extract_cell_links_from_html(text)
    _ensure_message_separator(control)
    doc = None
    try:
        doc = create_hidden_html_writer(ctx)
        if doc is None:
            log.warning("append_rich_text_via_clipboard: hidden Writer unavailable")
            return
        configure_hidden_writer_for_chat(doc)
        append_rich_text(doc, text, role=role, style_window=style_window)
        log.debug("append_rich_text_via_clipboard: hidden doc ready len=%d role=%s", len(text), role)
        inserted = False
        ok, direct_reason = _copy_formatted_from_hidden_doc_to_control(
            doc,
            control,
            ctx,
            role=role,
            style_window=style_window,
            auto_scroll=auto_scroll,
            cell_link_targets=cell_link_targets,
        )
        if ok:
            inserted = True
            log.info(
                "append_rich_text_via_clipboard: insert ok via=direct_copy control_len=%d role=%s",
                get_control_text_length(control),
                role,
            )
        else:
            log.warning(
                "append_rich_text_via_clipboard: formatted copy failed direct_copy_reason=%s role=%s",
                direct_reason,
                role,
            )
        if inserted and role == "user":
            with focus_preserved(ctx):
                _ensure_trailing_line_break(control)
            if auto_scroll:
                # Do not reveal_caret. That setFocus GetFocus-es the viewport,
                # which switches EESelectionMode to Std while SelectAll is still
                # covering the whole control (user-message flash). Agent stream
                # never reveal_caret, so it does not flash.
                _scroll_rich_to_tail(control, ctx)
            if callable(on_after_insert):
                try:
                    on_after_insert(get_control_text_length(control))
                except Exception:
                    log.exception("append_rich_text_via_clipboard: on_after_insert failed")
            log_rich_scroll("user_append_done", control=control, role=role)
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
