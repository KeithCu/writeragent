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
"""Writer search: search tool and shared text-find helpers for edit/dry_run paths."""

from __future__ import annotations

import logging
import re as re_mod
from dataclasses import dataclass
from itertools import islice
from typing import TYPE_CHECKING, Any, Callable, Iterable, Iterator, Literal, cast

from plugin.doc.text_helpers import (
    FAILED,
    TrackedDeletions,
    get_string_without_tracked_deletions,
    go_right,
    iter_elements,
    normalize_linebreaks,
    starts_at_or_after,
    visible_range_text,
    with_view_cursor_left_body_locked,
)
from plugin.framework.errors import make_tool_error, reraise_if_disposed, uno_read
from plugin.framework.tool import ToolBase, ToolBaseDummy
from plugin.framework.uno_context import get_ctx, get_service_manager, uno_same
from plugin.framework.worker_pool import run_in_background
from plugin.writer import html_import
from plugin.writer.review_scan import document_has_tracked_deletions

if TYPE_CHECKING:
    from plugin.framework.tool import ToolContext


log = logging.getLogger("writeragent.writer")

MAX_SEARCH_REPLACEMENTS = 200
DEFAULT_SEARCH_RESULTS = 20
SEARCH_CASE_SENSITIVE_DEFAULT = False


def case_sensitive_param() -> dict[str, Any]:
    """Schema property for search tools whose omitted case_sensitive means case-insensitive.

    The exact-first ladder tools use LADDER_CASE_SENSITIVE_DESCRIPTION and no default.
    """
    return {
        "type": "boolean",
        "default": SEARCH_CASE_SENSITIVE_DEFAULT,
        "description": "Case-sensitive match. The result includes matched_case_sensitive for the mode that ran.",
    }


def resolve_case_sensitive(raw: Any) -> bool:
    """Missing and JSON null both use SEARCH_CASE_SENSITIVE_DEFAULT."""
    if raw is None:
        return SEARCH_CASE_SENSITIVE_DEFAULT
    return bool(raw)


MAX_SEARCH_HITS = 10000
_SHAPE_REGEX_TIMEOUT_SEC = 0.25
_shape_regex_inflight: Any = None

# Non-breaking / exotic spaces -> ASCII space. Length-preserving (each maps to a
# single BMP char) so character offsets into the document text stay valid. NBSP
# (U+00A0) in particular is a common artifact of prior edits and breaks literal
# search when old_content uses a normal space.
_SPACE_CODEPOINTS = (
    0x00A0,  # NO-BREAK SPACE
    0x202F,  # NARROW NO-BREAK SPACE
    0x2007,  # FIGURE SPACE
    0x2009,  # THIN SPACE
    0x2000,  # EN QUAD
    0x2001,  # EM QUAD
    0x2002,  # EN SPACE
    0x2003,  # EM SPACE
    0x2004,  # THREE-PER-EM SPACE
    0x2005,  # FOUR-PER-EM SPACE
    0x2006,  # SIX-PER-EM SPACE
    0x2008,  # PUNCTUATION SPACE
    0x200A,  # HAIR SPACE
    0x205F,  # MEDIUM MATHEMATICAL SPACE
    0x3000,  # IDEOGRAPHIC SPACE
)
SPACE_NORMALIZE_MAP = {cp: " " for cp in _SPACE_CODEPOINTS}
HORIZONTAL_SPACE_CLASS = r"[ \t" + "".join("\\u%04x" % cp for cp in _SPACE_CODEPOINTS) + "]"

def _char_class(cps: tuple[int, ...]) -> str:
    return "[" + "".join(f"\\u{cp:04x}" for cp in cps) + "]"


# Quotes and dashes folding for lenient search. Straight or curly quotes and
# hyphen/dash variants match across styles so subtle punctuation differences
# between user prompts and document text do not break find-and-replace.
_SINGLE_QUOTE_CODEPOINTS = (0x0027, 0x2018, 0x2019, 0x201A, 0x201B)
_DOUBLE_QUOTE_CODEPOINTS = (0x0022, 0x201C, 0x201D, 0x201E, 0x201F)
_DASH_CODEPOINTS = (0x002D, 0x2010, 0x2011, 0x2012, 0x2013, 0x2014, 0x2015, 0x2212)

SINGLE_QUOTE_CLASS = _char_class(_SINGLE_QUOTE_CODEPOINTS)
DOUBLE_QUOTE_CLASS = _char_class(_DOUBLE_QUOTE_CODEPOINTS)
DASH_CLASS = _char_class(_DASH_CODEPOINTS)

_SINGLE_QUOTE_RE = re_mod.compile(SINGLE_QUOTE_CLASS)
_DOUBLE_QUOTE_RE = re_mod.compile(DOUBLE_QUOTE_CLASS)
_DASH_RE = re_mod.compile(DASH_CLASS)
_FOLDABLE_RE = re_mod.compile(
    _char_class(_SINGLE_QUOTE_CODEPOINTS + _DOUBLE_QUOTE_CODEPOINTS + _DASH_CODEPOINTS)
)

_QUOTE_FOLD_MAP = {
    **{cp: "'" for cp in _SINGLE_QUOTE_CODEPOINTS if cp != 0x0027},
    **{cp: '"' for cp in _DOUBLE_QUOTE_CODEPOINTS if cp != 0x0022},
}
_PUNCT_FOLD_MAP = {
    **_QUOTE_FOLD_MAP,
    **{cp: "-" for cp in _DASH_CODEPOINTS if cp != 0x002D},
}

_FOLD_NORMALIZE_MAP = {
    **SPACE_NORMALIZE_MAP,
    **_PUNCT_FOLD_MAP,
}


@dataclass(frozen=True)
class MatchMode:
    """One literal-search pass for an edit or anchor tool.

    case_sensitive means letters must match the needle's case.
    folded means straight/curly quotes and hyphen/dash variants match each other.
    Spaces always fold because normalize_search_string_for_find already collapsed
    the needle's whitespace.
    """

    case_sensitive: bool
    folded: bool

    def result_fields(self) -> dict[str, bool]:
        return {
            "matched_case_sensitive": self.case_sensitive,
            "matched_folded": self.folded,
        }


# Quote/dash folding and the matched_* result fields are explained once in
# APPLY_DOCUMENT_TARGET_RULES (plugin/framework/prompts.py).
LADDER_CASE_SENSITIVE_DESCRIPTION = (
    "Omit to match exact case first and ignore case only when nothing matches; "
    "true or false forces that mode."
)


class OccurrenceOutOfRange(IndexError):
    """Raised by find_occurrence_range when matches exist in *mode* but occurrence is past the end."""

    mode: MatchMode

    def __init__(self, message: str, mode: MatchMode) -> None:
        super().__init__(message)
        self.mode = mode


def explicit_case_sensitive(raw: Any) -> bool | None:
    """Return None when omitted or JSON null (use the case ladder), else bool(raw)."""
    if raw is None:
        return None
    return bool(raw)


def case_ladder(case_sensitive: bool | None) -> tuple[bool, ...]:
    """Return case-sensitivity passes: (True, False) when omitted, else (case_sensitive,)."""
    return (True, False) if case_sensitive is None else (case_sensitive,)


def match_modes(search_string: str, case_sensitive: bool | None) -> list[MatchMode]:
    """Ordered literal-search passes for *search_string*.

    Each case level tries exact quotes/dashes first, then folded quotes/dashes
    when the needle has both a foldable character and at least one letter or digit.
    """
    folds: tuple[bool, ...] = (
        (False, True)
        if _FOLDABLE_RE.search(search_string) and any(ch.isalnum() for ch in search_string)
        else (False,)
    )
    return [MatchMode(cs, f) for cs in case_ladder(case_sensitive) for f in folds]

# Literal shape and comment search folds typographic quotes; dashes and spaces
# stay literal here.
_SHAPE_QUOTE_FOLD = str.maketrans(_QUOTE_FOLD_MAP)

MIN_OVERSIZED_CHARS = 100
OVERSIZED_OLD_CONTENT_MESSAGE = (
    "old_content covers most of the document. Use target='full_document' with "
    "content only — do not pass the whole document as old_content."
)


def find_next_after_match(doc: Any, found: Any, sd: Any) -> Any:
    """Resume LO search after *found* — use getEnd() so the next hit starts after the match.

    A zero-length hit does not advance getEnd(), so returning None is what stops
    the edit-path loops at one empty range.
    """
    # A getString failure means the range is dead; it is not caught here because
    # findNext would raise the same error.
    if found.getString() == "":
        return None
    return doc.findNext(found.getEnd(), sd)


def validate_regex_pattern(pattern: str) -> str | None:
    """Return an error message when *pattern* is not valid Python regex, else None.

    Used by paths that run Python re directly (Draw shape text, Writer floating shape
    and comment sweeps). Writer body search uses LibreOffice's ICU engine and validates
    via validate_icu_regex instead.
    """
    try:
        re_mod.compile(pattern)
    except re_mod.error as rex:
        return str(rex)
    return None


def shape_sweep_skip_reason(pattern: str, use_regex: bool) -> str | None:
    """The warning to report when the shape and comment sweeps cannot run *pattern*, else None.

    The body search hands a regex to LibreOffice's ICU engine, but the floating-shape and
    comment sweeps run Python re. A pattern that compiles in ICU but not in Python (for
    example \\p{Lu}) is skipped there, and the caller reports this sentence instead of
    silently returning no shape or comment hits.
    """
    if use_regex and validate_regex_pattern(pattern) is not None:
        return "shape/comment text not searched: pattern uses ICU-only syntax"
    return None


ICU_REGEX_REJECTED = (
    "LibreOffice's ICU regex engine rejects it. Patterns use ICU syntax, which differs "
    "from Python re: named groups are (?<name>...) not (?P<name>...), backreferences by "
    "name are \\k<name>, and \\p{Lu} style classes are allowed"
)


def validate_icu_regex(ctx: Any, pattern: str) -> str | None:
    """Return an error message when LibreOffice's ICU engine cannot compile *pattern*, else None.

    LibreOffice reports a bad regex as "no match" (i18npool TextSearch::RESrchPrepare resets the
    matcher; RESrchFrwrd then returns no result), so a document search cannot tell a typo from
    zero hits. Compile ".|(?:pattern)" with the TextSearch2 service and search the one-character
    text "a": the "." branch always matches, so a hit means the whole pattern, including the
    user's part, compiled. Patterns with unclosed \\Q literal quotes or unbalanced parentheses
    matching the wrapper may produce false negatives or false positives. Main thread only, like
    every other UNO call in this module.
    """
    import uno

    smgr = get_service_manager(ctx)
    if smgr is None:
        ctx = get_ctx()
        smgr = get_service_manager(ctx)
    if smgr is None:
        return None
    ts = smgr.createInstanceWithContext("com.sun.star.util.TextSearch2", ctx)
    opts = cast("Any", uno.createUnoStruct("com.sun.star.util.SearchOptions2"))
    opts.AlgorithmType2 = uno.getConstantByName("com.sun.star.util.SearchAlgorithms2.REGEXP")
    opts.searchString = ".|(?:%s)" % pattern
    opts.Locale = uno.createUnoStruct("com.sun.star.lang.Locale")
    ts.setOptions2(opts)
    res = ts.searchForward("a", 0, 1)
    return None if int(res.subRegExpressions) > 0 else ICU_REGEX_REJECTED


def invalid_regex_tool_message(rex_msg: str) -> str:
    """Standard INVALID_REGEX message for tools."""
    return (
        "0 matches, and the pattern does not parse as a regular expression "
        "(%s). Fix the pattern, or set regex=false for a literal search." % rex_msg
    )


def search_results_cap_hint(returned: int, total_count: int) -> str:
    """Hint when a capped search omits matches (no offset pagination)."""
    return "Showing %d of %d. Narrow the query. This tool has no offset." % (returned, total_count)


def normalize_search_string_for_find(s: str) -> str:
    """Normalize linebreaks and collapse horizontal whitespace (incl. NBSP)."""
    return re_mod.sub(HORIZONTAL_SPACE_CLASS + "+", " ", normalize_linebreaks(s)).strip()


def prepare_old_content(raw: Any) -> str:
    """Normalize old_content needle: strip tags, collapse horizontal whitespace, preserve newlines."""
    if raw is None:
        return ""
    text = str(raw).strip()
    if not text:
        return ""
    plain = html_import.plain_text_if_markup(text)
    return normalize_search_string_for_find(plain)


# LibreOffice search also matches text a pending tracked change deleted, so matches inside
# one deletion are filtered out and a phrase that runs across a deletion is not found.
_TRACKED_DELETION_HINT = (
    " This document has pending tracked deletions: matches inside deleted text are left out,"
    " and a phrase that runs across a deletion is not found. Use a shorter piece on one side of it."
)


def parse_non_negative_int(
    raw: Any, *, name: str = "occurrence"
) -> tuple[int | None, str | None]:
    """Validate a non-negative integer parameter."""
    if raw is None:
        return None, None
    if type(raw) is not int or raw < 0:
        return None, f"{name} must be a non-negative integer."
    return raw, None


def shape_only_search_response(subject: str, shape_name: str, *, review: bool = False) -> dict[str, Any]:
    """SHAPE_TEXT_NEEDS_DELEGATION payload: status, code, message, replaced_count 0."""
    note = "In review mode it cannot be edited as a tracked change; " if review else ""
    verb = "edit" if review else "Edit"
    message = (
        f"{subject} is only inside a drawing shape / floating text box ('{shape_name}'). "
        f"{note}{verb} it via the shapes toolset (delegate_to_subagent domain='shapes')."
    )
    return {
        "status": "error",
        "code": "SHAPE_TEXT_NEEDS_DELEGATION",
        "message": message,
        "replaced_count": 0,
    }


def build_search_not_found_response(
    doc: Any,
    needle: str,
    *,
    case_sensitive: bool | None = None,
    use_regex: bool = False,
    all_matches: bool = False,
    lenient: bool = True,
    subject: str = "old_content",
    refuse_oversized: bool = False,
    review: bool = False,
) -> dict[str, Any]:
    """Standardized search not-found response dictionary.

    The builder sweeps floating shapes so any search tool whose needle is confined
    to a shape directs the caller to the shapes toolset. The subject parameter names
    the tool parameter visible to the model (such as 'old_content' or 'search_text').
    The document model doc and a non-empty search needle are required.
    When refuse_oversized is True (used by old_content replace tools), a needle covering
    more than 50% of the document body without a close mismatch returns OLD_CONTENT_TOO_LARGE.
    """
    if not needle:
        return {
            "status": "error",
            "code": "OLD_CONTENT_NOT_FOUND",
            "message": f"{subject} not found in document. Try a shorter, unique substring.",
            "replaced_count": 0,
        }

    shape_name = drawing_shape_containing(
        doc,
        needle,
        use_regex=use_regex,
        case_sensitive=resolve_case_sensitive(case_sensitive),
    )
    if shape_name:
        return shape_only_search_response(subject, shape_name, review=review)

    # Near-miss mismatch detection: check if there is a close match in the document.
    # We compute closest first so that a typo in a needle covering >50% of the body
    # still receives the helpful near-miss hint rather than an oversized refusal.
    closest = search_closest_mismatch(doc, needle, lenient=lenient) if not use_regex else None

    # The oversized refusal tells the model to use target='full_document', which
    # only the old_content edit tools have; callers opt in via refuse_oversized=True.
    if refuse_oversized and closest is None and not use_regex and old_content_covers_most(doc, len(needle)):
        return {
            "status": "error",
            "code": "OLD_CONTENT_TOO_LARGE",
            "message": OVERSIZED_OLD_CONTENT_MESSAGE,
            "replaced_count": 0,
            "oversized": True,
        }

    has_tracked = document_has_tracked_deletions(doc)
    hint = _TRACKED_DELETION_HINT if has_tracked else ""
    if all_matches:
        base_msg = "Replaced 0 occurrence(s). No matches found. Try a shorter substring." + hint
    else:
        base_msg = f"{subject} not found in document. Try a shorter, unique substring." + hint

    if closest:
        base_msg = f"{base_msg} {closest['message']}"

    resp: dict[str, Any] = {
        "status": "error",
        "code": "OLD_CONTENT_NOT_FOUND",
        "message": base_msg,
        "replaced_count": 0,
    }
    if closest:
        resp["closest_match"] = closest["closest_match"]
        resp["diff_at"] = closest["diff_at"]
    return resp


AnchorMissKind = Literal["start", "end", "different_text"]


def anchor_miss_message(kind: AnchorMissKind, *, tracked_deletions: bool = False) -> str:
    """Return an error message for a failed start_anchor or end_anchor search."""
    if kind == "start":
        msg = "start_anchor not found in document."
    elif kind == "different_text":
        msg = "end_anchor after start_anchor is not in the same text, so it cannot be one range."
    elif kind == "end":
        msg = "end_anchor not found after start_anchor."
    else:
        raise ValueError(f"Unknown anchor miss kind: {kind!r}")
    hint = _TRACKED_DELETION_HINT if tracked_deletions else ""
    return msg + hint


def find_text_ranges(
    model: Any,
    search: str,
    limit: int | None = None,
    case_sensitive: bool = SEARCH_CASE_SENSITIVE_DEFAULT,
    *,
    body_only: bool = False,
) -> list[dict[str, Any]]:
    """Find occurrences of *search*, returning a list of
    ``{"start": int, "end": int, "text": str}`` dicts.

    FOLLOW-UP: seeds ``findNext`` from body text but measures each hit with
    ``found.getText()``; ``start``/``end`` are offsets within that nested text
    object, not guaranteed global document indices (tables, frames, etc.).
    ``body_only=True`` drops other ``XText`` objects. A failure (closed document
    or UNO error) propagates. It is not an empty hit list. ``limit is None`` means
    no cap while ``limit <= 0`` returns no rows.
    """
    if limit is not None:
        if type(limit) is not int or limit <= 0:
            return []
    sd = _search_descriptor(model, search, False, case_sensitive)

    text = model.getText()
    cursor = text.createTextCursor()
    cursor.gotoStart(False)

    deletions = TrackedDeletions(model)
    matches = []
    found = model.findNext(cursor, sd)
    while found:
        if not deletions.covers(found) and (not body_only or uno_same(found.getText(), model.getText())):
            measure = found.getText().createTextCursor()
            measure.gotoStart(False)
            measure.gotoRange(found.getStart(), True)
            m_start = len(measure.getString())
            matched_text = found.getString()
            m_end = m_start + len(matched_text)
            matches.append({"start": m_start, "end": m_end, "text": matched_text})
            if limit is not None and len(matches) >= limit:
                break
        found = model.findNext(found.getEnd(), sd)
    return matches


def escape_for_lo_regex(s: str, *, fold: bool) -> str:
    """Escape regex characters and expand spaces; fold quotes and dashes when *fold* is True.

    Spaces always expand to HORIZONTAL_SPACE_CLASS+ because normalize_search_string_for_find
    collapsed horizontal whitespace in the needle. When *fold* is True, straight and curly
    quotes and hyphen/dash variants match interchangeably while keeping match length 1:1.
    """
    s = s.translate(SPACE_NORMALIZE_MAP)
    escaped = re_mod.sub(r'([\\^$.|?*+()\[\]{}])', r'\\\1', s)
    if fold:
        escaped = _SINGLE_QUOTE_RE.sub(lambda match: SINGLE_QUOTE_CLASS, escaped)
        escaped = _DOUBLE_QUOTE_RE.sub(lambda match: DOUBLE_QUOTE_CLASS, escaped)
        escaped = _DASH_RE.sub(lambda match: DASH_CLASS, escaped)
    return re_mod.sub(r' +', lambda match: HORIZONTAL_SPACE_CLASS + '+', escaped)


def _compare_normalize(s: str, mode: MatchMode) -> str:
    norm = normalize_linebreaks(s).translate(_FOLD_NORMALIZE_MAP if mode.folded else SPACE_NORMALIZE_MAP).strip()
    return norm if mode.case_sensitive else norm.lower()


def _paragraph_matches_part(
    para_text: str,
    part: str,
    mode: MatchMode,
    *,
    head: bool = False,
    tail: bool = False,
) -> tuple[bool, int | None]:
    """Compare a paragraph to a search part; return (ok, offset_len).

    *head*: part must match the start of the paragraph; offset_len is goRight length.
    *tail*: part must match the end; offset_len is goRight start offset from para start.
    Neither flag: full paragraph must equal part (after normalize).
    """
    para_text = normalize_linebreaks(para_text)
    part = normalize_linebreaks(part)
    expected_norm = _compare_normalize(part, mode)
    actual_norm = _compare_normalize(para_text, mode)
    if head:
        if not actual_norm.startswith(expected_norm):
            return False, None
        skipped_leading = len(para_text) - len(para_text.lstrip())
        return True, skipped_leading + len(part.strip())
    if tail:
        if not actual_norm.endswith(expected_norm):
            return False, None
        trimmed_trailing = para_text.rstrip()
        return True, max(0, len(trimmed_trailing) - len(part.strip()))
    return actual_norm == expected_norm, None


def _iter_descriptor_hits(doc: Any, sd: Any, after: Any = None) -> Iterator[Any]:
    deletions = TrackedDeletions(doc)
    found = doc.findFirst(sd) if after is None else doc.findNext(after, sd)
    while found is not None:
        if not deletions.covers(found):
            yield found
        found = find_next_after_match(doc, found, sd)


def _collect_capped(doc: Any, sd: Any, limit: int = MAX_SEARCH_REPLACEMENTS) -> list[Any]:
    return list(islice(_iter_descriptor_hits(doc, sd), limit))


def _lo_regex_descriptor(doc: Any, text: str, *, fold: bool) -> Any:
    sd = doc.createSearchDescriptor()
    sd.SearchRegularExpression = True
    sd.SearchString = escape_for_lo_regex(text, fold=fold)
    return sd


def _iter_lo_matches(
    doc: Any,
    candidate: str,
    mode: MatchMode,
    after: Any = None,
) -> Iterator[Any]:
    sd = _lo_regex_descriptor(doc, candidate, fold=mode.folded)
    sd.SearchCaseSensitive = mode.case_sensitive
    yield from _iter_descriptor_hits(doc, sd, after=after)


def _chain_hit_range(
    found: Any,
    parts: list[str],
    anchor_idx: int,
    mode: MatchMode,
    deletions: TrackedDeletions,
) -> Any | None:
    text = found.getText()

    anchor_cursor = text.createTextCursorByRange(found)
    anchor_cursor.gotoStartOfParagraph(False)
    anchor_cursor.gotoEndOfParagraph(True)
    anchor_para_text = get_string_without_tracked_deletions(anchor_cursor, deletions.state_at(anchor_cursor))

    is_anchor_first = (anchor_idx == 0)
    is_anchor_last = (anchor_idx == len(parts) - 1)

    ok, anchor_offset_len = _paragraph_matches_part(
        anchor_para_text,
        parts[anchor_idx],
        mode,
        head=is_anchor_last,
        tail=is_anchor_first
    )
    if not ok:
        return None

    first_start_cursor = None
    last_end_cursor = None

    def _offset_cursor(base: Any, off: int | None) -> Any:
        c = text.createTextCursorByRange(base)
        if off:
            c.goRight(off, False)
        return c

    anchor_start_cursor = text.createTextCursorByRange(found)
    anchor_start_cursor.gotoStartOfParagraph(False)

    if is_anchor_first:
        first_start_cursor = _offset_cursor(anchor_start_cursor, anchor_offset_len)
    if is_anchor_last:
        last_end_cursor = _offset_cursor(anchor_start_cursor, anchor_offset_len)

    forward_cursor = text.createTextCursorByRange(found)
    forward_cursor.gotoRange(found.getEnd(), False)

    for i in range(anchor_idx + 1, len(parts)):
        if not forward_cursor.gotoNextParagraph(False):
            return None

        check_cursor = text.createTextCursorByRange(forward_cursor)
        check_cursor.gotoEndOfParagraph(True)
        para_text = get_string_without_tracked_deletions(check_cursor, deletions.state_at(check_cursor))

        is_last = i == len(parts) - 1
        ok, offset_len = _paragraph_matches_part(para_text, parts[i], mode, head=is_last)
        if not ok:
            return None
        if is_last:
            last_end_cursor = _offset_cursor(forward_cursor, offset_len)

    backward_cursor = text.createTextCursorByRange(found)
    backward_cursor.gotoRange(found.getStart(), False)

    for i in range(anchor_idx - 1, -1, -1):
        if not backward_cursor.gotoPreviousParagraph(False):
            return None

        check_cursor = text.createTextCursorByRange(backward_cursor)
        check_cursor.gotoEndOfParagraph(True)
        para_text = get_string_without_tracked_deletions(check_cursor, deletions.state_at(check_cursor))

        is_first = i == 0
        ok, offset_len = _paragraph_matches_part(para_text, parts[i], mode, tail=is_first)
        if not ok:
            return None
        if is_first:
            first_start_cursor = _offset_cursor(backward_cursor, offset_len)

    start_range = first_start_cursor.getStart() if first_start_cursor else found.getStart()
    # last_end_cursor is collapsed (goRight with expand=False), so getEnd()
    # equals its start; getEnd() stays correct if that cursor is ever widened.
    end_range = last_end_cursor.getEnd() if last_end_cursor else found.getEnd()

    # Same-text combine failures propagate as UNO errors. Ranges in different
    # XText objects are skipped without counting as a dropped hit.
    if uno_same(start_range.getText(), end_range.getText()):
        result_range = text.createTextCursorByRange(start_range)
        result_range.gotoRange(end_range, True)
        return result_range

    log.debug("_chain_hit_range: chain crosses XText; no single range")
    return None


def _iter_chain_matches(
    doc: Any,
    parts: list[str],
    anchor_idx: int,
    mode: MatchMode,
    after: Any = None,
) -> Iterator[Any]:
    """Iterate matches across paragraph breaks via anchor search and chaining."""
    sd = _lo_regex_descriptor(doc, parts[anchor_idx], fold=mode.folded)
    sd.SearchCaseSensitive = mode.case_sensitive
    deletions = TrackedDeletions(doc)
    for found in _iter_descriptor_hits(doc, sd, after=after):
        match_range = _chain_hit_range(found, parts, anchor_idx, mode, deletions)
        if match_range is not None:
            yield match_range


def _anchor_index(parts: list[str]) -> int:
    for idx, part in enumerate(parts):
        if part.strip():
            return idx
    return -1


def _literal_sources(
    doc: Any,
    search_string: str,
    mode: MatchMode,
    after: Any,
) -> Iterator[Iterator[Any]]:
    """Yield lazy match iterators in priority order: typed literal, paragraph chain, collapsed."""
    yield _iter_lo_matches(doc, search_string, mode, after=after)

    parts = search_string.split("\n")
    if len(parts) > 1:
        anchor_idx = _anchor_index(parts)
        if anchor_idx != -1:
            yield _iter_chain_matches(doc, parts, anchor_idx, mode, after=after)

    collapsed = re_mod.sub(r" +", " ", search_string.replace("\n", " ")).strip()
    if collapsed and collapsed != search_string:
        yield _iter_lo_matches(doc, collapsed, mode, after=after)


def iter_literal_ranges(
    doc: Any,
    search_string: str,
    mode: MatchMode,
    after: Any = None,
    all_candidates: bool = False,
) -> Iterator[Any]:
    """Iterate matches in document order for one MatchMode pass.

    Sources run in order: typed needle, cross-paragraph chain (for multi-line
    needles), then newline-collapsed needle (HTML wrap artifact). The chain goes
    before the collapsed form because otherwise a heading-plus-paragraph needle
    lands on a one-paragraph phrase earlier in the document. When all_candidates
    is False, stops after the first source that yields a match. When True (e.g.
    for anchor end searches), yields hits across all sources so downstream
    filtering can find matches in the target text container or boundary.

    Not for whole-document replace: use apply_content(target='full_document') instead
    of passing the entire body as old_content.
    """
    if not search_string:
        return
    # Windows UNO getString() emits CRLF paragraph separators; normalize so
    # candidate generation and "\n" paragraph splitting never leave trailing "\r".
    search_string = normalize_linebreaks(search_string)

    for source in _literal_sources(doc, search_string, mode, after):
        matched = False
        for hit in source:
            matched = True
            yield hit
        if matched and not all_candidates:
            return


def find_literal_ranges(
    doc: Any,
    search_string: str,
    *,
    case_sensitive: bool | None = None,
    limit: int = MAX_SEARCH_REPLACEMENTS,
    keep: Callable[[Any], bool] | None = None,
) -> tuple[list[Any], MatchMode]:
    """Run the MatchMode ladder for *search_string* and return (hits, winning_mode).

    Tries each mode in match_modes(search_string, case_sensitive) until one yields
    at least one hit (after optional *keep* filtering), capped at *limit*.
    On a total miss, returns ([], modes[-1]) — the last (most lenient) pass tried.
    """
    modes = match_modes(search_string, case_sensitive)
    if not search_string:
        return [], modes[-1]
    for mode in modes:
        hits_iter = iter_literal_ranges(doc, search_string, mode)
        if keep is not None:
            hits_iter = filter(keep, hits_iter)
        hits = list(islice(hits_iter, limit))
        if hits:
            return hits, mode
    return [], modes[-1]


def find_first_range(
    doc: Any,
    search_string: str,
    *,
    case_sensitive: bool | None = None,
) -> Any:
    """First match in document order; native search with newline chaining."""
    hits = find_literal_ranges(doc, search_string, case_sensitive=case_sensitive, limit=1)[0]
    return hits[0] if hits else None


def find_all_ranges(
    doc: Any,
    search_string: str,
    *,
    case_sensitive: bool | None = None,
) -> list[Any]:
    """All occurrences, capped at MAX_SEARCH_REPLACEMENTS."""
    return find_literal_ranges(doc, search_string, case_sensitive=case_sensitive)[0]


def find_occurrence_range(
    doc: Any,
    text: str,
    *,
    occurrence: int = 0,
    case_sensitive: bool | None = None,
) -> tuple[Any | None, MatchMode]:
    """Find the occurrence-th match using the literal MatchMode ladder.

    Raises IndexError if matches exist in the winning pass but occurrence is out of range.
    Returns (None, last_mode) if no matches are found.
    """
    search_string = normalize_search_string_for_find(text)
    if not search_string:
        return None, match_modes("", case_sensitive)[-1]

    hits, mode = find_literal_ranges(
        doc,
        search_string,
        case_sensitive=case_sensitive,
        limit=occurrence + 1,
    )
    if not hits:
        return None, mode
    if occurrence >= len(hits):
        raise OccurrenceOutOfRange(
            f"occurrence {occurrence} out of range (found {len(hits)} match(es), use 0..{len(hits) - 1}).",
            mode,
        )
    return hits[occurrence], mode


def occurrence_range_or_error(
    doc: Any,
    text: str,
    raw_occurrence: Any,
    *,
    case_sensitive: bool | None = None,
    subject: str,
) -> tuple[Any | None, dict[str, Any] | None, MatchMode]:
    """Resolve the occurrence-th body match of *text* for a tool.

    Returns (range, None, mode) on a hit, else (None, payload, mode): INVALID_PARAM for
    a bad occurrence, OCCURRENCE_OUT_OF_RANGE when there are fewer matches, or the
    search-miss payload from build_search_not_found_response (subject names the
    tool parameter in the sentence). Callers add their own top-level fields.
    """
    last_mode = match_modes(text, case_sensitive)[-1]
    occurrence, occ_err = parse_non_negative_int(raw_occurrence)
    if occ_err:
        return None, make_tool_error(occ_err, code="INVALID_PARAM"), last_mode
    occurrence = 0 if occurrence is None else occurrence
    try:
        found, mode = find_occurrence_range(doc, text, occurrence=occurrence, case_sensitive=case_sensitive)
    except OccurrenceOutOfRange as exc:
        return None, make_tool_error(str(exc), code="OCCURRENCE_OUT_OF_RANGE"), exc.mode
    except IndexError as exc:
        return None, make_tool_error(str(exc), code="OCCURRENCE_OUT_OF_RANGE"), last_mode
    if found is None:
        return None, build_search_not_found_response(doc, text, case_sensitive=case_sensitive, subject=subject), mode
    return found, None, mode


def _is_same_text(candidate: Any, boundary_end: Any) -> bool:
    """True when candidate and boundary_end share the same XText container."""
    cand_text = uno_read(lambda: candidate.getText(), None)
    bound_text = uno_read(lambda: boundary_end.getText(), None)
    if cand_text is None or bound_text is None:
        return False
    return bool(uno_read(lambda: uno_same(cand_text, bound_text), False))


def pick_end_after_start(
    start: Any, ends: Iterator[Any] | Iterable[Any]
) -> tuple[Any | None, AnchorMissKind | None]:
    """Select the first end match at or after start.getEnd() and combine them into a single cursor.

    Walks candidate end matches. Candidates whose start falls before start.getEnd()
    (e.g. from backward chaining) are skipped. Candidates in a different XText are skipped,
    remembering that one was seen so if no same-text candidate is found, 'different_text' is
    returned rather than 'end'. Returns (cursor, None) on success.
    """
    boundary_end = start.getEnd()
    saw_different_text = False
    for end in ends:
        if not _is_same_text(end, boundary_end):
            saw_different_text = True
            continue
        cand_text = uno_read(lambda: end.getText(), None)
        if not starts_at_or_after(cand_text, end.getStart(), boundary_end):
            continue
        text = start.getText()
        cursor = text.createTextCursorByRange(start.getStart())
        cursor.gotoRange(end.getEnd(), True)
        return cursor, None
    return None, "different_text" if saw_different_text else "end"


def find_anchor_span(
    doc: Any,
    start_string: str,
    end_string: str,
    *,
    occurrence: int = 0,
    case_sensitive: bool | None = None,
) -> tuple[Any | None, AnchorMissKind | None, MatchMode]:
    """Find a range spanning from start_anchor through end_anchor.

    The end search starts at the start match's end so a repeated heading reaches the next
    occurrence, and the cursor includes both anchors. If occurrence is out of range,
    IndexError propagates from find_occurrence_range. Returns (cursor, None, mode) or
    (None, 'start' | 'end' | 'different_text', mode).
    """
    start, start_mode = find_occurrence_range(
        doc,
        start_string,
        occurrence=occurrence,
        case_sensitive=case_sensitive,
    )
    if start is None:
        return None, "start", start_mode

    end_modes = match_modes(end_string, case_sensitive)
    saw_different_text = False
    for mode in end_modes:
        ends = iter_literal_ranges(
            doc,
            end_string,
            mode,
            after=start.getEnd(),
            all_candidates=True,
        )
        cursor, kind = pick_end_after_start(start, ends)
        if cursor is not None:
            combined = MatchMode(
                start_mode.case_sensitive and mode.case_sensitive,
                start_mode.folded or mode.folded,
            )
            return cursor, None, combined
        if kind == "different_text":
            saw_different_text = True

    last_end = end_modes[-1]
    combined_miss = MatchMode(
        start_mode.case_sensitive and last_end.case_sensitive,
        start_mode.folded or last_end.folded,
    )
    return None, ("different_text" if saw_different_text else "end"), combined_miss


def _safe_name(obj: Any) -> str:
    name = uno_read(lambda: obj.getName(), FAILED)
    if name is not FAILED:
        return name
    return uno_read(lambda: getattr(obj, "Name", "") or "", "")


def _prop(obj: Any, name: str) -> Any:
    value = uno_read(lambda: obj.getPropertyValue(name), FAILED)
    return getattr(obj, name, None) if value is FAILED else value


def _cell_name(cur: Any, text: Any) -> str:
    # A non-string CellName must not become location text.
    for obj in (text, cur, _prop(cur, "Cell")):
        v = _prop(obj, "CellName")
        if isinstance(v, str) and v:
            return v
    return _safe_name(_prop(cur, "Cell"))


# A region that is off reads as None; Left, Right, and First return the shared
# text when their *IsShared flag is on (verified via SwXHeadFootText).
_HEADER_FOOTER_TEXT_PROPS: tuple[tuple[str, str], ...] = (
    ("HeaderText", "header"), ("HeaderTextLeft", "header"), ("HeaderTextRight", "header"),
    ("HeaderTextFirst", "first-page header"),
    ("FooterText", "footer"), ("FooterTextLeft", "footer"), ("FooterTextRight", "footer"),
    ("FooterTextFirst", "first-page footer"),
)


def _iter_header_footer_texts(doc: Any) -> Iterator[tuple[Any, str, Any]]:
    """Yield (page_style, region_label, xtext) for enabled header/footer regions on in-use page styles."""
    try:
        styles = doc.getStyleFamilies().getByName("PageStyles")
        for i in range(int(styles.getCount())):
            st = styles.getByIndex(i)
            try:
                if not st.isInUse():
                    continue
            except Exception as exc:
                reraise_if_disposed(exc)
                log.debug("_iter_header_footer_texts: isInUse failed", exc_info=True)
            for attr, region in _HEADER_FOOTER_TEXT_PROPS:
                try:
                    xtext = getattr(st, attr, None)
                except Exception as exc:
                    reraise_if_disposed(exc)
                    log.debug("_iter_header_footer_texts: %s unreadable: %s", attr, exc)
                    continue
                if xtext is not None:
                    yield st, region, xtext
    except Exception as exc:
        reraise_if_disposed(exc)
        log.debug("_iter_header_footer_texts: page-style walk failed", exc_info=True)


def _find_page_style_label(doc: Any, text_obj: Any) -> str | None:
    for st, region, region_text in _iter_header_footer_texts(doc):
        if uno_same(region_text, text_obj):
            name = _safe_name(st)
            return "%s (page style '%s')" % (region, name) if name else region
    return None


def _header_footer_label(text_obj: Any, doc: Any, label_cache: dict[int, str] | None = None) -> str | None:
    """ImplementationName == "SwXHeadFootText" is the discriminator because header/footer text
    is its own XText and there is no header/footer service to query. Body, cells, and frames use
    other implementation names.
    """
    if uno_read(lambda: getattr(text_obj, "ImplementationName", "") != "SwXHeadFootText", True):
        return None
    if label_cache is not None:
        key = id(text_obj)
        if key in label_cache:
            return label_cache[key]
    result = _find_page_style_label(doc, text_obj)
    if result is None:
        result = "header or footer"
    if label_cache is not None:
        label_cache[id(text_obj)] = result
    return result


def _text_and_cursor(found: Any) -> tuple[Any, Any]:
    """*found*'s XText and a cursor at its start."""
    text = found.getText()
    return text, text.createTextCursorByRange(found.getStart())


def describe_match_location(found: Any, doc: Any, label_cache: dict[int, str] | None = None) -> str:
    got = uno_read(lambda: _text_and_cursor(found), None)
    if got is None:
        return "body"
    text, cur = got
    tt = uno_read(lambda: cur.getPropertyValue("TextTable"), None)
    if tt is not None:
        tname = _safe_name(tt)
        cname = _cell_name(cur, text)
        if tname and cname:
            return "table '%s' cell %s" % (tname, cname)
        return "table '%s'" % tname if tname else "a table"
    tf = uno_read(lambda: cur.getPropertyValue("TextFrame"), None)
    if tf is not None:
        name = _safe_name(tf)
        return "text box '%s'" % name if name else "a text box"
    hf = _header_footer_label(text, doc, label_cache=label_cache)
    if hf:
        return hf
    return "body"


class LocationLabeler:
    """Labels match locations (body, table cell, text box, header/footer) with cached page-style lookups."""

    doc: Any
    _cache: dict[int, str]

    def __init__(self, doc: Any) -> None:
        self.doc = doc
        self._cache = {}

    def describe(self, found: Any) -> str:
        return describe_match_location(found, self.doc, label_cache=self._cache)


def enclosing_paragraph_text(doc: Any, found: Any) -> str:
    def paragraph() -> str:
        _text, cur = _text_and_cursor(found)
        cur.gotoStartOfParagraph(False)
        cur.gotoEndOfParagraph(True)
        return visible_range_text(doc, cur)

    para = uno_read(paragraph, FAILED)
    if para is not FAILED:
        return para
    return uno_read(lambda: found.getString(), "")


def iter_draw_shapes(container: Any) -> Iterator[Any]:
    n = uno_read(lambda: int(container.getCount()), FAILED)
    if n is FAILED:
        return
    for i in range(n):
        try:
            shape = container.getByIndex(i)
        except Exception as exc:
            reraise_if_disposed(exc)
            log.debug("iter_draw_shapes: getByIndex(%s) failed", i, exc_info=True)
            continue
        is_group = uno_read(lambda: shape.supportsService("com.sun.star.drawing.GroupShape"), False)
        if is_group:
            yield from iter_draw_shapes(shape)
        else:
            yield shape


def shape_is_as_character(shape: Any) -> bool:
    def anchored() -> bool:
        from com.sun.star.text.TextContentAnchorType import AS_CHARACTER
        return shape.getPropertyValue("AnchorType") == AS_CHARACTER

    return uno_read(anchored, False)


def shape_is_text_box(shape: Any) -> bool:
    return uno_read(lambda: bool(shape.getPropertyValue("TextBox")), False)


def text_box_frame(shape: Any) -> Any | None:
    """The hidden TextFrame holding a Word-style text box's text, or None for any other shape.

    A Word text box is a draw shape whose text lives in a hidden TextFrame (the shape's
    TextBoxContent). getTextFrames() hides those frames from getElementNames() but still
    serves them by name (sw/source/core/unocore/unocoll.cxx:SwXFrames::getByName vs
    SwXFrameEnumeration). Fly entries on the draw page have no TextBox property.
    """
    if shape_is_text_box(shape):
        return shape.getPropertyValue("TextBoxContent")
    return None


def text_box_shapes(doc: Any) -> list[tuple[int, Any, str]]:
    """(draw-page index, shape, frame name) for every Word-style text box."""
    page = doc.getDrawPage()
    boxes = []
    for i in range(page.getCount()):
        shape = page.getByIndex(i)
        frame = text_box_frame(shape)
        if frame is not None:
            boxes.append((i, shape, frame.getName()))
    return boxes


def iter_text_frame_names(doc: Any) -> Iterator[str]:
    """Names of every text frame including Word text boxes, all served by getTextFrames().getByName().

    Boxes inside group shapes are not included.
    """
    yield from doc.getTextFrames().getElementNames()
    for _idx, _shape, name in text_box_shapes(doc):
        yield name


def _lower_with_origins(text: str) -> tuple[str, list[int]]:
    lowered_chars: list[str] = []
    origins: list[int] = []
    for orig_idx, ch in enumerate(text):
        lowered = ch.lower()
        lowered_chars.append(lowered)
        origins.extend([orig_idx] * len(lowered))
    return "".join(lowered_chars), origins


def _regex_find_hits(pattern: str, text: str, flags: int) -> list[tuple[int, int]]:
    """Python re on shape/comment text, off the LibreOffice main thread.

    Catastrophic backtracking cannot be killed. A dedicated daemon is required:
    the shared pool joins workers with no timeout at shutdown, so a stuck re
    would hang process exit. One in-flight pattern at a time; a second call
    while the first is still running returns no hits instead of starting
    another thread.
    """
    global _shape_regex_inflight
    inflight = _shape_regex_inflight
    if inflight is not None and inflight.is_alive():
        log.warning("shape_text_hits: regex still running; skipping")
        return []
    box: dict[str, Any] = {}

    def _run() -> None:
        try:
            box["hits"] = [(m.start(), m.end()) for m in re_mod.finditer(pattern, text, flags) if m.group(0)]
        except Exception as exc:
            box["err"] = exc

    handle = run_in_background(_run, name="shape-text-regex", dedicated=True, daemon=True)
    _shape_regex_inflight = handle
    handle.join(_SHAPE_REGEX_TIMEOUT_SEC)
    if handle.is_alive():
        log.warning("shape_text_hits: regex exceeded %ss", _SHAPE_REGEX_TIMEOUT_SEC)
        return []
    err = box.get("err")
    if err is not None:
        log.debug("shape_text_hits: regex match failed", exc_info=err)
        return []
    hits = box.get("hits")
    return hits if isinstance(hits, list) else []


def iter_shape_text_spans(
    shape_text: str,
    pattern: str,
    case_sensitive: bool = SEARCH_CASE_SENSITIVE_DEFAULT,
    *,
    use_regex: bool = False,
) -> Iterator[tuple[int, int]]:
    """Yield [start, end) character offset spans of pattern in shape_text.

    Literal search folds typographic quotes via _SHAPE_QUOTE_FOLD.
    Offsets index the original shape_text string.
    """
    if not shape_text or not pattern:
        return
    if use_regex:
        flags = 0 if case_sensitive else re_mod.IGNORECASE
        yield from _regex_find_hits(pattern, shape_text, flags)
        return
    if case_sensitive:
        hay = shape_text.translate(_SHAPE_QUOTE_FOLD)
        needle = pattern.translate(_SHAPE_QUOTE_FOLD)

        def span_for_idx(idx: int) -> tuple[int, int]:
            return idx, idx + len(pattern)
    else:
        hay, origins = _lower_with_origins(shape_text)
        hay = hay.translate(_SHAPE_QUOTE_FOLD)
        needle = pattern.lower().translate(_SHAPE_QUOTE_FOLD)

        def span_for_idx(idx: int) -> tuple[int, int]:
            return origins[idx], origins[idx + len(needle) - 1] + 1

    step = len(needle) or 1
    idx = hay.find(needle)
    while idx != -1:
        yield span_for_idx(idx)
        idx = hay.find(needle, idx + step)


def shape_text_span(
    shape_text: str,
    pattern: str,
    case_sensitive: bool = SEARCH_CASE_SENSITIVE_DEFAULT,
    *,
    use_regex: bool = False,
) -> tuple[int, int] | None:
    """Return the [start, end) character offset span of pattern in shape_text, or None.

    Literal search folds typographic quotes via _SHAPE_QUOTE_FOLD.
    Offsets index the original shape_text string.
    """
    return next(
        iter_shape_text_spans(
            shape_text, pattern, case_sensitive=case_sensitive, use_regex=use_regex
        ),
        None,
    )


def replace_text_in_shape(
    shape: Any,
    old: str,
    new: str,
    *,
    case_sensitive: bool = SEARCH_CASE_SENSITIVE_DEFAULT,
    use_regex: bool = False,
) -> bool:
    """Replace the first occurrence of *old* with *new* inside a drawing shape's own text,
    preserving the formatting of the surrounding text via a text cursor. Returns True on success.

    Uses character-offset navigation (goRight) which aligns with getString() for single-paragraph
    shape text (the common case for callouts / text boxes). Returns False on an ordinary UNO failure;
    a disposed document propagates."""
    try:
        xtext = shape.getText()
        s = xtext.getString()
    except Exception as exc:
        reraise_if_disposed(exc, object_type="Text")
        return False
    span = shape_text_span(s, old, case_sensitive=case_sensitive, use_regex=use_regex)
    if span is None:
        return False
    start, end = span
    # goRight counts UTF-16 code units, while str offsets count code points. Convert so astral
    # characters (emoji, rare CJK) in the shape text don't misalign the selection / corrupt text.
    left_units = len(s[:start].encode("utf-16-le")) // 2
    old_units = len(s[start:end].encode("utf-16-le")) // 2
    if old_units <= 0:
        return False
    try:
        cursor = xtext.createTextCursorByRange(xtext.getStart())
        if left_units:
            cursor.goRight(left_units, False)
        cursor.goRight(old_units, True)
        cursor.setString(new)
        return True
    except Exception as exc:
        reraise_if_disposed(exc, object_type="Text")
        return False


def shape_text_hits(shape_text: str, pattern: str, use_regex: bool, case_sensitive: bool) -> list[str]:
    return [
        shape_text[start:end]
        for start, end in iter_shape_text_spans(
            shape_text, pattern, case_sensitive=case_sensitive, use_regex=use_regex
        )
    ]


def iter_floating_shape_texts(doc: Any) -> Iterator[tuple[Any, str]]:
    try:
        draw_page = doc.getDrawPage()
    except Exception as exc:
        reraise_if_disposed(exc)
        log.debug("search: draw-page sweep failed", exc_info=True)
        return
    for shape in iter_draw_shapes(draw_page):
        try:
            if shape_is_as_character(shape) or shape_is_text_box(shape):
                continue
            text = shape.getString() if hasattr(shape, "getString") else ""
        except Exception as exc:
            reraise_if_disposed(exc)
            log.debug("iter_floating_shape_texts: shape text unreadable", exc_info=True)
            continue
        yield shape, text


def shape_display_name(shape: Any) -> str:
    return (getattr(shape, "Name", "") or "").strip() or "(unnamed shape)"


def iter_shape_hits(doc: Any, pattern: str, use_regex: bool, case_sensitive: bool) -> Iterator[tuple[str, str, str]]:
    for shape, shape_text in iter_floating_shape_texts(doc):
        hits = shape_text_hits(shape_text, pattern, use_regex, case_sensitive)
        if not hits:
            continue
        sname = uno_read(lambda: shape_display_name(shape), "(unnamed shape)")
        for hit in hits:
            yield hit, "shape '%s'" % sname, shape_text.strip()


def iter_comment_hits(doc: Any, pattern: str, use_regex: bool, case_sensitive: bool) -> Iterator[tuple[str, str, str]]:
    fields = uno_read(lambda: doc.getTextFields().createEnumeration(), FAILED)
    if fields is FAILED:
        return
    for field in iter_elements(fields):
        try:
            if not field.supportsService("com.sun.star.text.textfield.Annotation"):
                continue
            content = getattr(field, "Content", "") or ""
            author = (getattr(field, "Author", "") or "").strip()
        except Exception as exc:
            reraise_if_disposed(exc)
            log.debug("comment_matches: annotation field unreadable", exc_info=True)
            continue
        for hit in shape_text_hits(content, pattern, use_regex, case_sensitive):
            yield hit, "comment by '%s'" % (author or "unknown"), content.strip()


def _search_descriptor(doc: Any, pattern: str, use_regex: bool, case_sensitive: bool) -> Any:
    sd = doc.createSearchDescriptor()
    sd.SearchString = pattern
    sd.SearchRegularExpression = bool(use_regex)
    sd.SearchCaseSensitive = bool(case_sensitive)
    return sd


def find_ranges_regex_case(doc: Any, pattern: str, use_regex: bool, case_sensitive: bool, all_matches: bool) -> Any:
    # Callers validate the pattern with validate_icu_regex before searching.
    sd = _search_descriptor(doc, pattern, use_regex, case_sensitive)
    if not all_matches:
        return next(_iter_descriptor_hits(doc, sd), None)
    return _collect_capped(doc, sd)


def old_content_covers_most(doc: Any, needle_len: int) -> bool:
    """Check whether a search needle spans more than half the document body.

    Probe the body using cursor character steps. If the needle length exceeds half
    the body character count, caller directs the user to target='full_document'.
    Short needles (under 100 characters) are never oversized: local edits on small
    documents or headings must not be forced to whole-document replacements.
    An empty body or non-positive needle length returns False.
    """
    if needle_len < MIN_OVERSIZED_CHARS:
        return False
    try:
        text = doc.getText()
        cursor = text.createTextCursor()
        cursor.gotoStart(False)
    except Exception as exc:
        reraise_if_disposed(exc)
        return False

    if not go_right(cursor, 1, False):
        return False
    cursor.gotoStart(False)
    if not go_right(cursor, needle_len, False):
        return True
    if not go_right(cursor, needle_len, False):
        return True
    return False


def closest_mismatch(
    needle: str,
    hay: str,
    *,
    lenient: bool,
) -> dict[str, Any] | None:
    """Find the best-matching window in *hay* for *needle* and report its first mismatch.

    Windows are scored by folded common-prefix length, with ties broken by earlier
    start. If the folded needle appears as a prefix of any window (an exact match),
    None is returned. Candidate starts are accelerated via str.find on the prefix.
    """
    if not needle or not hay:
        return None

    def _fold_with_map(s: str) -> tuple[str, list[int]]:
        if not lenient:
            return s, list(range(len(s)))
        translated = s.translate(_FOLD_NORMALIZE_MAP)
        chars: list[str] = []
        idx_map: list[int] = []
        in_space = False
        for i, c in enumerate(translated):
            c_folded = c.lower() if len(c.lower()) == 1 else c
            if c_folded == " ":
                if not in_space:
                    chars.append(" ")
                    idx_map.append(i)
                    in_space = True
            else:
                in_space = False
                chars.append(c_folded)
                idx_map.append(i)
        return "".join(chars), idx_map

    folded_needle, needle_map = _fold_with_map(needle)
    folded_hay, hay_map = _fold_with_map(hay)

    needle_len = len(folded_needle)
    hay_len = len(folded_hay)
    if needle_len == 0 or hay_len == 0:
        return None

    min_score = max(1, min(8, needle_len // 2))
    k = min_score
    candidate = folded_needle[:k]

    best_score = -1
    best_i = -1
    best_diff = -1
    comparisons = 0
    max_comparisons = 2_000_000

    start_pos = 0
    while True:
        if comparisons >= max_comparisons:
            break
        i = folded_hay.find(candidate, start_pos)
        if i == -1:
            break
        start_pos = i + 1

        window_len = min(needle_len, hay_len - i)
        mismatch_found = False
        diff_idx = -1
        score = k
        comparisons += k
        for j in range(k, window_len):
            comparisons += 1
            if folded_needle[j] == folded_hay[i + j]:
                score += 1
            else:
                mismatch_found = True
                diff_idx = j
                break
            if comparisons >= max_comparisons:
                break

        if not mismatch_found and score == needle_len:
            return None

        if mismatch_found and score > best_score:
            best_score = score
            best_i = i
            best_diff = diff_idx

    if best_score < min_score:
        return None

    diff_at = needle_map[best_diff]
    raw_needle_char = needle[diff_at]
    raw_hay_pos = hay_map[best_i + best_diff]
    raw_hay_char = hay[raw_hay_pos]

    raw_start = hay_map[best_i]
    raw_end = hay_map[min(best_i + needle_len - 1, hay_len - 1)] + 1
    window = hay[raw_start:raw_end]
    preview = window[:16] + ("\u2026" if len(window) > 16 else "")

    msg = (
        f"closest match starts {preview!r}, differs at character {diff_at}: "
        f"{raw_needle_char} vs {raw_hay_char}"
    )
    return {
        "closest_match": preview,
        "diff_at": diff_at,
        "message": msg,
    }


MAX_MISS_HAYSTACK_CHARS = 2_000_000


def get_search_miss_haystack(doc: Any) -> str:
    """Read document body, text frames (Word text boxes included), and header/footer regions (without tracked deletions) for near-miss search."""
    try:
        body_text = visible_range_text(doc, doc.getText())
    except Exception as exc:
        reraise_if_disposed(exc)
        log.warning("Failed to read document body for search miss: %s", exc)
        return ""

    parts = [body_text]
    try:
        frames = doc.getTextFrames()
        for name in iter_text_frame_names(doc):
            try:
                frame = frames.getByName(name)
                frame_text = visible_range_text(doc, frame.getText())
                if frame_text:
                    parts.append(frame_text)
            except Exception as exc:
                reraise_if_disposed(exc)
                log.warning("Failed to read text frame %r for search miss: %s", name, exc)
    except Exception as exc:
        reraise_if_disposed(exc)
        log.warning("Failed to enumerate text frames for search miss: %s", exc)

    seen_regions: set[str] = set()
    for _style, _region, xtext in _iter_header_footer_texts(doc):
        try:
            region_text = visible_range_text(doc, xtext)
        except Exception as exc:
            reraise_if_disposed(exc)
            log.warning("Failed to read header/footer text for search miss: %s", exc)
            continue
        if region_text and region_text not in seen_regions:
            seen_regions.add(region_text)
            parts.append(region_text)

    hay = "\n".join(parts)
    if len(hay) > MAX_MISS_HAYSTACK_CHARS:
        return hay[:MAX_MISS_HAYSTACK_CHARS]
    return hay


def search_closest_mismatch(
    doc: Any,
    needle: str,
    *,
    lenient: bool = True,
) -> dict[str, Any] | None:
    """Find the closest matching passage in the document for a failed search needle."""
    if not needle:
        return None
    hay = get_search_miss_haystack(doc)
    if not hay:
        return None
    return closest_mismatch(needle, hay, lenient=lenient)


def drawing_shape_object_containing(
    doc: Any,
    search_string: str,
    *,
    use_regex: bool = False,
    case_sensitive: bool = SEARCH_CASE_SENSITIVE_DEFAULT,
) -> Any | None:
    pattern = (search_string or "").strip()
    if not pattern:
        return None
    for shape, text in iter_floating_shape_texts(doc):
        if shape_text_hits(text, pattern, use_regex, case_sensitive):
            return shape
    return None


def drawing_shape_containing(
    doc: Any,
    search_string: str,
    *,
    use_regex: bool = False,
    case_sensitive: bool = SEARCH_CASE_SENSITIVE_DEFAULT,
) -> str | None:
    shape = drawing_shape_object_containing(
        doc, search_string, use_regex=use_regex, case_sensitive=case_sensitive)
    if shape is None:
        return None
    return shape_display_name(shape)


def _preview_matches(hits: Iterator[tuple[str, str, str]], limit: int) -> list[dict[str, Any]]:
    matches: list[dict[str, Any]] = []
    for hit, location, context in hits:
        matches.append({"text": hit, "location": location, "context": context})
        if len(matches) >= limit:
            break
    return matches


def sweep_draw_shape_preview_matches(doc: Any, pattern: str, use_regex: bool, case_sensitive: bool, limit: int = DEFAULT_SEARCH_RESULTS) -> list[dict[str, Any]]:
    """Edit-reachable draw-layer hits for dry_run (same skip rules as search sweep)."""
    return _preview_matches(iter_shape_hits(doc, pattern, use_regex, case_sensitive), limit)


def sweep_comment_preview_matches(doc: Any, pattern: str, use_regex: bool, case_sensitive: bool, limit: int = DEFAULT_SEARCH_RESULTS) -> list[dict[str, Any]]:
    return _preview_matches(iter_comment_hits(doc, pattern, use_regex, case_sensitive), limit)


class SearchInDocument(ToolBase):
    """Search for text anywhere in a document (body, tables, text boxes) and report where."""

    name: str | None = "search"
    description: str = (
        "Search for text ANYWHERE in the document using LibreOffice native search — body paragraphs "
        "and headings, table cells, text boxes / frames, floating drawing shapes, page headers/footers, "
        "AND comments (annotations) are all covered. Each match reports WHERE it was found (location, "
        "e.g. \"body\", \"table 'Table1' cell B2\", \"text box 'Frame1'\", \"shape 'Shape 1'\", "
        "\"header (page style 'Standard')\", \"comment by 'Ana'\") plus the surrounding paragraph (or "
        "shape/comment text) for context. "
        "With regex=true, body/table/frame hits use LibreOffice/ICU regex; patterns use ICU syntax "
        "(validated by LibreOffice before searching; e.g. (?<name>...) not (?P<name>...)); shape and "
        "comment sweeps use Python re, so ICU-only syntax such as \\p{Lu} finds nothing there."
    )
    parameters: dict[str, Any] | None = {
        "type": "object",
        "properties": {
            "pattern": {"type": "string", "description": "Search string or regex pattern."},
            "regex": {"type": "boolean", "default": False, "description": "Use regular expression (ICU syntax)."},
            "case_sensitive": case_sensitive_param(),
            "max_results": {
                "type": "integer",
                "default": DEFAULT_SEARCH_RESULTS,
                "description": "Maximum results to return. Must be a positive integer.",
            },
            "return_offsets": {"type": "boolean", "default": False, "description": (
                "If true, returns {start, end, text} character offsets for body text only "
                "(no regex, no shapes/comments).")},
        },
        "required": ["pattern"],
    }
    uno_services: list[str] | None = ["com.sun.star.text.TextDocument"]
    tier: str = "core"

    def execute(self, ctx: ToolContext, **kwargs: Any) -> dict[str, Any]:
        raw_max = kwargs.get("max_results", DEFAULT_SEARCH_RESULTS)
        if raw_max < 1:
            return self._tool_error("max_results must be a positive integer.", code="INVALID_PARAM")
        max_results = raw_max

        pattern = kwargs["pattern"]
        if not pattern:
            return self._tool_error("pattern is required.", code="INVALID_PARAM")

        use_regex = kwargs.get("regex", False)
        case_sensitive = resolve_case_sensitive(kwargs.get("case_sensitive"))
        return_offsets = kwargs.get("return_offsets", False)

        if return_offsets and use_regex:
            return self._tool_error(
                "return_offsets does not support regex=true; use regex=false or omit return_offsets.",
                code="INVALID_PARAM")
        if return_offsets:
            ranges = find_text_ranges(
                ctx.doc, pattern, limit=max_results, case_sensitive=case_sensitive, body_only=True)
            return {"status": "ok", "ranges": ranges, "matched_case_sensitive": case_sensitive}

        if use_regex:
            rex_err = validate_icu_regex(ctx.ctx, pattern)
            if rex_err:
                return self._tool_error(invalid_regex_tool_message(rex_err), code="INVALID_REGEX", count=0)

        doc = ctx.doc
        labeler = LocationLabeler(doc)
        try:
            sd = _search_descriptor(doc, pattern, use_regex, case_sensitive)
            found = doc.findFirst(sd)
        except Exception as exc:
            reraise_if_disposed(exc)
            log.exception("search: createSearchDescriptor / findFirst failed")
            return self._tool_error(
                "search failed: %s" % exc,
                code="SEARCH_FAILED",
                top_level={"matched_case_sensitive": case_sensitive},
            )

        matches: list[dict[str, str]] = []

        def add(text: str, location: str, context: str) -> None:
            matches.append({"text": text, "location": location, "context": context})

        deletions = TrackedDeletions(doc)
        total_count = 0
        warning = None
        while found is not None and total_count < MAX_SEARCH_HITS:
            try:
                hit_text = found.getString()
            except Exception as exc:
                reraise_if_disposed(exc)
                log.exception("search: getString failed")
                warning = "Text-model search ended early after a getString failure; count may be incomplete."
                break
            if hit_text == "":
                # Zero-width matches log a warning and stop the sweep so getString
                # failures are not treated as benign empty hits with a silent count.
                warning = "Search stopped on a zero-width match; count may be incomplete."
                break
            if not deletions.covers(found):
                total_count += 1
                if len(matches) < max_results:
                    add(
                        hit_text,
                        labeler.describe(found),
                        enclosing_paragraph_text(doc, found),
                    )
            try:
                # hit_text is non-empty here, so skip find_next_after_match's second getString.
                found = doc.findNext(found.getEnd(), sd)
            except Exception as exc:
                reraise_if_disposed(exc)
                log.exception("search: findNext failed")
                warning = "Text-model search ended early after a findNext failure; count may be incomplete."
                break

        shape_comment_warning = shape_sweep_skip_reason(pattern, use_regex)
        if shape_comment_warning is None:
            for extra in (
                iter_shape_hits(doc, pattern, use_regex, case_sensitive),
                iter_comment_hits(doc, pattern, use_regex, case_sensitive),
            ):
                for hit, location, context in extra:
                    if total_count >= MAX_SEARCH_HITS:
                        break
                    total_count += 1
                    if len(matches) < max_results:
                        add(hit, location, context)
                if total_count >= MAX_SEARCH_HITS:
                    break

        if warning is None and total_count >= MAX_SEARCH_HITS:
            warning = "Search stopped after %d matches; count may be incomplete." % MAX_SEARCH_HITS

        result = {
            "status": "ok",
            "matches": matches,
            "count": total_count,
            "returned": len(matches),
            "matched_case_sensitive": case_sensitive,
        }
        if total_count == 0 and document_has_tracked_deletions(doc):
            result["note"] = _TRACKED_DELETION_HINT.strip()

        if warning is not None:
            result["truncated"] = True
            result["warning"] = warning
        elif shape_comment_warning is not None:
            result["warning"] = shape_comment_warning

        returned = len(matches)
        if returned < total_count and warning is None:
            result["hint"] = search_results_cap_hint(returned, total_count)

        return result


class AdvancedSearch(ToolBaseDummy):
    name: str | None = "advanced_search"
    description: str = "Full-text search with Snowball stemming. Supports boolean queries: AND (default), OR, NOT, NEAR/N. Language auto-detected from document locale. Returns matching paragraphs with context and nearest heading bookmark. Use around_page to restrict results near a specific page."
    parameters: dict[str, Any] | None = {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": ("Search query. Examples: 'climate change', 'energy AND renewable', 'solar OR wind', 'climate NOT politics', 'ocean NEAR/3 warming'")},
            "max_results": {"type": "integer", "description": "Maximum results to return (default: 20)"},
            "context_paragraphs": {"type": "integer", "description": "Paragraphs of context around each match (default: 1)"},
            "around_page": {"type": "integer", "description": ("Restrict results to paragraphs near this 0-based page index (first page = 0; optional). Enables page numbers in results.")},
            "page_radius": {"type": "integer", "description": ("Page radius for around_page filter (default: 1, meaning +/-1 page)")},
            "include_pages": {"type": "boolean", "description": ("Add 0-based page numbers (first page = 0) to results. Automatic when around_page is set. (default: false)")},
        },
        "required": ["query"],
    }
    uno_services: list[str] | None = ["com.sun.star.text.TextDocument"]

    def execute(self, ctx: ToolContext, **kwargs: Any) -> dict[str, Any]:
        query = kwargs.get("query")
        if not isinstance(query, str) or not query.strip():
            return self._tool_error("query is required.", code="INVALID_PARAM")

        idx_svc = ctx.services.writer_index
        around_page = kwargs.get("around_page")
        page_radius = kwargs.get("page_radius", 1)
        include_pages = kwargs.get("include_pages", False)

        if around_page is not None:
            include_pages = True

        try:
            result = idx_svc.search_boolean(ctx.doc, query, max_results=kwargs.get("max_results", 20), context_paragraphs=kwargs.get("context_paragraphs", 1))
        except ValueError as e:
            return self._tool_error(str(e), code="INVALID_PARAM")

        if include_pages and result.get("matches"):
            page_map = _build_page_map(ctx.doc)
            for m in result["matches"]:
                pi = m.get("paragraph_index")
                if pi is not None and pi in page_map:
                    m["page"] = page_map[pi]

            if around_page is not None:
                lo = around_page - page_radius
                hi = around_page + page_radius
                before_count = len(result["matches"])
                # Matches without a mapped page cannot be placed near any page.
                result["matches"] = [m for m in result["matches"] if m.get("page") is not None and lo <= m["page"] <= hi]
                result["returned"] = len(result["matches"])
                result["filtered_by_page"] = {"around_page": around_page, "page_radius": page_radius, "before_filter": before_count}

        return {"status": "ok", **result}


def _build_page_map(doc: Any) -> dict[int, int]:
    """Build a mapping of paragraph_index to 0-based page index.

    No module cache is kept because documents mutate, id() can be reused, and a failed
    walk would be cached indefinitely as {}. Uses with_view_cursor_left_body_locked
    to ensure controllers are unlocked before restoring the view cursor.
    """
    try:
        controller = doc.getCurrentController()
        vc = controller.getViewCursor()
    except Exception as exc:
        reraise_if_disposed(exc)
        log.exception("advanced_search: could not read the view cursor")
        return {}

    def _walk() -> dict[int, int]:
        page_map: dict[int, int] = {}
        try:
            text = doc.getText()
            enum = text.createEnumeration()
            idx = 0
            for para in iter_elements(enum):
                try:
                    vc.gotoRange(para.getStart(), False)
                    lo_page = vc.getPage()
                    # 0 = unset/not laid out; LibreOffice pages are 1-based.
                    if lo_page > 0:
                        page_map[idx] = lo_page - 1
                except Exception as exc:
                    reraise_if_disposed(exc)
                    log.debug("advanced_search: page lookup failed for paragraph %s", idx, exc_info=True)
                idx += 1
        except Exception as exc:
            reraise_if_disposed(exc)
            log.exception("advanced_search: page map walk failed")
        return page_map

    return with_view_cursor_left_body_locked(doc, vc, _walk)


class GetIndexStats(ToolBaseDummy):
    name: str | None = "get_index_stats"
    description: str = "Get search index statistics: paragraph count, unique stems, language, build time, and top 20 most frequent stems."
    parameters: dict[str, Any] | None = {"type": "object", "properties": {}, "required": []}
    uno_services: list[str] | None = ["com.sun.star.text.TextDocument"]

    def execute(self, ctx: ToolContext, **kwargs: Any) -> dict[str, Any]:
        idx_svc = ctx.services.writer_index
        result = idx_svc.get_index_stats(ctx.doc)
        return {"status": "ok", **result}
