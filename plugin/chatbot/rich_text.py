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
"""Shared rich-text formatting for the RichTextControl sidebar (hidden Writer HTML import).

The hidden Writer document is a paint of the message list. ``render_messages_to_hidden_doc``
fills a fresh document from that list. It does not splice a later message into a
document that already failed partway.
"""

from __future__ import annotations

import logging
import re
from typing import Any, cast

from plugin.framework.appearance import get_theme_colors
from plugin.framework.html_stripper import strip_html_tags, text_has_real_html_tag
from plugin.framework.i18n import _

log = logging.getLogger(__name__)

_GO_RIGHT_CHUNK = 8192

def _go_right(cursor: Any, n: int, expand: bool) -> bool:  # pyright: ignore[reportUnusedFunction]  # used by rich_text_control.truncate_control_from
    """Move or extend *cursor* right by *n* characters (UNO caps the count)."""
    while n > 0:
        step = n if n < _GO_RIGHT_CHUNK else _GO_RIGHT_CHUNK
        if not cursor.goRight(step, expand):
            return False
        n -= step
    return True

_HTML_TAG_RE = re.compile(
    r"<(?:"
    r"p[>\s/]"
    r"|br[\s/>]"
    r"|/h[1-6]"
    r"|ul[\s/>]"
    r"|ol[\s/>]"
    r"|li[\s/>]"
    r"|strong[\s/>]"
    r"|em[\s/>]"
    r"|code[\s/>]"
    r"|pre[\s/>]"
    r"|div[\s/>]"
    r"|table[\s/>]"
    r")",
    re.IGNORECASE,
)

# Legacy plain-sidebar prefix; append_rich_text adds "Assistant:" instead.
_LEGACY_AI_LABEL_RE = re.compile(r"^\s*AI:\s*", re.IGNORECASE)


# Tight list margins for the narrow sidebar transcript (injected via shared HTML import).
_SIDEBAR_LIST_CSS = "ul, ol { margin-left: 0.2cm; padding-left: 0.3cm; }"

# A body that opens with a list is imported behind a one-character paragraph.
# Writer pastes the first imported paragraph into the paragraph at the cursor
# (the one holding "Assistant: "), so the first <li> lost its list attributes.
# The sentinel paragraph takes that merge instead and is deleted afterwards.
_LIST_SENTINEL = "\u200b"
_LEADING_LIST_RE = re.compile(r"^\s*<(?:ol|ul)\b", re.IGNORECASE)
# Models also write a list as paragraphs with typed markers:
# <p>1. ...</p><p>2. ...</p> (gpt-oss-20b). The first one then shared the
# "Assistant:" line and the rest sat below it.
_LEADING_MARKER_P_RE = re.compile(r"^\s*<p\b[^>]*>\s*(?:\d{1,3}[.)]|[\u2022*-])\s", re.IGNORECASE)


def _body_starts_with_list(html: str) -> bool:
    """True when the body opens with ``<ol>``/``<ul>`` or a ``<p>`` that starts with a list marker."""
    text = html or ""
    return bool(_LEADING_LIST_RE.match(text) or _LEADING_MARKER_P_RE.match(text))


def _drop_list_sentinel(text_obj: Any, body_anchor: Any) -> None:
    """Delete the sentinel character the import left right after the prefix.

    Best effort: a sentinel left behind is an invisible character, not a
    reason to drop the formatted message.
    """
    try:
        probe = text_obj.createTextCursorByRange(body_anchor.getStart())
        probe.goRight(1, False)
        probe.goRight(1, True)
        if probe.getString() == _LIST_SENTINEL:
            probe.setString("")
    except Exception:
        log.debug("list sentinel not removed", exc_info=True)

CHAT_FONT_NAME = "Liberation Sans"
CHAT_FONT_HEIGHT = 10.0
CHAT_FONT_WEIGHT = 100.0
# Writer paragraph margins (1/100 mm) — horizontal padding inside RichTextControl EditEngine.
CHAT_PARA_SIDE_MARGIN = 250


def apply_chat_char_props(target: Any, *, bg_color: int | None = None) -> None:
    """Apply sidebar chat Liberation Sans 10pt Char* props to a cursor, portion, or style object."""
    for name, val in (
        ("CharFontName", CHAT_FONT_NAME),
        ("CharFontNameAsian", CHAT_FONT_NAME),
        ("CharFontNameComplex", CHAT_FONT_NAME),
        ("CharHeight", CHAT_FONT_HEIGHT),
        ("CharWeight", CHAT_FONT_WEIGHT),
        ("CharPosture", 0),
    ):
        try:
            setattr(target, name, val)
        except Exception:
            pass
    if bg_color is not None:
        try:
            target.CharBackColor = bg_color
        except Exception:
            pass


def apply_rich_control_para_margins(cursor: Any) -> None:
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


def configure_hidden_writer_for_chat(doc: Any) -> None:
    """Apply sidebar chat defaults on a hidden Writer doc (font, zero margins, no spellcheck)."""
    try:
        import uno

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
                apply_chat_char_props(std_para)
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
        log.debug("configure_hidden_writer_for_chat failed: %s", e)


def strip_legacy_ai_label(text: str) -> str:
    """Remove leading ``AI:`` from greeting/assistant text (avoid ``Assistant: AI:``)."""
    if not text:
        return text
    return _LEGACY_AI_LABEL_RE.sub("", text, count=1)


USER_COLOR = 0x2A6099
ASSISTANT_COLOR = 0x1E293B


class ChatTheme:
    """Encapsulates theme-aware colors derived from StyleSettings."""

    bg_color: int
    user_color: int
    assistant_color: int

    def __init__(self, bg_color: int, user_color: int, assistant_color: int) -> None:
        self.bg_color = bg_color
        self.user_color = user_color
        self.assistant_color = assistant_color

    @classmethod
    def resolve(cls, doc: Any = None, style_window: Any = None) -> "ChatTheme":
        """Factory method to resolve colors from style_window or document frame."""
        bg_color, user_color, assistant_color = get_theme_colors(doc, style_window=style_window)
        return cls(bg_color, user_color, assistant_color)


class HiddenDocHTMLImporter:
    """Encapsulates importing HTML into a document and tightening indents on lists."""

    doc: Any

    def __init__(self, doc: Any) -> None:
        self.doc = doc

    def insert_html_at_cursor(self, cursor: Any, html_fragment: str) -> None:
        """Import an HTML fragment into self.doc at *cursor* using Writer's HTML filter."""
        _insert_html_at_cursor(self.doc, cursor, html_fragment)

    def tighten_list_indent(self, body_range: Any) -> None:
        """Tighten indentation on list paragraphs within *body_range*."""
        _tighten_list_indent(body_range)


def _tighten_list_indent(body_range: Any) -> None:
    """Tighten indentation on list paragraphs within *body_range*.

    The HTML filter imports <ul>/<ol> as indented paragraphs using ParaLeftMargin
    (not Writer's NumberingRules mechanism). This function detects paragraphs with
    non-zero ParaLeftMargin and reduces them to tight values suitable for the
    narrow sidebar.
    """
    import uno
    try:
        enum = body_range.createEnumeration()
    except Exception as e:
        log.debug("_tighten_list_indent: createEnumeration failed: %s", e)
        return

    para_count = 0
    tightened = 0
    processed_levels = set()
    while enum.hasMoreElements():
        para = enum.nextElement()
        para_count += 1
        try:
            if not para.getPropertyValue("NumberingIsNumber"):
                continue
        except Exception:
            continue

        try:
            level = para.getPropertyValue("NumberingLevel")
            list_id = para.getPropertyValue("ListId")
        except Exception:
            continue

        key = (list_id, level)
        if key in processed_levels:
            continue
        processed_levels.add(key)

        try:
            rules = para.getPropertyValue("NumberingRules")
            props = list(rules.getByIndex(level))
            # Read the existing FirstLineOffset so we can position the bullet
            # with a small left gap while preserving the original bullet-to-text spacing
            flo = 0
            for p in props:
                if p.Name == "FirstLineOffset":
                    flo = p.Value
                    break
            for p in props:
                if p.Name == "LeftMargin":
                    log.debug("_tighten_list_indent: level=%d orig LeftMargin=%s text=%r", level, p.Value, para.getString()[:40])
                    p.Value = abs(flo) + 115 + level * 225
            # uno.Any is a pyuno helper; stubs/mypy do not export it as a module attribute.
            uno_any = getattr(uno, "Any")
            any_props = uno_any("[]com.sun.star.beans.PropertyValue", cast("Any", tuple(props)))
            uno.invoke(rules, "replaceByIndex", (level, any_props))
            para.NumberingRules = rules
            tightened += 1
        except Exception as e:
            log.debug("_tighten_list_indent: failed for level %d: %s", level, e)

    log.debug("_tighten_list_indent: scanned %d paragraphs, tightened %d", para_count, tightened)


def _insert_html_at_cursor(doc: Any, cursor: Any, html_fragment: str) -> None:
    """Import an HTML fragment into *doc* at *cursor* using Writer's HTML filter."""
    from plugin.writer.html_import import insert_html_fragment_at_cursor

    insert_html_fragment_at_cursor(cursor, html_fragment, extra_css=_SIDEBAR_LIST_CSS)


def contains_html_tag(text: str) -> bool:
    """True when *text* contains a real HTML tag, not a bare ``<`` comparison.

    ``<String>``, ``<https://example.com>``, and ``<user@example.com>`` are
    not tags. ``<b>``, ``<i>``, and ``<script>`` are.
    """
    return text_has_real_html_tag(text or "")


def restore_writer_text(text_obj: Any, previous: str) -> None:
    """Put a hidden Writer body back to *previous*.

    A failed insert must not stay in the document. ``setString`` replaces the
    body in one write. The cursor fallback covers a text object that only
    implements the cursor API (the unit-test double has ``setString``).
    """
    try:
        setter = getattr(text_obj, "setString", None)
        if callable(setter):
            setter(previous)
            if (text_obj.getString() or "") == previous:
                return
    except Exception:
        log.debug("restore_writer_text setString failed", exc_info=True)
    try:
        cursor = text_obj.createTextCursor()
        cursor.gotoStart(False)
        cursor.gotoEnd(True)
        cursor.setString(previous)
    except Exception:
        log.exception("restore_writer_text failed")


def _writer_suffix(text_obj: Any, previous: str) -> str:
    current = text_obj.getString() or ""
    if current.startswith(previous):
        return current[len(previous):]
    return current


def _role_prefix(role: str) -> str:
    if role == "user":
        return "You: "
    return _("Assistant:") + " "


def _span_matches_message(added: str, role: str, content: str) -> bool:
    """True when *added* is the role label plus the message's visible words.

    The HTML filter does not reproduce the source character for character
    (paragraph breaks, list layout). A tag, or a word that is not in the
    message, is a failed edit. The caller undoes that span.
    """
    if contains_html_tag(added):
        return False
    visible = strip_html_tags(content or "")
    remainder = added or ""
    # strip_html_tags joins adjacent blocks ("<li>a</li><li>b</li>" -> "ab")
    # while Writer gives one paragraph each, so a compact list failed this
    # check and the full repaint wrote it as plain "ab". Accept both splits.
    spaced = strip_html_tags((content or "").replace("<", " <"))
    labels = ("You:",) if role == "user" else (_("Assistant:"), "Assistant:")
    for label in labels:
        if label and label in remainder:
            remainder = remainder.replace(label, " ", 1)
            break
    allowed_words = {word.lower() for word in re.findall(r"\w+", visible + " " + spaced)}
    for token in re.findall(r"\w+", remainder):
        if token.lower() not in allowed_words:
            return False
    message_words = re.findall(r"\w+", visible + " " + spaced)
    if message_words and not any(word.lower() in remainder.lower() for word in message_words):
        return False
    return True


def append_plain_transcript_row(doc: Any, text: str, role: str = "assistant", style_window: Any = None) -> None:
    """Write one stripped row. No HTML filter, so a tag cannot land in the doc."""
    del style_window
    text_obj = doc.getText()
    cursor = text_obj.createTextCursor()
    cursor.gotoEnd(False)
    if text_obj.getString():
        text_obj.insertString(cursor, "\n\n", False)
    body = strip_html_tags(text or "")
    text_obj.insertString(cursor, _role_prefix(role) + body, False)


def render_messages_to_hidden_doc(doc: Any, items: Any, style_window: Any = None) -> None:
    """Fill *doc* from *items*. Each message is one edit against the previous body.

    Snapshot the body, append, and if the new span is not that message,
    put the body back and write the stripped message. The document stays a
    paint of the list. A failed edit otherwise leaves partial tags that
    are not in the message list.
    """
    text_obj = doc.getText()
    for role, content in items:
        content = content or ""
        before = text_obj.getString() or ""
        ok = append_rich_text(doc, content, role=role, style_window=style_window)
        added = _writer_suffix(text_obj, before)
        if ok and _span_matches_message(added, role, content):
            continue
        restore_writer_text(text_obj, before)
        append_plain_transcript_row(doc, content, role=role, style_window=style_window)
        if not _span_matches_message(_writer_suffix(text_obj, before), role, content):
            # The plain row is the message text. If it still does not match,
            # drop the span rather than keep text the list does not have.
            restore_writer_text(text_obj, before)


def _sidebar_import_html(text: str) -> str:
    """Body fragment for the sidebar HTML filter.

    Extract the body here. ``_wrap_html_fragment`` returns a document that
    already has ``<html>`` and ``<body>`` unchanged, so head and script
    would stay in the import. The Writer import wrapper stays as it is.
    """
    lowered = text.lower()
    if (
        "<html" in lowered
        and "</html>" in lowered
        and "<body" in lowered
        and "</body>" in lowered
    ):
        from plugin.writer.format import _strip_html_boilerplate

        return _strip_html_boilerplate(text)
    return text


def append_rich_text(doc: Any, text: str, role: str = "assistant", style_window: Any = None) -> bool:
    """Append one message to a Writer document (hidden doc for RichTextControl copy).

    Inserts a bold, colored role prefix (``You:`` / ``Assistant:``) then
    imports *text* as HTML via Writer's StarWriter HTML filter so that
    ``<strong>``, ``<em>``, ``<code>``, ``<ul>`` etc. render natively.

    Returns False when the HTML filter fails. The body is restored to the
    text from before this call, so a tag the filter wrote before raising
    does not stay, and the role prefix is not left without its body.
    The caller writes the stripped message.
    """
    text_obj = None
    previous = ""
    try:
        text_obj = doc.getText()
        previous = text_obj.getString() or ""
        cursor = text_obj.createTextCursor()
        cursor.gotoEnd(False)

        theme = ChatTheme.resolve(doc, style_window=style_window)
        importer = HiddenDocHTMLImporter(doc)

        if text and text.strip():
            text = strip_legacy_ai_label(text) if role == "assistant" else text
            from plugin.calc.navigation import render_calc_cell_refs

            text = render_calc_cell_refs(text)

        if text_obj.getString():
            text_obj.insertString(cursor, "\n\n", False)

        # Bold colored role prefix.
        # _role_prefix goes through _(). A bare "Assistant:" literal is
        # skipped by xgettext, so JA/ES catalogs cannot translate it.
        start_pos = cursor.getStart()
        prefix = _role_prefix(role)
        text_obj.insertString(cursor, prefix, False)

        prefix_range = text_obj.createTextCursorByRange(start_pos)
        prefix_range.gotoRange(cursor.getStart(), True)
        prefix_range.CharHeight = CHAT_FONT_HEIGHT
        prefix_range.CharWeight = 150.0  # BOLD
        prefix_range.CharColor = theme.user_color if role == "user" else theme.assistant_color

        # Body content via HTML import
        cursor.gotoEnd(False)
        cursor.CharWeight = CHAT_FONT_WEIGHT  # Reset to normal after bold prefix
        cursor.CharColor = theme.user_color if role == "user" else theme.assistant_color
        # Anchor on the prefix's last character, one before the insert point.
        # A range AT the insert point does not stay put: the HTML import
        # leaves it after the imported body (see html_import._parked_cursor),
        # which made body_range empty and skipped list tightening. A position
        # before the insert point is not moved by the insert.
        body_anchor = text_obj.createTextCursorByRange(cursor.getStart())
        body_anchor.goLeft(1, False)

        if text and text.strip():
            # A tag _HTML_TAG_RE does not list (<script>, a full document the
            # regex missed) used to take the insertString branch and land in
            # the hidden doc as characters.
            looks_html = bool(_HTML_TAG_RE.search(text)) or contains_html_tag(text)
            log.debug("append_rich_text: looks_html=%s len=%d snippet=%r", looks_html, len(text), text[:120])

            used_html_import = False
            if looks_html:
                try:
                    body_html = _sidebar_import_html(text)
                    # A throwaway sentinel paragraph takes the import's
                    # first-paragraph merge (see _LIST_SENTINEL). Without it a
                    # leading <ol> draws "Assistant: a / 1. b / 2. c". Ordinary
                    # prose keeps sharing the label line.
                    leading_list = _body_starts_with_list(body_html)
                    if leading_list:
                        body_html = "<p>%s</p>%s" % (_LIST_SENTINEL, body_html)
                    importer.insert_html_at_cursor(cursor, body_html)
                    if leading_list:
                        _drop_list_sentinel(text_obj, body_anchor)
                    used_html_import = True
                except Exception:
                    # Restore the body from before this call. The filter
                    # can insert tags and then raise; return False only skips
                    # the copy, and a caller that keeps the doc still has the
                    # partial message.
                    log.warning("HTML import failed; restoring hidden doc role=%s", role)
                    restore_writer_text(text_obj, previous)
                    return False
            else:
                text_obj.insertString(cursor, text, False)

            added = _writer_suffix(text_obj, previous)
            if contains_html_tag(added):
                # The filter returned without raising and left the tags as text.
                log.warning("HTML import left tags in the hidden doc role=%s", role)
                restore_writer_text(text_obj, previous)
                return False

            # Start body_range one character after body_anchor (the end of
            # the prefix). Counting from the document start with
            # doc.CharacterCount stops short by the paragraph breaks that
            # statistic excludes, lands inside the previous assistant word,
            # and gotoEnd then paints that tail in the user color.
            body_range = text_obj.createTextCursorByRange(body_anchor.getStart())
            body_range.goRight(1, False)
            body_range.gotoEnd(True)
            # Plain text gets the role tint; successful HTML import keeps
            # per-span CharColor from the filter (red/blue runs, etc.).
            if not used_html_import:
                body_range.CharColor = theme.user_color if role == "user" else theme.assistant_color
            importer.tighten_list_indent(body_range)
        return True

    except Exception as e:
        log.exception("Error in append_rich_text: %s", e)
        if text_obj is not None:
            restore_writer_text(text_obj, previous)
        return False


def finalize_sidebar_assistant_response(listener: Any, *, allow_rerender: bool = True) -> None:
    """Re-import the last assistant message as HTML when rich sidebar is active.

    Skip HTML rerender after an API error. Drain ERROR appends ``[API error: …]``
    after ``_assistant_stream_start_len``; rerender looks up the previous HTML
    assistant message and ``truncate_control_from`` would delete that error line
    (Packet F HTTP 429/500 looked like a leftover hello).

    Skip rerender after Stop. The Stop path already stored the partial answer
    and the ``[Stopped by user]`` row, then painted that list. A second paint
    is not required. The old splice truncated the tail and pasted the answer
    without the marker (Packet B1).

    Empty / truncated STREAM_DONE must AddMessageEffect the banner (see
    ``tool_loop_state``) so this path does not paste the previous HTML assistant
    over ``[Response truncated]`` / ``[No text from model]`` (Packet C).
    """
    # Append a leftover unclosed tag when the rerender did not replace
    # the control. The Error path clears the stripper without finalize(),
    # and the default sidebar has the rich widget, so Stop and error would
    # drop that tail. A successful rerender already replaced it from the
    # session.
    replaced = False
    if getattr(listener, "_terminal_status", None) != "Error" and allow_rerender:
        rerender = getattr(listener, "rerender_rich_text_session", None)
        if callable(rerender):
            replaced = rerender() is True
    from plugin.chatbot.tool_loop_actions import TurnController, current_turn

    turn = current_turn(listener)
    if isinstance(turn, TurnController) and not turn.same_messages():
        return
    stripper = turn.stripper if isinstance(turn, TurnController) else None
    if stripper is not None and isinstance(turn, TurnController):
        leftover = turn.take_stripper_tail()
        if leftover and not replaced:
            listener._append_response(leftover, role="assistant")

    # Append _STOP_LINE to the control for this stopped turn. Skipping
    # rerender keeps the ramble from painting over the banner, and
    # close_stopped only persisted the line on session.messages, so the
    # control never drew [Stopped by user].
    stop_requested = getattr(listener, "stop_requested", False) or not allow_rerender
    if stop_requested and getattr(listener, "_terminal_status", None) != "Error":
        if isinstance(turn, TurnController):
            if getattr(turn, "_stop_banner_appended", False):
                return
            turn._stop_banner_appended = True
        from plugin.chatbot.tool_loop_actions import _STOP_LINE
        from plugin.chatbot.dialogs import get_control_text, set_control_text

        widget = getattr(listener, "rich_text_widget", None)
        if widget is not None:
            run_rich = getattr(listener, "_run_rich_ui", None)
            if callable(run_rich):
                run_rich(lambda: widget.append_chunk(_STOP_LINE))
            else:
                widget.append_chunk(_STOP_LINE)
        else:
            control = getattr(listener, "response_control", None)
            if control is not None and control.getModel():
                cur = get_control_text(control, default="") or ""
                set_control_text(control, cur + _STOP_LINE)
