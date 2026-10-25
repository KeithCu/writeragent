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

The hidden Writer document is a paint of the message list, configured and rendered
by ``rich_text_paste``. It does not splice a later message into a
document that already failed partway.
"""

from __future__ import annotations

from collections import Counter
import logging
import re
from typing import Any, cast

from plugin.calc.navigation import WRITERAGENT_CELL_URL_PREFIX, normalize_cell_address
from plugin.framework.appearance import get_theme_colors
from plugin.framework.errors import best_effort, uno_probe
from plugin.framework.html_stripper import strip_html_tags, text_has_real_html_tag
from plugin.framework.i18n import _

log = logging.getLogger(__name__)


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
    """Delete the sentinel character the import left right after the prefix."""
    probe = text_obj.createTextCursorByRange(body_anchor.getStart())
    probe.goRight(1, False)
    probe.goRight(1, True)
    if probe.getString() == _LIST_SENTINEL:
        probe.setString("")

CHAT_FONT_NAME = "Liberation Sans"
CHAT_FONT_HEIGHT = 10.0
CHAT_FONT_WEIGHT = 100.0
# Writer paragraph margins (1/100 mm) — horizontal padding inside RichTextControl EditEngine.
CHAT_PARA_SIDE_MARGIN = 250


def _safe_set_prop(obj: Any, name: str, val: Any) -> None:
    uno_probe(lambda: setattr(obj, name, val), None)


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
        _safe_set_prop(target, name, val)
    if bg_color is not None:
        _safe_set_prop(target, "CharBackColor", bg_color)


_SERIF_FONT_MARKERS = ("serif", "times", "roman", "courier", "mono")


def normalize_portion_font(portion: Any) -> None:
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


def apply_rich_control_para_margins(cursor: Any) -> None:
    """Keep chat text off the RichTextControl edges (EditEngine has no CSS padding)."""
    for name, val in (
        ("ParaLeftMargin", CHAT_PARA_SIDE_MARGIN),
        ("ParaRightMargin", CHAT_PARA_SIDE_MARGIN),
        ("ParaFirstLineIndent", 0),
    ):
        _safe_set_prop(cursor, name, val)


def strip_legacy_ai_label(text: str) -> str:
    """Remove leading ``AI:`` from greeting/assistant text (avoid ``Assistant: AI:``)."""
    if not text:
        return text
    return _LEGACY_AI_LABEL_RE.sub("", text, count=1)


# HTML: <a href="cell://B2">B2</a> (Calc chat uses HTML, not markdown)
_CELL_LINK_HTML_RE = re.compile(r'(href\s*=\s*)(["\'])cell://([^"\']+)\2', re.IGNORECASE)


def render_calc_cell_refs(text: str) -> str:
    """Rewrite ``cell://`` in HTML ``<a href>`` to the internal sidebar link scheme."""
    if not text or "cell://" not in text.lower():
        return text

    def _rewrite_href(match: re.Match[str]) -> str:
        prefix, quote, raw_addr = match.group(1), match.group(2), match.group(3)
        addr = normalize_cell_address(raw_addr)
        if not addr:
            return match.group(0)
        return f"{prefix}{quote}{WRITERAGENT_CELL_URL_PREFIX}{addr}{quote}"

    return _CELL_LINK_HTML_RE.sub(_rewrite_href, text)


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


def _tighten_list_indent(body_range: Any) -> None:
    """Tighten indentation on list paragraphs within *body_range*.

    Modifies NumberingRules for paragraphs with NumberingIsNumber to tight
    values suitable for the narrow sidebar.
    """
    import uno

    enum = body_range.createEnumeration()

    para_count = 0
    tightened = 0
    processed_levels = set()
    while enum.hasMoreElements():
        para = enum.nextElement()
        para_count += 1
        if not uno_probe(lambda: para.getPropertyValue("NumberingIsNumber"), False):
            continue

        level = para.getPropertyValue("NumberingLevel")
        list_id = para.getPropertyValue("ListId")

        key = (list_id, level)
        if key in processed_levels:
            continue
        processed_levels.add(key)

        def _tighten() -> bool:
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
            return True

        if best_effort(_tighten, f"_tighten_list_indent: failed for level {level}"):
            tightened += 1

    log.debug("_tighten_list_indent: scanned %d paragraphs, tightened %d", para_count, tightened)


def _insert_html_at_cursor(cursor: Any, html_fragment: str) -> None:
    """Import an HTML fragment at *cursor* using Writer's HTML filter."""
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

    A failed insert must not stay in the document. ``XText`` is an
    ``XTextRange``, so ``setString`` replaces the body in one write.
    """
    text_obj.setString(previous)


def writer_suffix(text_obj: Any, previous: str) -> str:
    current = text_obj.getString() or ""
    if current.startswith(previous):
        return current[len(previous):]
    return current


def _role_prefix(role: str) -> str:
    if role == "user":
        return "You: "
    return _("Assistant:") + " "


def span_matches_message(added: str, role: str, content: str) -> bool:
    """True when *added* is the role label plus the message's visible words.

    The HTML filter does not reproduce the source character for character
    (paragraph breaks, list layout). A tag, or a word that is not in the
    message, is a failed edit. The caller undoes that span.
    """
    if contains_html_tag(added):
        return False
    remainder = (added or "").lstrip()
    labels = ("You:",) if role == "user" else (_("Assistant:"), "Assistant:")
    matched_label: str | None = None
    for label in labels:
        if label and remainder.startswith(label):
            matched_label = label
            break
    if matched_label is None:
        return False
    remainder = remainder[len(matched_label):]

    remainder_counter = Counter(tok.casefold() for tok in re.findall(r"\w+", remainder))
    visible = strip_html_tags(content or "")
    # strip_html_tags joins adjacent blocks ("<li>a</li><li>b</li>" -> "ab")
    # while Writer gives one paragraph each, so a compact list failed this
    # check and the full repaint wrote it as plain "ab". Accept both splits.
    spaced = strip_html_tags((content or "").replace("<", " <"))
    visible_counter = Counter(tok.casefold() for tok in re.findall(r"\w+", visible))
    spaced_counter = Counter(tok.casefold() for tok in re.findall(r"\w+", spaced))
    return remainder_counter == visible_counter or remainder_counter == spaced_counter


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


def _append_rich_text_with_span(
    doc: Any,
    text: str,
    role: str = "assistant",
    style_window: Any = None,
) -> tuple[bool, str, str]:
    """Append one message returning (ok, previous, added)."""
    text_obj = None
    previous = ""
    added = ""
    try:
        text_obj = doc.getText()
        previous = text_obj.getString() or ""
        cursor = text_obj.createTextCursor()
        cursor.gotoEnd(False)

        theme = ChatTheme.resolve(doc, style_window=style_window)

        if text and text.strip():
            text = strip_legacy_ai_label(text) if role == "assistant" else text
            text = render_calc_cell_refs(text)

        # Park anchor before the insert point so added span includes separator and prefix.
        parked_anchor = None
        if previous:
            parked_anchor = text_obj.createTextCursorByRange(cursor.getStart())
            parked_anchor.goLeft(1, False)
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
            looks_html = contains_html_tag(text)
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
                    _insert_html_at_cursor(cursor, body_html)
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
                    return False, previous, ""
            else:
                text_obj.insertString(cursor, text, False)

            if parked_anchor is not None:
                added_cur = text_obj.createTextCursorByRange(parked_anchor.getStart())
                added_cur.goRight(1, False)
                added_cur.gotoEnd(True)
                added = added_cur.getString() or ""
            else:
                added = text_obj.getString() or ""

            if contains_html_tag(added):
                # The filter returned without raising and left the tags as text.
                log.warning("HTML import left tags in the hidden doc role=%s", role)
                restore_writer_text(text_obj, previous)
                return False, previous, ""

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
            _tighten_list_indent(body_range)
        else:
            if parked_anchor is not None:
                added_cur = text_obj.createTextCursorByRange(parked_anchor.getStart())
                added_cur.goRight(1, False)
                added_cur.gotoEnd(True)
                added = added_cur.getString() or ""
            else:
                added = text_obj.getString() or ""

        return True, previous, added

    except Exception as e:
        log.exception("Error in append_rich_text: %s", e)
        if text_obj is not None:
            restore_writer_text(text_obj, previous)
        return False, previous, ""


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
    ok, _prev, _added = _append_rich_text_with_span(doc, text, role=role, style_window=style_window)
    return ok
