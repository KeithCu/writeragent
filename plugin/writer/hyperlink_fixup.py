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
"""Keep Writer outline-hyperlink targets in step with a search replace.

Discussion #819: a same-length ``setString`` inside ``replace_preserving_format``
keeps ``HyperLinkURL``, so a TOC entry whose visible title changed still points
at ``#old title|outline``. The HTML path's ``setString("")``, and a
length-changing preserve-format replace, can clear the property on the new
characters instead. Capture the URL before the edit and write it back after,
in both cases.

Bookmark targets (``#__RefHeading___Toc…``) do not embed the heading text.
They are left alone. Only URLs ending in ``|outline`` are rewritten, and only
when the matched text occurs once outside that suffix (or the caller passed
``hyperlink_url`` for that one outline link).

The write covers every portion in the paragraph that still has that same URL,
not only the replaced characters. A TOC link usually also covers the number,
the tab, and the page number; painting just the title splits one link into two
targets.
"""

from __future__ import annotations

import logging
from typing import Any

log = logging.getLogger("writeragent.writer.hyperlink")

_OUTLINE_SUFFIX = "|outline"
_URL = "HyperLinkURL"
_NAME = "HyperLinkName"
_TARGET = "HyperLinkTarget"


class OutlineLink:
    """One distinct outline hyperlink overlapping a search match."""

    __slots__ = ("url", "name", "target")

    def __init__(self, url: str, name: str, target: str) -> None:
        self.url = url
        self.name = name
        self.target = target


class OutlineSnapshot:
    """Outline links plus the paragraph text around the match, copied out before the edit.

    The match range itself is not kept: the HTML path deletes it. ``prefix`` /
    ``suffix`` are plain strings so the replacement can be measured afterwards
    from whatever text actually landed.
    """

    __slots__ = ("links", "prefix", "suffix", "matched", "single_paragraph", "preserve_url")

    def __init__(self, links: list[OutlineLink], prefix: str, suffix: str,
                 matched: str, single_paragraph: bool, preserve_url: str = "") -> None:
        self.links = links
        self.prefix = prefix
        self.suffix = suffix
        self.matched = matched
        self.single_paragraph = single_paragraph
        # A non-outline URL (bookmark TOC target) that covered the match. It is
        # never rewritten. A same-length setString keeps HyperLinkURL. HTML
        # setString("") and a length-changing preserve-format replace can clear
        # it on the new characters; this URL is put back unchanged so the
        # bookmark is not unlinked.
        self.preserve_url = preserve_url


def empty_snapshot() -> OutlineSnapshot:
    return OutlineSnapshot([], "", "", "", True, "")


def _prop(obj: Any, name: str) -> str:
    try:
        value = obj.getPropertyValue(name)
    except Exception:
        return ""
    return value if isinstance(value, str) else ""


def _point_before(text: Any, left: Any, right: Any) -> bool:
    """True when *left* starts before *right* (``compareRegionStarts`` == 1)."""
    try:
        return int(text.compareRegionStarts(left, right)) == 1
    except Exception:
        return False


def _point_after(text: Any, left: Any, right: Any) -> bool:
    """True when *left* ends after *right* (``compareRegionEnds`` == -1)."""
    try:
        return int(text.compareRegionEnds(left, right)) == -1
    except Exception:
        return False


def _ranges_overlap(text: Any, portion: Any, match: Any) -> bool:
    """True when *portion* and *match* share a character (touching at an edge does not)."""
    try:
        p0, p1 = portion.getStart(), portion.getEnd()
        m0, m1 = match.getStart(), match.getEnd()
    except Exception:
        return False
    return _point_before(text, p0, m1) and _point_after(text, p1, m0)


def substitute_if_present(value: str, matched: str, new_plain: str) -> str | None:
    """Replace the one title copy of *matched* in *value*.

    None when *matched* is empty, absent, or ambiguous. ``str.replace`` would
    rewrite every copy, so ``line`` in ``#1.The line.|outline`` becomes
    ``|outrow`` and a repeated title is renamed in both places. Hits that
    overlap a trailing ``|outline`` are ignored; exactly one hit must remain.
    Uses the visible matched text, not the search pattern, so a regex replace
    does not paste the pattern into the URL.
    """
    if not matched or matched not in value:
        return None
    suffix_at = len(value) - len(_OUTLINE_SUFFIX) if value.endswith(_OUTLINE_SUFFIX) else None
    suffix_end = None if suffix_at is None else suffix_at + len(_OUTLINE_SUFFIX)
    hits: list[int] = []
    start = 0
    while True:
        found = value.find(matched, start)
        if found < 0:
            break
        end = found + len(matched)
        overlaps_suffix = (
            suffix_at is not None and suffix_end is not None
            and found < suffix_end and end > suffix_at)
        if not overlaps_suffix:
            hits.append(found)
        start = found + 1
    if len(hits) != 1:
        return None
    at = hits[0]
    return value[:at] + new_plain + value[at + len(matched):]


def plan_outline_updates(links: list[OutlineLink], matched: str, new_plain: str,
                         override: str | None) -> list[dict[str, Any]]:
    """Decide the URL (and name/target, when they contain *matched*) for each distinct link.

    ``new_url`` is None when that link must not be written. An explicit *override*
    is that URL and skips substitution. Bookmark URLs never appear here; callers
    only pass ``|outline`` links.
    """
    plans: list[dict[str, Any]] = []
    seen: set[str] = set()
    for link in links:
        if link.url in seen or not link.url.endswith(_OUTLINE_SUFFIX):
            continue
        seen.add(link.url)
        new_url: str | None
        if override:
            new_url = override if override != link.url else None
        else:
            replaced = substitute_if_present(link.url, matched, new_plain)
            new_url = replaced if replaced is not None and replaced != link.url else None
        # Name/target follow the visible rename only when the URL is actually changing.
        # Otherwise a spacing mismatch would rewrite the name and leave a stale target.
        new_name = None
        new_target = None
        if new_url is not None:
            new_name = substitute_if_present(link.name, matched, new_plain)
            new_target = substitute_if_present(link.target, matched, new_plain)
        after = new_url if new_url is not None else link.url
        plans.append({
            "url": link.url,
            "new_url": new_url,
            "new_name": new_name,
            "new_target": new_target,
            "hyperlink_url": link.url,
            "hyperlink_url_after": after,
            "hyperlink_updated": new_url is not None,
        })
    return plans


def public_hyperlink_reports(plans: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The fields returned to the model. Internal write keys stay out of the tool result."""
    return [
        {
            "hyperlink_url": plan["hyperlink_url"],
            "hyperlink_url_after": plan["hyperlink_url_after"],
            "hyperlink_updated": plan["hyperlink_updated"],
        }
        for plan in plans
    ]


def _cursor_at(text: Any, pos: Any) -> Any:
    return text.createTextCursorByRange(pos)


def _same_point(text: Any, left: Any, right: Any) -> bool:
    try:
        return int(text.compareRegionStarts(left, right)) == 0
    except Exception:
        return False


def _paragraph_edges(text: Any, text_range: Any) -> tuple[Any, Any, bool]:
    """Collapsed cursors at the start and end of the paragraph that contains the match start.

    ``single`` is False when the match ends in a different paragraph — prefix/suffix
    measurement would then pair text from two paragraphs.
    """
    start_cur = _cursor_at(text, text_range.getStart())
    start_cur.gotoStartOfParagraph(False)
    end_probe = _cursor_at(text, text_range.getEnd())
    end_probe.gotoStartOfParagraph(False)
    single = _same_point(text, start_cur.getStart(), end_probe.getStart())
    end_cur = _cursor_at(text, text_range.getEnd() if single else text_range.getStart())
    end_cur.gotoEndOfParagraph(False)
    return start_cur, end_cur, single


def _between(text: Any, start_pos: Any, end_pos: Any) -> str:
    cursor = _cursor_at(text, start_pos)
    cursor.gotoRange(end_pos, True)
    try:
        return str(cursor.getString() or "")
    except Exception:
        return ""


def _enum_has_more(enum: Any) -> bool:
    """True only for a real UNO ``True``.

    ``if not enum.hasMoreElements()`` is wrong on ``MagicMock``: the return is
    another mock, which is truthy, so the loop never stops. PR #823 pull_request
    CI merges this module onto unit tests that pass MagicMock ranges
    (``test_edited_context``, ``test_small_fixes_r5``) and hung until GHA
    SIGTERM. ``is True`` matches UNO bools and the ``_Enum`` fake.
    """
    try:
        return enum.hasMoreElements() is True
    except Exception:
        return False


def _iter_portions(text: Any, anchor_pos: Any):
    """Portions of the paragraph containing *anchor_pos*."""
    cursor = _cursor_at(text, anchor_pos)
    cursor.gotoStartOfParagraph(False)
    cursor.gotoEndOfParagraph(True)
    try:
        para_enum = cursor.createEnumeration()
    except Exception:
        return
    while True:
        try:
            if not _enum_has_more(para_enum):
                break
            para = para_enum.nextElement()
        except Exception:
            break
        try:
            if hasattr(para, "supportsService") and not para.supportsService(
                    "com.sun.star.text.Paragraph"):
                continue
            portion_enum = para.createEnumeration()
        except Exception:
            continue
        while True:
            try:
                if not _enum_has_more(portion_enum):
                    break
                yield portion_enum.nextElement()
            except Exception:
                break


def capture_outline_hyperlinks(text_range: Any) -> OutlineSnapshot:
    """Read ``|outline`` links that overlap *text_range*, and the paragraph text around it."""
    try:
        text = text_range.getText()
        matched = str(text_range.getString() or "")
    except Exception:
        log.debug("capture_outline_hyperlinks: match range unreadable", exc_info=True)
        return empty_snapshot()

    try:
        start_cur, end_cur, single = _paragraph_edges(text, text_range)
        prefix = _between(text, start_cur.getStart(), text_range.getStart())
        suffix = _between(text, text_range.getEnd(), end_cur.getEnd()) if single else ""
    except Exception:
        log.debug("capture_outline_hyperlinks: paragraph edges failed", exc_info=True)
        return OutlineSnapshot([], "", "", matched, False, "")

    links: list[OutlineLink] = []
    seen: set[str] = set()
    preserve_url = ""
    for portion in _iter_portions(text, text_range.getStart()):
        if not _ranges_overlap(text, portion, text_range):
            continue
        url = _prop(portion, _URL)
        if not url:
            continue
        if not url.endswith(_OUTLINE_SUFFIX):
            # First bookmark (or other) URL on the match. Not rewritten; restored
            # onto the replaced characters if the text replace clears it.
            if not preserve_url:
                preserve_url = url
            continue
        if url in seen:
            continue
        seen.add(url)
        links.append(OutlineLink(url, _prop(portion, _NAME), _prop(portion, _TARGET)))
    return OutlineSnapshot(links, prefix, suffix, matched, single, preserve_url)


def measured_replacement(anchor: Any, snapshot: OutlineSnapshot) -> str | None:
    """Visible text that replaced the match, taken from the paragraph after the edit.

    None when the match spanned paragraphs or the surrounding text no longer lines up,
    so the caller falls back to the plain string it asked to insert.
    """
    if anchor is None or not snapshot.single_paragraph:
        return None
    try:
        text = anchor.getText()
        cursor = _cursor_at(text, anchor.getStart())
        cursor.gotoStartOfParagraph(False)
        cursor.gotoEndOfParagraph(True)
        para = str(cursor.getString() or "")
    except Exception:
        log.debug("measured_replacement: paragraph read failed", exc_info=True)
        return None
    prefix, suffix = snapshot.prefix, snapshot.suffix
    if not para.startswith(prefix):
        return None
    if suffix:
        if len(para) < len(prefix) + len(suffix) or not para.endswith(suffix):
            return None
        return para[len(prefix):len(para) - len(suffix)]
    return para[len(prefix):]


def _set_prop(cursor: Any, name: str, value: str) -> None:
    try:
        cursor.setPropertyValue(name, value)
    except Exception as exc:
        # A protected index refuses the property write. The caller rolls the text
        # change back with this error instead of leaving a stale outline target.
        raise RuntimeError(
            "Could not update the outline hyperlink (the index may be protected "
            "against manual changes): %s" % exc
        ) from exc


def _step_right(cursor: Any, count: int, expand: bool) -> bool:
    """Move or extend *cursor* right by *count*. False if the move stops short.

    UNO caps one goRight at a C++ short, so a long run is taken in chunks.
    """
    remaining = count
    while remaining > 0:
        step = remaining if remaining < 32767 else 32767
        try:
            moved = cursor.goRight(step, expand)
        except Exception:
            return False
        if moved is False:
            return False
        remaining -= step
    return True


def _select_replacement(text: Any, anchor: Any, snapshot: OutlineSnapshot, length: int):
    """Cursor covering the replaced characters, measured from the paragraph start.

    A cursor saved at the match start does not stay there: replace_preserving_format
    calls setString on each changed character, and that pushes other cursors at
    the same position forward. Painting from that drifted cursor writes the URL
    onto the following bookmark. The paragraph start plus the saved prefix length
    is still the replacement, including after that drift.
    """
    if not snapshot.single_paragraph or length < 0:
        return None
    try:
        cursor = _cursor_at(text, anchor.getStart())
        cursor.gotoStartOfParagraph(False)
    except Exception:
        return None
    if snapshot.prefix and not _step_right(cursor, len(snapshot.prefix), False):
        return None
    if length and not _step_right(cursor, length, True):
        return None
    return cursor


def _write_plan_on_cursor(cursor: Any, plan: dict[str, Any]) -> None:
    if plan["new_url"] is not None:
        _set_prop(cursor, _URL, plan["new_url"])
    if plan["new_name"] is not None:
        _set_prop(cursor, _NAME, plan["new_name"])
    if plan["new_target"] is not None:
        _set_prop(cursor, _TARGET, plan["new_target"])


def _paint(text: Any, anchor: Any, snapshot: OutlineSnapshot, new_plain: str,
           plan: dict[str, Any], *, required: bool) -> bool:
    """Set *plan* on the replaced characters. Return False when the range cannot be selected.

    *required* raises: an outline update that cannot see the new text would leave
    the link stale. A bookmark put-back is best-effort so a selection failure does
    not roll back a text replace that did not ask to change the target.
    """
    cursor = _select_replacement(text, anchor, snapshot, len(new_plain))
    if cursor is None:
        if required:
            raise RuntimeError(
                "Could not select the replaced text to update its outline hyperlink.")
        log.debug("outline hyperlink: could not reselect the replaced text to restore its URL")
        return False
    _write_plan_on_cursor(cursor, plan)
    return True


def restore_outline_hyperlinks(anchor: Any, snapshot: OutlineSnapshot,
                               fallback_plain: str | None,
                               override: str | None) -> list[dict[str, Any]]:
    """Write the planned outline URL onto the whole span, using *anchor* (the match start).

    *fallback_plain* is the plain text the caller inserted, used when the paragraph
    can no longer be measured. Returns the public before/after reports.
    """
    if anchor is None:
        if snapshot.links or override:
            raise RuntimeError("Could not locate the replaced text to update its outline hyperlink.")
        return []

    new_plain = measured_replacement(anchor, snapshot)
    if new_plain is None:
        new_plain = fallback_plain if fallback_plain is not None else snapshot.matched

    plans = plan_outline_updates(snapshot.links, snapshot.matched, new_plain, override)
    # An override with no outline link is rejected in apply_document_content
    # before the edit. Do not paint it onto a bookmark or plain text here.

    writable = [plan for plan in plans if plan["new_url"] is not None]
    try:
        text = anchor.getText()
    except Exception as exc:
        raise RuntimeError(
            "Could not locate the replaced text to update its outline hyperlink: %s" % exc
        ) from exc
    if not writable:
        # Put the same target back on the replaced characters only when they
        # belonged to that one link. A same-length setString keeps HyperLinkURL.
        # HTML setString("") and a length-changing preserve-format replace can
        # clear it, which would unlink "New" and leave only " title". Painting
        # links[0] or the bookmark across a match that also overlaps another
        # link would give the neighbor the wrong target; those characters stay
        # unlinked instead.
        put_back = ""
        if len(snapshot.links) == 1 and not snapshot.preserve_url:
            put_back = snapshot.links[0].url
        elif not snapshot.links and snapshot.preserve_url:
            put_back = snapshot.preserve_url
        if put_back and new_plain:
            _paint(text, anchor, snapshot, new_plain, {
                "new_url": put_back,
                "new_name": None,
                "new_target": None,
            }, required=False)
        return public_hyperlink_reports(plans)

    by_url = {plan["url"]: plan for plan in writable if plan["url"]}

    # Collect runs before writing. setPropertyValue can split a portion, which
    # invalidates an enumeration that is still in progress.
    pending = []
    for portion in _iter_portions(text, anchor.getStart()):
        current = _prop(portion, _URL)
        plan = by_url.get(current)
        if plan is None:
            continue
        try:
            if _same_point(text, portion.getStart(), portion.getEnd()):
                continue
            pending.append((portion.getStart(), portion.getEnd(), plan))
        except Exception as exc:
            raise RuntimeError(
                "Could not locate an outline hyperlink run to update: %s" % exc
            ) from exc
    for start, end, plan in pending:
        cursor = _cursor_at(text, start)
        cursor.gotoRange(end, True)
        _write_plan_on_cursor(cursor, plan)

    # len(writable) == 1 is not "one link": another outline URL may have been
    # left unchanged, or a bookmark may overlap the match. Painting this target
    # across the replacement would give those neighbors the wrong URL. The HTML
    # path still needs the paint when the match was that single outline link,
    # because setString("") drops HyperLinkURL on the new characters.
    one_outline = len(snapshot.links) == 1 and not snapshot.preserve_url
    paint = writable[0] if one_outline and len(writable) == 1 else None

    # An empty replacement that deletes the whole link leaves no portion to paint.
    # That delete succeeds: nothing with a stale URL remains. A non-empty
    # replacement that cannot be reselected still raises inside _paint.
    if paint is not None and new_plain:
        _paint(text, anchor, snapshot, new_plain, paint, required=True)

    log.debug("outline hyperlink updated: %s", public_hyperlink_reports(plans))
    return public_hyperlink_reports(plans)
