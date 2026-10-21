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
"""Hidden Writer HTML import and direct copy into the sidebar RichTextControl.

``session.messages`` is the transcript. ``paint_message_items`` builds a fresh
hidden Writer from that list and replaces the control with the paint. Clear,
Stop, and a new chunk change the list, then the control is drawn again. The
hidden doc is not spliced in place: copy and paste still read it, and a failed
edit does not leave a partial copy.

Pipeline: create_hidden_html_writer → render the message list → direct portion
copy into the control, replacing what was there. A failed copy clears the
control and writes the same list as plain text. There is no transferable,
SystemClipboard, or Ctrl+V step.

The direct-copy path walks Writer body enumeration (paragraphs and TextTables) and inserts
via insertString on the form TextField model. RichTextControl is EditEngine — no table grid —
so TextTables are flattened to tab-separated rows with ParaTabStops at the max
column width (EditEngine has no table grid). EditEngine paste does not preserve Writer
NumberingRules, so list bullets and ordered numbers are reconstructed manually — see
_list_prefix_for_paragraph.
"""

from __future__ import annotations

import logging
import unicodedata
from typing import Any, Sequence, cast

from plugin.chatbot.rich_text import (
    CHAT_FONT_HEIGHT,
    CHAT_FONT_NAME,
    CHAT_FONT_WEIGHT,
    ChatTheme,
    configure_hidden_writer_for_chat,
    _role_prefix,
    _span_matches_message,
    append_rich_text,
    contains_html_tag,
    render_messages_to_hidden_doc,
    strip_legacy_ai_label,
)
from plugin.chatbot.rich_text_control import (
    _apply_sidebar_para_margins,
    _insert_string_at_rich_cursor,
    _is_automatic_char_color,
    clear_control,
    get_control_text_length,
    truncate_control_from,
    _scroll_rich_to_tail,
    log_rich_scroll,
)
from plugin.calc.navigation import (
    drop_cell_link_spans_from,
    extract_cell_links_from_html,
    normalize_cell_address,
    portion_cell_href,
    portion_looks_like_cell_link,
    register_cell_link_span,
    render_calc_cell_refs,
)
from plugin.framework.errors import best_effort, uno_probe
from plugin.framework.i18n import _
from plugin.framework.uno_context import focus_preserved, process_events_to_idle

log = logging.getLogger(__name__)

# Rich-sidebar formatted-insert fallback diagnostics use WARNING so release builds
# (default log_level=WARN) capture direct-copy failures and clipboard fallbacks.

_SERIF_FONT_MARKERS = ("serif", "times", "roman", "courier", "mono")


def _cell_links_for_rows(rows: Sequence[tuple[str, str]]) -> list[tuple[str, str]]:
    links: list[tuple[str, str]] = []
    for _role, content in rows:
        rendered = render_calc_cell_refs(content) if content else content
        links.extend(extract_cell_links_from_html(rendered or ""))
    return links


def create_hidden_html_writer(ctx: Any) -> Any | None:
    """Load a hidden Writer document for HTML import + direct copy."""
    try:
        from plugin.framework.uno_context import new_blank_writer

        return new_blank_writer(ctx)
    except Exception:
        log.exception("create_hidden_html_writer failed")
        return None


def _is_writer_text_table(element: Any) -> bool:
    """True for a Writer XTextTable in body enumeration (not a paragraph)."""
    return bool(element.supportsService("com.sun.star.text.TextTable"))


def _text_table_cell_rows(table: Any) -> list[list[str]]:
    """Cell strings for a Writer TextTable, row-major."""
    n_rows = int(table.getRows().getCount())
    n_cols = int(table.getColumns().getCount())
    rows: list[list[str]] = []

    def _cell(c: int, r: int) -> str:
        return uno_probe(lambda: table.getCellByPosition(c, r).getString() or "", "")

    for row_idx in range(n_rows):
        cells: list[str] = [_cell(col_idx, row_idx) for col_idx in range(n_cols)]
        rows.append(cells)
    return rows


def _flatten_text_table_rows(table: Any) -> list[str]:
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
            cat = unicodedata.category(ch)
            if cat in ("Mn", "Cf"):
                continue
            eaw = unicodedata.east_asian_width(ch)
            if eaw in ("W", "F"):
                adv += 1.0
            else:
                adv += _FALLBACK_EM
    if bold:
        adv *= _BOLD_EM
    return adv


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


def _apply_table_row_vpad(cursor: Any, *, top: int = 0, bottom: int = 0) -> None:
    if cursor is None:
        return

    def _apply() -> None:
        cursor.ParaTopMargin = int(top)
        cursor.ParaBottomMargin = int(bottom)

    best_effort(_apply, "_apply_table_row_vpad")


def _apply_table_tab_stops(cursor: Any, positions_twips: Any) -> None:
    if cursor is None or not positions_twips:
        return

    def _apply() -> None:
        import uno

        stops = []
        for pos in positions_twips:
            ts = cast("Any", uno.createUnoStruct("com.sun.star.style.TabStop"))
            ts.Position = int(pos)
            ts.Alignment = 0  # com.sun.star.style.TabAlign.LEFT
            ts.DecimalChar = "."
            ts.FillChar = " "
            stops.append(ts)
        cursor.ParaTabStops = tuple(stops)

    best_effort(_apply, "_apply_table_tab_stops")


def _utf16_units(text: str) -> int:
    return len((text or "").encode("utf-16-le")) // 2


def _role_from_portion_text(text: str) -> str | None:
    stripped = (text or "").strip()
    if stripped == "You:":
        return "user"
    # Written prefix is _("Assistant:"); also accept the English msgid so a
    # portion copied before translation still picks the assistant color.
    if stripped in ("Assistant:", _("Assistant:")):
        return "assistant"
    return None


def _role_color_for_text(text: str, user_color: int, assistant_color: int, default_role: str = "assistant") -> int:
    role = _role_from_portion_text(text) or default_role
    return user_color if role == "user" else assistant_color


def _resolve_portion_char_color(src_portion: Any, txt: str, user_color: int, assistant_color: int, default_role: str = "assistant") -> int:
    raw = getattr(src_portion, "CharColor", None)
    if isinstance(raw, int) and not _is_automatic_char_color(raw):
        return raw
    return _role_color_for_text(txt, user_color, assistant_color, default_role)


def _normalize_portion_font(portion: Any) -> None:
    """Clamp hidden-doc portions to sidebar sans 10pt (HTML import often uses serif headings)."""
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




def _list_prefix_for_paragraph(para: Any, order_counters: dict[Any, int]) -> str:
    """Bullet or number prefix for a Writer list paragraph.

    RichTextControl's EditEngine does not preserve Writer NumberingRules on insertString
    paste/copy, so ordered lists and bullets would disappear without manual prefix text.
    We read NumberingRules from the hidden Writer doc and emit literal prefix strings
    (e.g. ``• `` or ``1. ``) before each list paragraph's portions.
    """
    is_number = bool(para.getPropertyValue("NumberingIsNumber"))
    if not is_number:
        return ""

    level = int(para.getPropertyValue("NumberingLevel") or 0)
    list_id = para.getPropertyValue("ListId")
    key = (list_id, level)
    indent = "  " * max(0, level)

    # Writer's own label honors <ol start="11"> and letter/roman types.
    try:
        label = para.getPropertyValue("ListLabelString")
    except Exception:
        label = None
    if isinstance(label, str) and label.strip():
        return "%s%s " % (indent, label.strip())

    bullet_char = None
    num_type = None
    rules = para.getPropertyValue("NumberingRules")
    if rules is not None:
        props = list(rules.getByIndex(level))
        for p in props:
            if p.Name == "BulletChar" and p.Value:
                ch = p.Value
                bullet_char = ch if isinstance(ch, str) else str(ch)
            if p.Name == "NumberingType":
                num_type = p.Value

    try:
        n_type = int(num_type) if num_type is not None else None
    except (TypeError, ValueError):
        n_type = None

    # com.sun.star.style.NumberingType: BULLET=6, BITMAP=8.
    if n_type in (6, 8) or (n_type is None and bullet_char):
        ch = (bullet_char or "\u2022").strip()
        if ch and not ch.endswith(" "):
            ch = ch + " "
        return indent + ch

    order_counters[key] = order_counters.get(key, 0) + 1
    return "%s%d. " % (indent, order_counters[key])


def _rich_control_bg_color(model: Any, style_window: Any = None) -> int:
    """Theme fill color for the control (matches sidebar dialog chrome)."""
    bg = getattr(model, "BackgroundColor", None)
    if isinstance(bg, int):
        return bg

    theme = ChatTheme.resolve(style_window=style_window)
    return theme.bg_color


def _apply_cursor_char_props(dest_cursor: Any, src_portion: Any, char_color: Any = None, bg_color: int | None = None) -> None:
    """Copy character formatting from a Writer text portion onto a RichText cursor."""
    for prop in (
        "CharWeight",
        "CharPosture",
        "CharUnderline",
        "CharHeight",
        "CharFontName",
        "CharUnderlineColor",
    ):

        def _copy(p: str = prop) -> None:
            setattr(dest_cursor, p, getattr(src_portion, p))

        uno_probe(_copy, None)
    resolved = char_color
    if resolved is None:
        raw = getattr(src_portion, "CharColor", None)
        if not _is_automatic_char_color(raw):
            resolved = raw
    if resolved is not None and not _is_automatic_char_color(resolved):
        dest_cursor.CharColor = resolved
    if bg_color is not None:
        dest_cursor.CharBackColor = bg_color


# Streamed tokens live here until the committed assistant message replaces the row.
# Not a history write. add_assistant_message pops a trailing row with this key.
OPEN_TRANSCRIPT = "_open_transcript"


def fold_transcript_chunk(session: Any, text: str, role: str = "assistant") -> bool:
    """Record *text* on ``session.messages``. The control is painted afterwards.

    User rows are already stored by the send path. Assistant tokens grow one
    open row until the committed message replaces it. The stop line is a
    separate message the turn writes when it closes, not a chunk matched by
    text. Returns True when the list changed.
    """
    messages = getattr(session, "messages", None)
    if not isinstance(messages, list):
        return False
    if role == "user":
        # A user row already in session.messages (add_user_message at send
        # time) is still a paint. Returning False makes panel._paint_from_list
        # skip the session, so 'You:' waits for the first stream chunk and a
        # Stop before that token draws '[Stopped by user]' under the greeting.
        # Return True without appending again.
        if not text or not str(text).strip():
            return False
        if messages and messages[-1].get("role") == "user":
            return True
        messages.append({"role": "user", "content": text})
        return True
    if not text or not str(text).strip():
        return False
    if messages and messages[-1].get(OPEN_TRANSCRIPT):
        messages[-1]["content"] = (messages[-1].get("content") or "") + text
        return True
    messages.append({"role": "assistant", "content": text, OPEN_TRANSCRIPT: True})
    return True


def _visible_message_text(content: Any) -> str:
    """Text the paint can import. A multimodal user row keeps its text parts."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for part in content:
            if isinstance(part, str):
                parts.append(part)
            elif isinstance(part, dict) and isinstance(part.get("text"), str):
                parts.append(part["text"])
        return "\n".join(part for part in parts if part)
    if content is None:
        return ""
    return str(content)


def plain_transcript_text(session: Any, greeting: str = "") -> str:
    """Plain sidebar text for ``session.messages``. The control is this string."""
    from plugin.framework.i18n import _

    # The plain box is a text field. Panel wiring paints history before the
    # rich control is ready, and writing stored HTML answers verbatim shows
    # "Assistant <p>done</p>" after File > Reload until the rich control
    # takes over. Give it the same stripped text the stream stripper shows.
    text = (_plain_fallback_text(greeting) + "\n") if greeting else ""
    messages = getattr(session, "messages", None)
    if not isinstance(messages, list):
        return text
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        role = msg.get("role", "")
        content = _plain_fallback_text(_visible_message_text(msg.get("content", "")))
        if role == "user":
            text += "\nYou: %s\n" % content
        elif role == "assistant":
            if content:
                text += "\n%s %s" % (_("Assistant:"), content)
            elif msg.get("tool_calls"):
                text += "\n%s [Thinking...]" % _("Assistant:")
            text += "\n"
    return text


def session_history_items(session: Any, greeting: str = "") -> list[tuple[str, str]]:
    """Build (role, content) pairs for session history display (skips system messages)."""
    # Tool-call lines ("[Running tool: ...]", "[tool: result]") are not restored:
    # they are live status text only. ChatSession.add_assistant_message writes
    # only non-empty content to history_db and add_tool_result writes nothing
    # (panel.py), so a reloaded session has no rows to draw them from.
    items: list[tuple[str, str]] = []
    if greeting:
        items.append(("assistant", greeting))
    for msg in session.messages:
        role = msg.get("role", "")
        content = msg.get("content", "")
        if role == "user":
            items.append(("user", _visible_message_text(content)))
        elif role == "assistant":
            if content:
                text = _visible_message_text(content)
                if text.strip() == _STOP_BANNER:
                    _append_stop_banner(items, text)
                else:
                    items.append(("assistant", text))
            elif msg.get("tool_calls"):
                items.append(("assistant", "[Thinking...]"))
    return items


# Body of tool_loop_actions._STOP_LINE ("\n[Stopped by user]\n"), stored by
# close_stopped as its own assistant message.
_STOP_BANNER = "[Stopped by user]"


def _append_stop_banner(items: list[tuple[str, str]], text: str) -> None:
    """Show the stop line as the last paragraph of the answer it stopped.

    The stop line is a separate assistant message whose content is
    "\n[Stopped by user]\n". Painted as its own row it gives a bold
    "Assistant:" label with nothing after it, the banner on the next line,
    and a double blank gap before the next "You:" row (every full repaint of a
    stopped turn, and the turn-end format after a Stop). Display only:
    session.messages and the model context keep the separate message.
    """
    if items and items[-1][0] == "assistant" and items[-1][1].strip():
        prev = items[-1][1]
        looks_html = contains_html_tag(prev)
        joined = prev + ("<p>%s</p>" % _STOP_BANNER if looks_html else "\n\n" + _STOP_BANNER)
        items[-1] = ("assistant", joined)
        return
    # Stop before the first token: no answer to attach to. Keep the row, without
    # the newlines that made the empty label line and the extra gap.
    items.append(("assistant", text.strip()))


def _replace_control_with_plain(
    control: Any,
    items: Any,
    ctx: Any,
    style_window: Any,
    *,
    restore_focus: Any = None,
) -> None:
    """Wipe the control and write every row. The text is the message list."""
    clear_control(control)
    _plain_append_messages(
        control,
        items,
        ctx,
        style_window,
        auto_scroll=True,
        restore_focus=restore_focus,
    )


def rows_need_hidden_writer(rows: Any) -> bool:
    """True when any message content has real HTML tags needing hidden Writer import."""
    return any(contains_html_tag(content or "") for _role, content in (rows or []))


def _force_rich_full_reformat(control: Any) -> None:
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


def paint_message_items(
    ctx: Any,
    control: Any,
    items: Any,
    style_window: Any = None,
    *,
    restore: Any = None,
    restore_focus: Any = None,
) -> bool:
    """Replace the control with a paint of *items*.

    The hidden Writer is created empty and filled from the list. It is copied
    only after every message has been rendered. A bad element does not leave
    tags. A copy that fails halfway is wiped, and the control is written again
    from the same list as plain text, so the partial copy does not remain.
    """
    if control is None:
        return False
    rows = [(role, content) for role, content in (items or [])]
    if not rows:
        clear_control(control)
        return True
    if not rows_need_hidden_writer(rows):
        clear_control(control)
        _plain_append_messages(control, rows, ctx, style_window, auto_scroll=False, restore_focus=restore_focus)
        if rows[-1][0] == "user":
            with focus_preserved(ctx, restore, restore_focus=restore_focus):
                _ensure_trailing_line_break(control)
        _force_rich_full_reformat(control)
        _scroll_rich_to_tail(control, ctx, restore_focus=restore_focus)
        return True
    doc = None
    try:
        doc = create_hidden_html_writer(ctx)
        if doc is None:
            log.warning("paint_message_items: hidden Writer unavailable")
            _replace_control_with_plain(control, rows, ctx, style_window, restore_focus=restore_focus)
            return True
        configure_hidden_writer_for_chat(doc)
        render_messages_to_hidden_doc(doc, rows, style_window=style_window)
        if contains_html_tag(doc.getText().getString() or ""):
            # render_messages_to_hidden_doc already reverts a bad element.
            # If a tag is still in the body, do not copy that document.
            log.warning("paint_message_items: hidden doc still has tags; plain paint")
            _replace_control_with_plain(control, rows, ctx, style_window, restore_focus=restore_focus)
            return True
        links = _cell_links_for_rows(rows)
        # Replace, do not append. A previous splice is not part of this paint.
        clear_control(control)
        res = _copy_formatted_from_hidden_doc_to_control(
            doc,
            control,
            ctx,
            role="assistant",
            style_window=style_window,
            auto_scroll=False,
            cell_link_targets=links,
            restore=restore,
            restore_focus=restore_focus,
        )
        ok, copy_reason = res if isinstance(res, tuple) else (bool(res), None)
        if ok:
            if rows[-1][0] == "user":
                with focus_preserved(ctx, restore, restore_focus=restore_focus):
                    _ensure_trailing_line_break(control)
            # Reformat before the restick so the scroll target is the real end.
            _force_rich_full_reformat(control)
            _scroll_rich_to_tail(control, ctx, restore_focus=restore_focus)
            return True
        log.warning("paint_message_items: formatted copy failed reason=%s; plain paint", copy_reason)
        _replace_control_with_plain(control, rows, ctx, style_window, restore_focus=restore_focus)
        return True
    except Exception:
        log.exception("paint_message_items failed")
        _replace_control_with_plain(control, rows, ctx, style_window, restore_focus=restore_focus)
        return True
    finally:
        _close_hidden_doc(doc, "paint_message_items")


def _close_hidden_doc(doc: Any, context: str) -> None:
    if doc is not None:
        try:
            doc.close(True)
        except Exception:
            log.debug("%s: hidden writer close failed", context, exc_info=True)


def _copy_formatted_from_hidden_doc_to_control(
    src_doc: Any,
    control: Any,
    ctx: Any,
    role: str = "assistant",
    style_window: Any = None,
    auto_scroll: bool = True,
    cell_link_targets: list[tuple[str, str]] | None = None,
    *,
    restore: Any = None,
    restore_focus: Any = None,
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
    element_skipped = False

    def _do_copy() -> None:
        nonlocal inserted, copy_failed_with_exception, element_skipped
        try:
            if auto_scroll:
                process_events_to_idle(ctx)
            theme = ChatTheme.resolve(style_window=style_window)
            current_role = role
            default_color = theme.user_color if current_role == "user" else theme.assistant_color

            dest_cursor = model.createTextCursor()
            dest_cursor.gotoEnd(False)
            _apply_sidebar_para_margins(dest_cursor)
            fill_color = _rich_control_bg_color(model, style_window=style_window)

            control_len = get_control_text_length(control)

            src_text = src_doc.getText()
            para_enum = src_text.createEnumeration()
            first_para = True
            order_counters: dict[Any, int] = {}
            pending_links = list(cell_link_targets or [])
            pending_link_idx = 0
            while para_enum.hasMoreElements():
                para = para_enum.nextElement()
                element_inserted = False
                try:
                    if not first_para:
                        _insert_string_at_rich_cursor(model, dest_cursor, "\n")
                        if control_len is not None:
                            control_len += _utf16_units("\n")
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
                                if control_len is not None:
                                    control_len += _utf16_units("\n")
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
                            table_text = "\t".join(cells)
                            _insert_string_at_rich_cursor(
                                model,
                                dest_cursor,
                                table_text,
                                default_color,
                                bold=is_header,
                                underline=is_header,
                            )
                            if control_len is not None:
                                control_len += _utf16_units(table_text)
                            dest_cursor.gotoEnd(False)
                            inserted = True
                            element_inserted = True
                        continue

                    line_prefix = _list_prefix_for_paragraph(para, order_counters)
                    prefix_inserted = not line_prefix
                    portion_enum = para.createEnumeration()
                    while portion_enum.hasMoreElements():
                        portion = portion_enum.nextElement()
                        txt = portion.getString()
                        if not txt:
                            continue
                        detected = _role_from_portion_text(txt)
                        if detected is not None:
                            current_role = detected
                            default_color = theme.user_color if current_role == "user" else theme.assistant_color
                        if line_prefix and not prefix_inserted:
                            # Force normal: right after "Assistant: " the number
                            # took the label's bold (EditEngine sticky attrs).
                            _insert_string_at_rich_cursor(
                                model, dest_cursor, line_prefix, default_color, bold=False, underline=False
                            )
                            if control_len is not None:
                                control_len += _utf16_units(line_prefix)
                            dest_cursor.gotoEnd(False)
                            prefix_inserted = True
                            element_inserted = True
                        portion_color = _resolve_portion_char_color(
                            portion, txt, theme.user_color, theme.assistant_color, current_role
                        )
                        _normalize_portion_font(portion)
                        _apply_cursor_char_props(dest_cursor, portion, char_color=portion_color, bg_color=fill_color)
                        href = portion_cell_href(portion)
                        is_pending = bool(pending_links and pending_link_idx < len(pending_links) and pending_links[pending_link_idx][0] == txt)
                        maybe_link = bool(href or is_pending or portion_looks_like_cell_link(portion, txt))
                        before_len = control_len
                        txt_units = _utf16_units(txt)
                        _insert_string_at_rich_cursor(model, dest_cursor, txt, portion_color)
                        if control_len is not None:
                            control_len += txt_units
                        element_inserted = True
                        if maybe_link and before_len is not None:
                            after_len = control_len
                            addr = normalize_cell_address(href) if href else None
                            if is_pending:
                                # Pop the matched queue head even when href supplied the address.
                                # An href used to leave the queue head, so the next same label reused it.
                                pending_addr = pending_links[pending_link_idx][1]
                                pending_link_idx += 1
                                if not addr:
                                    addr = pending_addr
                            elif not addr and portion_looks_like_cell_link(portion, txt):
                                addr = normalize_cell_address(txt.strip())
                            if addr and isinstance(before_len, int) and isinstance(after_len, int):
                                register_cell_link_span(control, before_len, after_len, addr)
                        dest_cursor.gotoEnd(False)
                        inserted = True
                    if line_prefix and not prefix_inserted:
                        _insert_string_at_rich_cursor(
                            model, dest_cursor, line_prefix, default_color, bold=False, underline=False
                        )
                        if control_len is not None:
                            control_len += _utf16_units(line_prefix)
                        inserted = True
                        element_inserted = True
                except Exception as exc:
                    # Resync length on error: failed insert or post-formatting may have written text.
                    control_len = get_control_text_length(control)
                    if element_inserted:
                        # A partly copied element means the formatted copy is not
                        # complete; reporting success would make rerender drop the plain tail.
                        element_skipped = True
                        log.exception(
                            "_copy_formatted_from_hidden_doc_to_control: skip element role=%s",
                            role,
                        )
                        break
                    else:
                        log.warning(
                            "_copy_formatted_from_hidden_doc_to_control: element failed before insert, trying visible text role=%s: %s",
                            role,
                            exc,
                        )
                        try:
                            if _is_writer_text_table(para):
                                fallback_text = "\n".join(_flatten_text_table_rows(para))
                            else:
                                fallback_text = para.getString()
                            control_len = get_control_text_length(control)
                            if fallback_text:
                                _insert_string_at_rich_cursor(model, dest_cursor, fallback_text, default_color)
                                if control_len is not None:
                                    control_len += _utf16_units(fallback_text)
                                dest_cursor.gotoEnd(False)
                                inserted = True
                        except Exception:
                            control_len = get_control_text_length(control)
                            element_skipped = True
                            log.exception(
                                "_copy_formatted_from_hidden_doc_to_control: fallback element string failed role=%s",
                                role,
                            )
                            break

            if inserted and not element_skipped:
                if auto_scroll:
                    _scroll_rich_to_tail(control, ctx, restore_focus=restore_focus)
                log_rich_scroll("copy_done", control=control, role=role, auto_scroll=int(auto_scroll))
                log.info(
                    "_copy_formatted_from_hidden_doc_to_control: ok role=%s",
                    role,
                )
        except Exception:
            log.exception("_copy_formatted_from_hidden_doc_to_control failed role=%s", role)
            copy_failed_with_exception = True

    if ctx is not None:
        with focus_preserved(ctx, restore, restore_focus=restore_focus):
            _do_copy()
    else:
        _do_copy()
    if inserted and not element_skipped and not copy_failed_with_exception:
        return True, None
    reason = "element_skipped" if element_skipped else (
        "exception" if copy_failed_with_exception else "no_content_inserted"
    )
    log.warning("_copy_formatted_from_hidden_doc_to_control: failed reason=%s role=%s", reason, role)
    return False, reason


def _plain_fallback_text(text: str) -> str:
    """Visible text when the hidden Writer that formats HTML is missing.

    A missing Writer must still insert the message, or the sidebar skips
    it. A later regex leaves ``<script>`` / ``<style>`` bodies and disagrees
    with the chat stream stripper. Use that stripper (it also unescapes
    entities).
    """
    from plugin.framework.html_stripper import strip_html_tags

    return strip_html_tags(text or "")


def _rollback_rich_insert(control: Any, before: int | None) -> None:
    """Undo a separator or partial copy that did not finish as a full insert.

    The separator (and any partial copy) must not stay in the control
    when the formatted insert returns false, and cell-link spans recorded
    for that tail must not resolve clicks into deleted text.
    A length of None means the control length could not be read. Truncate
    treats 0 as "delete from the start", so an unknown length must not roll
    back.
    """
    if before is None:
        return
    truncate_control_from(control, before)
    drop_cell_link_spans_from(control, before)


def _plain_role_prefix(role: str) -> str:
    """Same ``You:`` / ``Assistant:`` label ``append_rich_text`` writes on the formatted path."""
    return _role_prefix(role)


def _row_separator_suffix(text: str | None) -> str:
    """Return the newline suffix needed to leave one blank line after *text*."""
    # The separator used to append \n\n after a single trailing newline,
    # so the gap became two blank lines.
    if not text or not text.strip():
        return ""
    if text.endswith("\n\n"):
        return ""
    if text.endswith("\n"):
        return "\n"
    return "\n\n"


def _plain_append_messages(control: Any, batch: Any, ctx: Any, style_window: Any, auto_scroll: bool = False, *, restore_focus: Any = None) -> bool:
    """Plain transcript rows with the formatted path's role label, color, and gap."""
    model = control.getModel() if control else None
    if model is None or not hasattr(model, "createTextCursor"):
        return False

    wrote = False
    theme = ChatTheme.resolve(style_window=style_window)
    last_text = model.Text or ""

    cursor = model.createTextCursor()
    cursor.gotoEnd(False)

    for role, content in batch:
        plain = _plain_fallback_text(content or "")
        if not plain.strip():
            continue
        sep = _row_separator_suffix(last_text)
        if sep:
            _insert_string_at_rich_cursor(model, cursor, sep)
            cursor.gotoEnd(False)
        _apply_sidebar_para_margins(cursor)

        color = theme.user_color if role == "user" else theme.assistant_color
        prefix = _plain_role_prefix(role)
        _insert_string_at_rich_cursor(model, cursor, prefix, char_color=color, bold=True)
        cursor.gotoEnd(False)
        _apply_sidebar_para_margins(cursor)

        _insert_string_at_rich_cursor(model, cursor, plain, char_color=color, bold=False)
        cursor.gotoEnd(False)
        _apply_sidebar_para_margins(cursor)

        wrote = True
        last_text = plain

    if wrote and auto_scroll:
        _scroll_rich_to_tail(control, ctx, restore_focus=restore_focus)
        process_events_to_idle(ctx)
    return wrote


def append_formatted_rows(
    ctx: Any,
    control: Any,
    items: Any,
    style_window: Any = None,
    *,
    restore: Any = None,
    restore_focus: Any = None,
) -> None:
    """Render many chat messages with minimal UI updates (hidden Writer + direct copy)."""
    if not control or not items:
        return
    rows = list(items)
    before = get_control_text_length(control)
    if before is None:
        log.warning("append_formatted_rows: control length unavailable; plain append")
        _plain_append_messages(control, rows, ctx, style_window, auto_scroll=False, restore_focus=restore_focus)
        if rows[-1][0] == "user":
            _ensure_trailing_line_break(control)
        _scroll_rich_to_tail(control, ctx, restore_focus=restore_focus)
        return

    _ensure_message_separator(control)
    if not rows_need_hidden_writer(rows):
        if _plain_append_messages(control, rows, ctx, style_window, auto_scroll=False, restore_focus=restore_focus):
            if rows[-1][0] == "user":
                _ensure_trailing_line_break(control)
            _scroll_rich_to_tail(control, ctx, restore_focus=restore_focus)
        return

    doc = None
    inserted = False
    try:
        doc = create_hidden_html_writer(ctx)
        if doc is None:
            # Tip: write this batch as plain text; dropping it would lose that history.
            log.warning("append_formatted_rows: hidden Writer unavailable")
            _rollback_rich_insert(control, before)
            if _plain_append_messages(control, rows, ctx, style_window, restore_focus=restore_focus):
                if rows[-1][0] == "user":
                    _ensure_trailing_line_break(control)
                _scroll_rich_to_tail(control, ctx, restore_focus=restore_focus)
            return
        configure_hidden_writer_for_chat(doc)
        batch_links = _cell_links_for_rows(rows)

        render_messages_to_hidden_doc(doc, rows, style_window=style_window)
        log.debug(
            "append_formatted_rows: hidden doc ready messages=%d total_chars=%d",
            len(rows),
            sum(len(c or "") for _unused, c in rows),
        )
        doc_text = doc.getText().getString() or ""
        if contains_html_tag(doc_text):
            log.warning("append_formatted_rows: hidden doc still has tags; plain append")
            _rollback_rich_insert(control, before)
            if _plain_append_messages(control, rows, ctx, style_window, restore_focus=restore_focus):
                if rows[-1][0] == "user":
                    _ensure_trailing_line_break(control)
                _scroll_rich_to_tail(control, ctx, restore_focus=restore_focus)
            return

        res = _copy_formatted_from_hidden_doc_to_control(
            doc,
            control,
            ctx,
            role="assistant",
            style_window=style_window,
            auto_scroll=False,
            cell_link_targets=batch_links,
            restore=restore,
            restore_focus=restore_focus,
        )
        ok, copy_reason = res if isinstance(res, tuple) else (bool(res), None)
        if ok:
            inserted = True
            if rows[-1][0] == "user":
                _ensure_trailing_line_break(control)
            _scroll_rich_to_tail(control, ctx, restore_focus=restore_focus)
        else:
            # Tip: history has no second copy, so the rollback must not lose the rows.
            log.warning(
                "append_formatted_rows: insert into control failed messages=%d reason=%s",
                len(rows),
                copy_reason,
            )
            _rollback_rich_insert(control, before)
            if _plain_append_messages(control, rows, ctx, style_window, restore_focus=restore_focus):
                if rows[-1][0] == "user":
                    _ensure_trailing_line_break(control)
                _scroll_rich_to_tail(control, ctx, restore_focus=restore_focus)
    except Exception:
        log.exception("append_formatted_rows failed")
        if not inserted:
            _rollback_rich_insert(control, before)
            if _plain_append_messages(control, rows, ctx, style_window, restore_focus=restore_focus):
                if rows[-1][0] == "user":
                    _ensure_trailing_line_break(control)
                _scroll_rich_to_tail(control, ctx, restore_focus=restore_focus)
    finally:
        _close_hidden_doc(doc, "append_formatted_rows")


def _ensure_message_separator(control: Any) -> None:
    """Insert paragraph breaks without assigning model.Text (preserves rich formatting)."""
    def _do_separate() -> None:
        model = control.getModel() if control else None
        if model is None or not hasattr(model, "createTextCursor"):
            return
        text = model.Text or ""
        suffix = _row_separator_suffix(text)
        if not suffix:
            return
        cursor = model.createTextCursor()
        cursor.gotoEnd(False)
        _insert_string_at_rich_cursor(model, cursor, suffix)
        log_rich_scroll("separator", control=control)

    best_effort(_do_separate, "_ensure_message_separator")


def _ensure_trailing_line_break(control: Any) -> None:
    """Leave a blank line after the user message before assistant streaming.

    Formatted copy from Writer often has no trailing ``\\n``; a single ``\\n`` only moves to
    the next line. We want ``\\n\\n`` (same gap as ``_ensure_message_separator``).
    """
    def _do_break() -> None:
        model = control.getModel() if control else None
        if model is None or not hasattr(model, "createTextCursor"):
            return
        text = model.Text or ""
        suffix = _row_separator_suffix(text)
        if not suffix:
            return
        cursor = model.createTextCursor()
        cursor.gotoEnd(False)
        _insert_string_at_rich_cursor(model, cursor, suffix)
        log_rich_scroll("trailing_break", control=control, suffix_len=len(suffix))

    best_effort(_do_break, "_ensure_trailing_line_break")


def append_formatted_row(
    ctx: Any,
    control: Any,
    text: str,
    role: str = "assistant",
    style_window: Any = None,
    auto_scroll: bool = True,
    on_after_insert: Any = None,
    *,
    restore: Any = None,
    restore_focus: Any = None,
) -> bool:
    """Import HTML in a hidden Writer doc and copy formatted content directly into the RichText control.

    Returns True for a full formatted insert, or for the plain-text fallback
    when the hidden Writer is missing or content is plain text. False when a later
    body element was skipped, so a caller that already cut plain text can put it back.
    """
    if not control or not text or not text.strip():
        return False
    if role == "assistant":
        text = strip_legacy_ai_label(text)
    text = render_calc_cell_refs(text)
    cell_link_targets = extract_cell_links_from_html(text)

    if not rows_need_hidden_writer([(role, text)]):
        _ensure_message_separator(control)
        _plain_append_messages(
            control, [(role, text)], ctx, style_window, auto_scroll=False, restore_focus=restore_focus,
        )
        if role == "user":
            with focus_preserved(ctx, restore, restore_focus=restore_focus):
                _ensure_trailing_line_break(control)
            if auto_scroll:
                _scroll_rich_to_tail(control, ctx, restore_focus=restore_focus)
            if callable(on_after_insert):
                try:
                    on_after_insert(get_control_text_length(control))
                except Exception:
                    log.exception("append_formatted_row: on_after_insert failed")
            log_rich_scroll("user_append_done", control=control, role=role)
        elif auto_scroll:
            _scroll_rich_to_tail(control, ctx, restore_focus=restore_focus)
        return True

    # Remember the length before the separator. A failed copy used to leave
    # that blank gap, and a missing Writer returned False so the message
    # never appeared. Roll back, then plain-append only when there is no
    # Writer: a False copy still lets the caller restore its own tail.
    before = get_control_text_length(control)
    if before is None:
        log.warning("append_formatted_row: control length unavailable")
        _plain_append_messages(
            control, [(role, text)], ctx, style_window, auto_scroll=auto_scroll, restore_focus=restore_focus,
        )
        return True
    _ensure_message_separator(control)
    doc = None
    inserted = False
    try:
        doc = create_hidden_html_writer(ctx)
        if doc is None:
            log.warning("append_formatted_row: hidden Writer unavailable")
            _rollback_rich_insert(control, before)
            _plain_append_messages(
                control, [(role, text)], ctx, style_window, auto_scroll=auto_scroll, restore_focus=restore_focus,
            )
            return True
        configure_hidden_writer_for_chat(doc)
        if append_rich_text(doc, text, role=role, style_window=style_window) is False:
            # Filter failed. The hidden doc has the role prefix and not the body.
            # Copying it would show the prefix alone, or the raw tags if they
            # had been inserted. Roll back and write the stripped message.
            log.warning("append_formatted_row: HTML import failed role=%s", role)
            _rollback_rich_insert(control, before)
            _plain_append_messages(
                control, [(role, text)], ctx, style_window, auto_scroll=auto_scroll, restore_focus=restore_focus,
            )
            return True
        span = uno_probe(lambda: doc.getText().getString(), None)
        if isinstance(span, str) and not _span_matches_message(span, role, text):
            log.warning("append_formatted_row: imported span does not match message role=%s", role)
            _rollback_rich_insert(control, before)
            _plain_append_messages(
                control, [(role, text)], ctx, style_window, auto_scroll=auto_scroll, restore_focus=restore_focus,
            )
            return True
        log.debug("append_formatted_row: hidden doc ready len=%d role=%s", len(text), role)
        ok, direct_reason = _copy_formatted_from_hidden_doc_to_control(
            doc,
            control,
            ctx,
            role=role,
            style_window=style_window,
            auto_scroll=False,
            cell_link_targets=cell_link_targets,
            restore=restore,
            restore_focus=restore_focus,
        )
        if ok:
            inserted = True
            log.info(
                "append_formatted_row: insert ok via=direct_copy role=%s",
                role,
            )
        else:
            log.warning(
                "append_formatted_row: formatted copy failed direct_copy_reason=%s role=%s",
                direct_reason,
                role,
            )
            _rollback_rich_insert(control, before)
        if inserted and role == "user":
            with focus_preserved(ctx, restore, restore_focus=restore_focus):
                _ensure_trailing_line_break(control)
            if auto_scroll:
                # Do not reveal_caret. That setFocus GetFocus-es the viewport,
                # which switches EESelectionMode to Std while SelectAll is still
                # covering the whole control (user-message flash). Agent stream
                # never reveal_caret, so it does not flash.
                _scroll_rich_to_tail(control, ctx, restore_focus=restore_focus)
            if callable(on_after_insert):
                try:
                    on_after_insert(get_control_text_length(control))
                except Exception:
                    log.exception("append_formatted_row: on_after_insert failed")
            log_rich_scroll("user_append_done", control=control, role=role)
        elif inserted and auto_scroll:
            _scroll_rich_to_tail(control, ctx, restore_focus=restore_focus)
        if inserted:
            return True
    except Exception:
        log.exception("append_formatted_row failed")
        if not inserted:
            _rollback_rich_insert(control, before)
    finally:
        _close_hidden_doc(doc, "append_formatted_row")
    return False
