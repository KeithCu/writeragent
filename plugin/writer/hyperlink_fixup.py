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

Discussion #819: an untracked ``replace_preserving_format`` keeps ``HyperLinkURL``
(except on a one-character run), so a TOC entry whose visible title changed still
points at ``#old title|outline``. HTML replaces and recorded replaces can clear
the property on the new characters instead. Capture the URL before the edit and
write it back after, in both cases.

Bookmark targets (``#__RefHeading___Toc…``) do not embed the heading text.
They are left alone. Only URLs ending in ``|outline`` are rewritten, and only
when the matched text occurs once outside that suffix (or the caller passed
``hyperlink_url`` for that one outline link).

The write covers every portion in the paragraph that still has that same URL,
not only the replaced characters. A TOC link usually also covers the number,
the tab, and the page number; painting just the title splits one link into two
targets.

Subrange writes: assigning ``HyperLinkURL`` on a *subrange* of an
already-linked line (the title only) splits that portion and LibreOffice
paints Internet-link defaults (navy + single underline) on the title and the
leftover punctuation/tab. Numbering and the page number are usually their
own portions, so a whole-portion URL write leaves them alone. A preserve-
format replace keeps the old URL on the whole line; the pending pass already
rewrites it. Skip the title-only paint when the replacement already has the
new URL. When a paint is still needed (HTML dropped the URL), snapshot
direct ``CharColor`` / ``CharUnderline`` overrides after the text replace;
name, target, and link character styles come from before the replace. Both go
on the same cursor immediately after the assignment.

Every link write in this module uses ``write_link``, which resets
``HyperLinkURL`` first to record the old link for undo and restores name,
target, and link styles.

A page-number replace does not change the URL. ``setString`` of those digits
still splits the run onto Internet-link chrome, and ``CharColor`` on that
subrange repaints the whole link. To prevent painting that subrange from
turning inherited style colors into direct attributes, the direct values
and link styles captured before the replace are written on the whole
paragraph after assigning the same URL. Failures raise
ToolExecutionError(code="HYPERLINK_UPDATE_FAILED") and the caller rolls the text
change back.

While track changes is recording, matched-link outline URLs are not renamed (a
tracked HyperLinkURL write on existing text becomes a Format redline, and rejecting
it drops the link). Instead, ``restore_links`` writes the link's own URL onto
only the inserted runs, untracked, and adds ``hyperlink_note`` to the report when
a live run still carries the link. Recorded plain replaces carry their own link
when the whole match shares one URL, and partly linked matches are not painted.

When an edit renames a heading (``heading_renames``), ``retarget_outline_links``
finds every ``#old|outline`` portion in the document (including numbered and
percent-encoded outline URLs) and updates it to ``#new|outline`` — directly when
not recording or inside a tracked insert, and via tracked Delete + Insert when
recording changes. Returned stale URLs carry a reason: field runs that cannot be
retyped (``field``), or a title mapped to ``None`` in ``successors`` that no
heading replaces (``no_heading``, left unchanged).
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from difflib import SequenceMatcher
import logging
import re
from typing import Any, Iterator, Literal
from urllib.parse import quote, unquote

import uno

from plugin.doc.text_helpers import DeletionState, go_right, iter_elements
from plugin.framework.errors import ToolExecutionError, uno_read, uno_write
from plugin.writer.record_changes import is_recording_changes, record_changes, redline_kinds_covering

log = logging.getLogger("writeragent.writer.hyperlink")

_OUTLINE_SUFFIX = "|outline"
# Lowest SequenceMatcher ratio that counts as "the same title, reworded".
_SIMILAR_TITLE = 0.6
# Shared numbering is not evidence of the same title; strip "word + short number" labels before comparing.
_LEADING_LABEL_RE = re.compile(r"^\w+\s+\w{1,4}\s*[:.\-\u2013\u2014]\s+(?=\w)")
# A number, Roman numeral or single letter that can number a heading ("2.1", "iv", "a"), in casefolded text.
_NUMBER_RE = re.compile(r"\d+(?:\.\d+)*|\b(?:[ivxlcdm]+|[^\W\d_])\b")
# Why a link still names an old heading title.
StaleReason = Literal["field", "no_heading"]
_URL = "HyperLinkURL"
_NAME = "HyperLinkName"
_TARGET = "HyperLinkTarget"
LINK_STYLE_PROPS: tuple[str, str] = ("UnvisitedCharStyleName", "VisitedCharStyleName")
# Link chrome only. HyperLinkURL assignment overwrites these; the HTML range
# copy also treats them as the run's look. Other Char* (weight, font) stay
# with the text replace / HTML import and must not be re-painted here. Only
# direct values of these properties, alongside link character styles, are copied.
_CHAR_LOOK = ("CharColor", "CharUnderline")


@dataclass(eq=False)
class OutlineLink:
    """One distinct outline hyperlink overlapping a search match."""

    url: str
    name: str
    target: str


@dataclass(eq=False)
class OutlineSnapshot:
    """Outline links plus the paragraph text around the match, copied out before the edit.

    The match range itself is not kept: the HTML path deletes it. ``prefix`` /
    ``suffix`` are plain strings so the replacement can be measured afterwards
    from whatever text actually landed.
    """

    links: list[OutlineLink]
    prefix: str
    suffix: str
    matched: str
    single_paragraph: bool
    # A non-outline URL (bookmark TOC target) that covered the match. It is
    # never rewritten. An untracked preserve-format replace keeps HyperLinkURL
    # except on a one-character run; HTML replaces and recorded replaces can
    # clear it on the new characters, so this URL is put back unchanged to keep
    # the bookmark linked.
    preserve_url: str = ""
    # ``read_link_props`` of the first linked run of the match, copied before the text
    # replace. A later snapshot already includes Internet-link chrome.
    props: dict[str, Any] = field(default_factory=dict)
    # False when part of the match is not in any link.
    fully_linked: bool = True
    # True when the match spans more than one distinct non-outline URL.
    multi_preserve: bool = False
    # Every non-empty portion of the paragraph has the same URL, name and target.
    whole_paragraph: bool = False


def _prop(obj: Any, name: str) -> str:
    # Marker portions may lack link properties; a failed read counts as empty.
    value = uno_read(lambda: obj.getPropertyValue(name), None)
    return value if isinstance(value, str) else ""


def _ranges_overlap(text: Any, portion: Any, match: Any) -> bool:
    """True when *portion* and *match* share a character (touching at an edge does not)."""
    p0, p1 = portion.getStart(), portion.getEnd()
    m0, m1 = match.getStart(), match.getEnd()
    # compareRegionStarts == 1: p0 starts before m1
    # compareRegionEnds == -1: p1 ends after m0
    return (
        uno_read(lambda: int(text.compareRegionStarts(p0, m1)) == 1, False)
        and uno_read(lambda: int(text.compareRegionEnds(p1, m0)) == -1, False)
    )


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
    body = value[:-len(_OUTLINE_SUFFIX)] if value.endswith(_OUTLINE_SUFFIX) else value
    i = body.find(matched)
    if i < 0 or body.find(matched, i + 1) >= 0:
        return None
    return value[:i] + new_plain + value[i + len(matched):]


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
        plans.append({
            "url": link.url,
            "new_url": new_url,
            "new_name": new_name,
            "new_target": new_target,
        })
    return plans


def public_hyperlink_reports(plans: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The fields returned to the model. Internal write keys stay out of the tool result."""
    return [
        {
            "hyperlink_url": plan["url"],
            "hyperlink_url_after": plan["new_url"] if plan["new_url"] is not None else plan["url"],
            "hyperlink_updated": plan["new_url"] is not None,
        }
        for plan in plans
    ]


def hyperlink_fields(reports: list[dict[str, Any]]) -> dict[str, Any]:
    """Return ``{"hyperlinks": reports}`` when non-empty, else ``{}``."""
    return {"hyperlinks": reports} if reports else {}


def _final_reports(text: Any, anchor: Any, plans: list[dict[str, Any]],
                   written: set[str], measured: bool) -> list[dict[str, Any]]:
    """Reports for what the document holds after the edit.

    *written* has the old URLs of the links that got their new URL. A link that no live
    run of the paragraph carries any more is reported with "" as its URL (runs inside
    a tracked deletion are ignored). *measured* is False when the replacement broke the
    paragraph, so the rest of the link may be in a later one and the old URL is reported.
    """
    left: set[str] = set()
    state = DeletionState()
    for p in _iter_portions(text, anchor.getStart(), include_redlines=True):
        if _prop(p, "TextPortionType") == "Redline":
            state.apply_marker(p)
            continue
        if not state.in_delete and p.getString():
            left.add(_prop(p, _URL))
    reports = []
    for plan in plans:
        url = plan["url"]
        if url in written:
            after = plan["new_url"]
        else:
            after = url if url in left or not measured else ""
        reports.append({
            "hyperlink_url": url,
            "hyperlink_url_after": after,
            "hyperlink_updated": url in written,
        })
    return reports


def _paragraph_cursor(text: Any, pos: Any) -> Any:
    cursor = text.createTextCursorByRange(pos)
    cursor.gotoStartOfParagraph(False)
    cursor.gotoEndOfParagraph(True)
    return cursor


def _same_point(text: Any, left: Any, right: Any) -> bool:
    return uno_read(lambda: int(text.compareRegionStarts(left, right)) == 0, False)


def _paragraph_edges(text: Any, text_range: Any) -> tuple[Any, Any, bool]:
    """Collapsed cursors at the start and end of the paragraph that contains the match start.

    ``single`` is False when the match ends in a different paragraph — prefix/suffix
    measurement would then pair text from two paragraphs.
    """
    start_cur = text.createTextCursorByRange(text_range.getStart())
    start_cur.gotoStartOfParagraph(False)
    end_probe = text.createTextCursorByRange(text_range.getEnd())
    end_probe.gotoStartOfParagraph(False)
    single = _same_point(text, start_cur.getStart(), end_probe.getStart())
    end_cur = text.createTextCursorByRange(text_range.getEnd() if single else text_range.getStart())
    end_cur.gotoEndOfParagraph(False)
    return start_cur, end_cur, single


def _between(text: Any, start_pos: Any, end_pos: Any) -> str:
    cursor = text.createTextCursorByRange(start_pos)
    cursor.gotoRange(end_pos, True)
    return str(cursor.getString() or "")


def _iter_portions(text: Any, anchor_pos: Any, *, include_redlines: bool = False) -> Iterator[Any]:
    """Portions of the paragraph containing *anchor_pos*."""
    cursor = _paragraph_cursor(text, anchor_pos)
    para_enum = cursor.createEnumeration()
    for para in iter_elements(para_enum):
        if hasattr(para, "supportsService") and not para.supportsService(
                "com.sun.star.text.Paragraph"):
            continue
        portion_enum = para.createEnumeration()
        for portion in iter_elements(portion_enum):
            # Skip tracked-change redline markers; they do not hold text or link properties.
            if (
                not include_redlines
                and uno_read(lambda: str(portion.getPropertyValue("TextPortionType") or ""), "") == "Redline"
            ):
                continue
            yield portion


def capture_outline_hyperlinks(text_range: Any) -> OutlineSnapshot:
    """Read ``|outline`` links that overlap *text_range*, and the paragraph text around it."""
    text = text_range.getText()
    matched = str(text_range.getString() or "")

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
    multi_preserve = False
    props: dict[str, Any] = {}
    fully_linked = True
    runs: set[tuple[str, str, str]] = set()
    for portion in _iter_portions(text, text_range.getStart()):
        url = _prop(portion, _URL)
        if portion.getString():
            runs.add((url, _prop(portion, _NAME), _prop(portion, _TARGET)))
        if not _ranges_overlap(text, portion, text_range):
            continue
        if not url:
            if portion.getString():
                fully_linked = False
            continue
        # Taken after the unlinked skip so a partly linked match keeps the link's name/target.
        if not props:
            props = read_link_props(portion)
        if not url.endswith(_OUTLINE_SUFFIX):
            # First bookmark (or other) URL on the match. Not rewritten; restored
            # onto the replaced characters if the text replace clears it.
            if not preserve_url:
                preserve_url = url
            elif url != preserve_url:
                multi_preserve = True
            continue
        if url in seen:
            continue
        seen.add(url)
        links.append(OutlineLink(url, _prop(portion, _NAME), _prop(portion, _TARGET)))
    whole_paragraph = len(runs) == 1 and all(url for url, _name, _target in runs)
    return OutlineSnapshot(
        links, prefix, suffix, matched, single, preserve_url, props,
        fully_linked, multi_preserve, whole_paragraph,
    )


def _paragraph_text(text: Any, anchor: Any) -> str:
    cursor = _paragraph_cursor(text, anchor.getStart())
    return str(cursor.getString() or "")


def measured_replacement(anchor: Any, snapshot: OutlineSnapshot) -> str | None:
    """Visible text that replaced the match, taken from the paragraph after the edit.

    None when the match spanned paragraphs or the surrounding text no longer lines up,
    so the caller falls back to the plain string it asked to insert.
    """
    if anchor is None or not snapshot.single_paragraph:
        return None
    text = anchor.getText()
    para = _paragraph_text(text, anchor)
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
        # A failed write would leave a stale outline target, so the caller rolls the text
        # change back with this error.
        raise ToolExecutionError(
            "Could not update the outline hyperlink: %s" % exc,
            code="HYPERLINK_UPDATE_FAILED",
        ) from exc


def _select_replacement(text: Any, anchor: Any, snapshot: OutlineSnapshot, new_plain: str) -> Any | None:
    """Cursor covering the replaced characters, measured from the paragraph start.

    A cursor saved at the match start does not stay there: replace_preserving_format
    inserts and deletes text around it, pushing other cursors at the same position
    forward. Painting from that drifted cursor writes the URL onto the following
    bookmark. The paragraph start plus the saved prefix length is still the
    replacement, including after that drift.
    """
    # goRight counts code points, as len() does (breakiterator_unicode.cxx: nextCharacters).
    if not snapshot.single_paragraph:
        return None
    try:
        cursor = text.createTextCursorByRange(anchor.getStart())
        cursor.gotoStartOfParagraph(False)
        if snapshot.prefix and not go_right(cursor, len(snapshot.prefix), False):
            return None
        if new_plain and not go_right(cursor, len(new_plain), True):
            return None
    except Exception:
        return None
    return cursor


def write_link_look(cursor: Any, props: dict[str, Any]) -> None:
    """Set *props* on *cursor* after its HyperLinkURL write. Uses uno_write: disposal
    re-raises, any other failure is logged at debug and skipped.

    A later cursor over the same characters does not stick after a splitting
    HyperLinkURL write. The setup path (URL, then look on that cursor) does.
    """
    for name, value in props.items():
        if not uno_write(lambda: cursor.setPropertyValue(name, value)):
            log.debug("write_link_look: could not set %s", name)


def _plan_props(plan: dict[str, Any], props: dict[str, Any]) -> dict[str, Any]:
    """*props* with the name and target the plan renames."""
    out = dict(props)
    for name, key in ((_NAME, "new_name"), (_TARGET, "new_target")):
        if plan[key] is not None:
            out[name] = plan[key]
    return out


def read_link_look(obj: Any, char_props: tuple[str, ...] = _CHAR_LOOK) -> dict[str, Any]:
    """Direct Char* on *obj* (getPropertyState is DIRECT_VALUE) plus the link's
    character style names when *obj* is a hyperlink. Inherited values are not copied.

    While portion-level getPropertyState is generally unreliable for paragraph-level
    attributes (which report DEFAULT_VALUE), hint-level overrides on hyperlinks do
    reliably report DIRECT_VALUE and take precedence over the link character style.
    Paragraph-level direct formatting reaches newly inserted rows via the paragraph-break
    split rather than property cloning.
    """
    props: dict[str, Any] = {}
    for name in char_props:
        state = uno_read(lambda: obj.getPropertyState(name), None)
        if getattr(state, "value", None) == "DIRECT_VALUE":
            value = uno_read(lambda: obj.getPropertyValue(name), None)
            if value is not None:
                props[name] = value
    for name in LINK_STYLE_PROPS:
        value = uno_read(lambda: obj.getPropertyValue(name), None)
        if isinstance(value, str) and value:
            props[name] = value
    return props


def read_link_props(portion: Any) -> dict[str, Any]:
    """Name, target and look to put back after a HyperLinkURL write."""
    return {_NAME: _prop(portion, _NAME), _TARGET: _prop(portion, _TARGET), **read_link_look(portion)}


def write_link(cursor: Any, url: str, props: dict[str, Any]) -> None:
    """Make *cursor* a link to *url* ("" unlinks it). Call with recording off."""
    # Undo of a plain overwrite drops the old link; a reset first records it in the undo history
    # (SwDoc::ResetAttrs, docfmt.cxx). The reset also clears name, target and link styles,
    # so *props* puts them back.
    try:
        cursor.setPropertyToDefault(_URL)
    except Exception as exc:
        raise ToolExecutionError(
            "Could not update the outline hyperlink: %s" % exc,
            code="HYPERLINK_UPDATE_FAILED",
        ) from exc
    if url:
        _set_prop(cursor, _URL, url)
        write_link_look(cursor, props)


def _snapshot_char_look(text: Any, anchor: Any) -> list[tuple[int, int, dict[str, Any]]]:
    """Direct Char* and link styles of each non-empty portion, from the paragraph start.

    Taken after the text replace and before HyperLinkURL is written. Offsets stay
    valid through the URL write because that write does not change the characters.
    """
    runs: list[tuple[int, int, dict[str, Any]]] = []
    offset = 0
    start = anchor.getStart()
    for portion in _iter_portions(text, start):
        chunk = str(portion.getString() or "")
        if chunk:
            props = read_link_look(portion)
            if props:
                runs.append((offset, offset + len(chunk), props))
        offset += len(chunk)
    return runs


def _look_covering(runs: list[tuple[int, int, dict[str, Any]]],
                   start: int, end: int) -> dict[str, Any]:
    """Char* of the snapshotted run that covers *start*, else the first overlap."""
    for lo, hi, props in runs:
        if lo <= start < hi:
            return props
    for lo, hi, props in runs:
        if lo < end and hi > start:
            return props
    return {}


def _paint(text: Any, anchor: Any, snapshot: OutlineSnapshot, new_plain: str,
           url: str, props: dict[str, Any], *, required: bool) -> bool:
    """Make the replaced characters a link to *url* with *props*.

    Return False when the range cannot be selected. *required* raises: an outline
    update that cannot see the new text would leave the link stale. A bookmark
    put-back is best-effort so a selection failure does not roll back a text
    replace that did not ask to change the target.

    Skip when those characters already have *url*: a preserve-format replace
    keeps the URL on the whole TOC line, and a title-only rewrite is a subrange
    write (see the module docstring).
    """
    cursor = _select_replacement(text, anchor, snapshot, new_plain)
    selected = None if cursor is None else uno_read(lambda: str(cursor.getString() or ""), None)
    if cursor is None or selected != new_plain:
        if required:
            raise ToolExecutionError(
                "Could not select the replaced text to update its outline hyperlink.",
                code="HYPERLINK_UPDATE_FAILED",
            )
        log.debug("outline hyperlink: could not reselect the replaced text to restore its URL")
        return False
    if _prop(cursor, _URL) != url:
        write_link(cursor, url, props)
    return True


def _put_back_url(snapshot: OutlineSnapshot) -> str:
    if not snapshot.fully_linked or snapshot.multi_preserve:
        return ""
    if len(snapshot.links) == 1 and not snapshot.preserve_url:
        return snapshot.links[0].url
    if not snapshot.links and snapshot.preserve_url:
        return snapshot.preserve_url
    return ""


def restore_outline_hyperlinks(anchor: Any, snapshot: OutlineSnapshot,
                               fallback_plain: str | None,
                               override: str | None) -> list[dict[str, Any]]:
    """Write the planned outline URL onto the whole span, using *anchor* (the match start).

    *fallback_plain* is the plain text the caller inserted, used when the paragraph
    can no longer be measured. Returns the public before/after reports.
    """
    if anchor is None:
        if snapshot.links or override:
            raise ToolExecutionError(
                "Could not locate the replaced text to update its outline hyperlink.",
                code="HYPERLINK_UPDATE_FAILED",
            )
        return []

    new_plain = measured_replacement(anchor, snapshot)
    was_measured = new_plain is not None
    if new_plain is None:
        new_plain = fallback_plain if fallback_plain is not None else snapshot.matched

    plans = plan_outline_updates(snapshot.links, snapshot.matched, new_plain, override)
    # An override with no outline link is rejected in apply_content
    # before the edit. Do not paint it onto a bookmark or plain text here.

    writable = [plan for plan in plans if plan["new_url"] is not None]
    try:
        text = anchor.getText()
    except Exception as exc:
        raise ToolExecutionError(
            "Could not locate the replaced text to update its outline hyperlink: %s" % exc,
            code="HYPERLINK_UPDATE_FAILED",
        ) from exc
    # A parked anchor can land after imported blocks, so a multi-paragraph
    # match is only measured when the anchor paragraph still starts with prefix.
    measured = was_measured or (
        not snapshot.single_paragraph
        and _paragraph_text(text, anchor).startswith(snapshot.prefix)
    )
    # A title-only HyperLinkURL write repaints Internet-link chrome
    # (module docstring). _paint skips it when the replacement already
    # has the new URL; otherwise the snapshotted look goes back on the
    # same cursor right after the assignment.
    look_runs = _snapshot_char_look(text, anchor)
    title_props = dict(
        _look_covering(
            look_runs, len(snapshot.prefix), len(snapshot.prefix) + len(new_plain)
        )
    )
    # Name, target and link styles come from the pre-replace snapshot because an HTML
    # replace drops the link and an unlinked portion reports the default style names.
    title_props.update({k: v for k, v in snapshot.props.items() if k not in _CHAR_LOOK})

    if not writable:
        # Put the same target back on the replaced characters only when they
        # belonged to that one link. An untracked preserve-format replace keeps
        # HyperLinkURL except on a one-character run; HTML replaces and recorded
        # replaces can clear it, which would unlink "New" and leave only " title".
        # Painting links[0] or the bookmark across a match that also overlaps another
        # link would give the neighbor the wrong target; those characters stay
        # unlinked instead.
        put_back = _put_back_url(snapshot)
        if put_back and new_plain:
            # When one outline link spans the whole paragraph, write the link with
            # the pre-replace props on a whole-paragraph cursor so page-digit
            # edits do not split the run into navy underlined digits.
            restored = False
            if snapshot.whole_paragraph and snapshot.links and snapshot.single_paragraph:
                para = _paragraph_text(text, anchor)
                if para == snapshot.prefix + new_plain + snapshot.suffix:
                    cursor = _paragraph_cursor(text, anchor.getStart())
                    write_link(cursor, put_back, snapshot.props)
                    restored = True
            if not restored:
                _paint(text, anchor, snapshot, new_plain, put_back, snapshot.props, required=False)
        return _final_reports(text, anchor, plans, set(), measured)

    by_url = {plan["url"]: plan for plan in writable}

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
            pending.append((portion.getStart(), portion.getEnd(), plan, read_link_props(portion)))
        except Exception as exc:
            raise ToolExecutionError(
                "Could not locate an outline hyperlink run to update: %s" % exc,
                code="HYPERLINK_UPDATE_FAILED",
            ) from exc
    for start, end, plan, portion_props in pending:
        cursor = text.createTextCursorByRange(start)
        cursor.gotoRange(end, True)
        write_link(cursor, plan["new_url"], _plan_props(plan, portion_props))

    # len(writable) == 1 is not "one link": another outline URL may have been
    # left unchanged, or a bookmark may overlap the match. Painting this target
    # across the replacement would give those neighbors the wrong URL. The HTML
    # path still needs the paint when the match was that single outline link,
    # because setString("") drops HyperLinkURL on the new characters.
    one_outline = len(snapshot.links) == 1 and not snapshot.preserve_url
    # A partly linked match whose link survived the replace was already updated portion by portion.
    paint = writable[0] if one_outline and len(writable) == 1 and (snapshot.fully_linked or not pending) else None

    # An empty replacement that deletes the whole link leaves no portion to paint.
    # That delete succeeds: nothing with a stale URL remains. A non-empty
    # replacement that cannot be reselected still raises inside _paint.
    written = {plan["url"] for _start, _end, plan, _props in pending}
    if paint is not None and new_plain:
        _paint(text, anchor, snapshot, new_plain, paint["new_url"],
               _plan_props(paint, title_props), required=True)
        written.add(paint["url"])

    reports = _final_reports(text, anchor, plans, written, measured)
    log.debug("outline hyperlink updated: %s", reports)
    return reports


_RECORDING_NOTE = (
    "Link target not changed while track changes is recording; "
    "update it after the edit is accepted."
)


def _keep_links_while_recording(doc: Any, anchor: Any, snapshot: OutlineSnapshot,
                                plain: str, override: str | None) -> list[dict[str, Any]]:
    """Put the matched link's own URL on the tracked insertion.

    A HyperLinkURL write on existing text becomes a Format redline, and rejecting that drops the
    link. The struck text keeps its link, so only the inserted runs are written, untracked: Accept
    keeps the link on the new text and Reject removes it with the insertion. No URL is renamed.
    """
    url = _put_back_url(snapshot)
    raw = measured_replacement(anchor, snapshot)
    if url and raw:
        text = anchor.getText()
        # Collected before writing: setPropertyValue can split a portion mid-enumeration.
        offset, stop = -len(snapshot.prefix), len(raw)
        unlinked = []
        state = DeletionState()
        for portion in _iter_portions(text, anchor.getStart(), include_redlines=True):
            if _prop(portion, "TextPortionType") == "Redline":
                state.apply_marker(portion)
                continue
            chunk = str(portion.getString() or "")
            if (
                not state.in_delete
                and chunk
                and offset >= 0
                and offset + len(chunk) <= stop
                and _prop(portion, _URL) != url
            ):
                unlinked.append((portion.getStart(), portion.getEnd()))
            offset += len(chunk)
        with record_changes(doc, False):
            for start, end in unlinked:
                cursor = text.createTextCursorByRange(start)
                cursor.gotoRange(end, True)
                write_link(cursor, url, snapshot.props)
    plans = plan_outline_updates(snapshot.links, snapshot.matched, plain, override)
    reports = _final_reports(anchor.getText(), anchor, plans, set(), raw is not None)
    for plan, report in zip(plans, reports):
        if plan["new_url"] is not None and report["hyperlink_url_after"]:
            report["hyperlink_note"] = _RECORDING_NOTE
    return reports


def restore_links(doc: Any, anchor: Any, snapshot: OutlineSnapshot,
                  plain: str, override: str | None) -> list[dict[str, Any]]:
    """Fix the links of a replaced match: rename outline targets, or only keep them while recording."""
    if is_recording_changes(doc):
        return _keep_links_while_recording(doc, anchor, snapshot, plain, override)
    return restore_outline_hyperlinks(anchor, snapshot, plain, override)


def _similar_titles(
    before: list[tuple[int, str]], after: list[tuple[int, str]], old_free: set[str], new_free: set[str],
) -> dict[str, str]:
    """Pair each title in *old_free* with the one in *new_free* it clearly resembles.

    Both titles must be the other's single best match among all headings of the same level.
    Headings that kept their title compete too: "Q1 results" resembles the kept "Q2 results"
    as much as the added "Q4 results", so it is left unpaired.
    """
    old_keys = [_LEADING_LABEL_RE.sub("", old.casefold(), count=1) for _lv, old in before]
    new_keys = [_LEADING_LABEL_RE.sub("", new.casefold(), count=1) for _lv, new in after]
    scores: dict[tuple[int, int], float] = {}
    matcher = SequenceMatcher(autojunk=False)
    for j, ((new_level, new), new_key) in enumerate(zip(after, new_keys)):
        matcher.set_seq2(new_key)  # SequenceMatcher caches seq2, so it changes least often
        for i, ((old_level, old), old_key) in enumerate(zip(before, old_keys)):
            if old_level != new_level or (old not in old_free and new not in new_free):
                continue
            matcher.set_seq1(old_key)
            # The two quick ratios are cheap upper bounds of ratio().
            if matcher.real_quick_ratio() < _SIMILAR_TITLE or matcher.quick_ratio() < _SIMILAR_TITLE:
                continue
            ratio = matcher.ratio()
            if ratio >= _SIMILAR_TITLE:
                scores[i, j] = ratio

    def single_best(axis: int) -> set[tuple[int, int]]:
        groups: dict[int, list[tuple[float, tuple[int, int]]]] = {}
        for pair, ratio in scores.items():
            groups.setdefault(pair[axis], []).append((ratio, pair))
        best = set()
        for group in groups.values():
            top = max(ratio for ratio, _pair in group)
            winners = [pair for ratio, pair in group if ratio == top]
            if len(winners) == 1:
                best.add(winners[0])
        return best

    pairs = [(before[i][1], after[j][1]) for i, j in sorted(single_best(0) & single_best(1))]
    return {old: new for old, new in pairs if old in old_free and new in new_free}


def _number_label(title: str) -> str:
    """The casefolded title up to its first number ("step 1" of "Step 1: Install"), or ""."""
    folded = title.casefold()
    match = _NUMBER_RE.search(folded)
    return folded[: match.end()] if match else ""


def _number_passed_on(old: str, new: str, old_labels: Counter[str], new_labels: Counter[str]) -> bool:
    """True unless *old* and *new* differ only in number and nothing shows a renumbering.

    In a renumbering another heading takes over the old number (or its parent number) or gave
    up the new one. A label several headings share ("Sales 2023: Europe", "Sales 2023: Asia")
    is not such a number, and a title that is only a number ("Appendix A") has no text to
    recognise it by.
    """
    was, now = old.casefold(), new.casefold()
    if was == now or _NUMBER_RE.sub("#", was) != _NUMBER_RE.sub("#", now):
        return True
    a, b = _number_label(old), _number_label(new)
    if not re.search(r"\w", was[len(a):]):
        return False
    while a != b:
        if (old_labels[a] == 1 and new_labels[a] > 0) or (new_labels[b] == 1 and old_labels[b] > 0):
            return True
        parent_a, parent_b = re.sub(r"\.\d+$", "", a), re.sub(r"\.\d+$", "", b)
        if parent_a == a or parent_b == b:
            return False
        a, b = parent_a, parent_b  # a subsection follows its chapter's number
    return False


def heading_renames(
    before: list[tuple[int, str]],
    after: list[tuple[int, str]],
) -> tuple[dict[str, str], list[str]]:
    """Return ``(renames, lost)``: old title to new title, and old titles with no successor.

    A rename joins two headings of one level whose titles are each unique on their own side
    and absent from the other. Titles are paired by similarity first; after a move, titles
    that differ only in a number pair only when the number was passed on. Headings with no
    similar title (a translation, say) are paired by position inside a ``replace`` stretch
    whose levels match, but only when no known heading changed place: after a move, position
    says nothing.
    """
    old_titles = [text for _level, text in before]
    new_titles = [text for _level, text in after]
    old_counts, new_counts = Counter(old_titles), Counter(new_titles)
    old_free = {t for t, n in old_counts.items() if n == 1 and t not in new_counts}
    new_free = {t for t, n in new_counts.items() if n == 1 and t not in old_counts}
    renames = _similar_titles(before, after, old_free, new_free)

    # Align with each similar title counted as unchanged.
    old_of = {new: old for old, new in renames.items()}
    aligned = [old_of.get(title, title) for title in new_titles]
    by_position: dict[str, str] = {}
    removed: set[str] = set()
    added: set[str] = set()
    for tag, i1, i2, j1, j2 in SequenceMatcher(None, old_titles, aligned, autojunk=False).get_opcodes():
        if tag == "equal":
            continue
        removed.update(old_titles[i1:i2])
        added.update(aligned[j1:j2])
        if tag == "replace" and [lv for lv, _t in before[i1:i2]] == [lv for lv, _t in after[j1:j2]]:
            by_position.update(
                (old, new)
                for old, new in zip(old_titles[i1:i2], new_titles[j1:j2])
                if old in old_free and new in new_free
            )
    if not removed & added:  # a title on both sides is a heading that moved
        renames.update(by_position)
    else:
        old_labels = Counter(map(_number_label, old_titles))
        new_labels = Counter(map(_number_label, new_titles))
        renames = {o: n for o, n in renames.items() if _number_passed_on(o, n, old_labels, new_labels)}
    return renames, [t for t in old_counts if t not in new_counts and t not in renames]


def _linked_ranges(doc: Any) -> list[Any]:
    """One range per hyperlink and paragraph, in every text of *doc* (cells, frames, notes, headers)."""
    search = doc.createSearchDescriptor()
    search.ValueSearch = False  # search for the presence of HyperLinkURL, not a value
    attr: Any = uno.createUnoStruct("com.sun.star.beans.PropertyValue")
    attr.Name = _URL
    search.setSearchAttributes((attr,))
    found = doc.findAll(search)
    return [found.getByIndex(i) for i in range(found.getCount())]


_OUTLINE_URL_RE = re.compile(r"#(.*)(\|outline|%7Coutline)", re.I | re.S)
_OUTLINE_NUM_PREFIX_RE = re.compile(r"\d+\.")


def _renamed_url(url: str, successors: dict[str, str | None]) -> str | None:
    """The URL *url* should have now: itself when its heading kept its title, None when it is lost.

    *successors* maps each known title to its title now. The longest match wins: ``SwDoc::GotoOutline``
    (sw/source/core/doc/docnum.cxx) tries a live automatic number first, and titles alone do not
    show automatic numbering, so the full title is preferred here.
    """
    match = _OUTLINE_URL_RE.fullmatch(url)
    if match is None:
        return url
    body, suffix = match.group(1), match.group(2)
    if body in successors:
        new = successors[body]
        return None if new is None else "#" + new + suffix
    decoded = unquote(body)
    pos = 0
    while decoded[pos:] not in successors:
        num = _OUTLINE_NUM_PREFIX_RE.match(decoded, pos)
        if num is None:
            return url
        pos = num.end()
    title = decoded[pos:]
    new = successors[title]
    if new is None or new == title:  # a kept title keeps the URL's own encoding
        return None if new is None else url
    target = decoded[:pos] + new
    return "#" + (quote(target) if decoded != body else target) + suffix


def retarget_outline_links(
    doc: Any, successors: dict[str, str | None],
) -> tuple[int, dict[str, StaleReason]]:
    """Update ``#old|outline`` portions across *doc* according to *successors*.

    *successors* maps each known heading title to its title now (itself when kept,
    ``None`` when lost). Links to titles that remain in the document are left alone.
    When recording changes, a tracked attribute write keeps the new URL, but
    ``lcl_SetRedline`` (``DocumentContentOperationsManager.cxx``) clears
    ``RES_TXTATR_INETFMT`` from the saved old attributes so ``lcl_RejectRedline``
    (``DocumentRedlineManager.cxx``, ``ResetAttrs``) loses the old link on
    Reject. Plain-text runs therefore re-insert the text with the new URL and
    delete the old run, while runs already inside a tracked Insert are written
    directly because Reject removes them whole. Returned stale URLs carry a reason:
    field runs that cannot be retyped (``field``), or a title mapped to ``None``
    that no heading replaces (``no_heading``, left unchanged).
    """
    recording = is_recording_changes(doc)
    stale: dict[str, StaleReason] = {}
    count = 0
    for linked in _linked_ranges(doc):
        text = linked.getText()
        runs: list[tuple[Any, str, str, bool]] = []
        blocked: set[str] = set()  # per paragraph: one field link must not block the others
        for portion in _iter_portions(text, linked.getStart()):
            url = _prop(portion, _URL)
            if not url or not portion.getString():
                continue
            new_url = _renamed_url(url, successors)
            if new_url == url:
                continue
            kinds = redline_kinds_covering(doc, portion)
            if "Delete" in kinds:
                continue
            if new_url is None:
                stale[url] = "no_heading"
                continue
            tracked = recording and "Insert" not in kinds
            if tracked and _prop(portion, "TextPortionType") != "Text":
                blocked.add(url)
            runs.append((portion, url, new_url, tracked))
        # Last run first: retyping a run moves the text after it.
        for portion, url, new_url, tracked in reversed(runs):
            if url in blocked:
                stale[url] = "field"
                continue
            props = read_link_props(portion)
            target = text.createTextCursorByRange(portion)
            if not tracked:
                with record_changes(doc, False):
                    write_link(target, new_url, props)
            else:
                start = text.createTextCursorByRange(target.getStart())
                retyped = text.createTextCursorByRange(target.getEnd())
                # bAbsorb: the cursor covers the inserted text, which takes the run's formatting.
                text.insertString(retyped, target.getString(), True)
                with record_changes(doc, False):
                    write_link(retyped, new_url, props)
                # The insertion stretched the run's own range, so delete up to the new text.
                start.gotoRange(retyped.getStart(), True)
                start.setString("")
            count += 1
    return count, stale
