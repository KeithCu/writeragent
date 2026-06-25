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
"""Writer content tools — read, apply, find, and paragraph operations."""

import itertools
import logging
import os
import threading

from plugin.framework.tool import ToolBase, ToolBaseDummy
from plugin.framework.constants import APPLY_DOCUMENT_CONTENT_TOOL_RESEARCH_HINT
from plugin.doc.document_helpers import normalize_linebreaks, get_string_without_tracked_deletions
from plugin.writer.edit_review import EditReviewSession, edit_review_wait_seconds, review_recording_enabled, get_agent_edit_review_mode
from plugin.framework.errors import safe_json_loads, ToolExecutionError
import re as re_mod


log = logging.getLogger("writeragent.writer")

# Cap for replace-all search (_find_all_ranges).
_MAX_SEARCH_REPLACEMENTS = 200

# Non-breaking / exotic spaces -> ASCII space. Length-preserving (each maps to a
# single BMP char) so character offsets into the document text stay valid. NBSP
# (U+00A0) in particular is a common artifact of prior edits and breaks literal
# search when old_content uses a normal space.
#
# Regenerate the inventory table: python3 -c "..."  (see git history / plan doc) or
# run the snippet in the finish-NBSP plan; paste rows here when expanding the map.
#
# | Code   | Name                         | In _SPACE_NORMALIZE_MAP | Follow-up note |
# |--------|------------------------------|-------------------------|----------------|
# | U+0020 | SPACE                        | no                      | target; not mapped |
# | U+00A0 | NO-BREAK SPACE               | yes                     | mapped today |
# | U+1680 | OGHAM SPACE MARK             | no                      | OGHAM SPACE MARK; rare in Writer |
# | U+2000 | EN QUAD                      | yes                     | mapped today |
# | U+2001 | EM QUAD                      | yes                     | mapped today |
# | U+2002 | EN SPACE                     | yes                     | mapped today |
# | U+2003 | EM SPACE                     | yes                     | mapped today |
# | U+2004 | THREE-PER-EM SPACE           | yes                     | mapped today |
# | U+2005 | FOUR-PER-EM SPACE            | yes                     | mapped today |
# | U+2006 | SIX-PER-EM SPACE             | yes                     | mapped today |
# | U+2007 | FIGURE SPACE                 | yes                     | mapped today |
# | U+2008 | PUNCTUATION SPACE            | yes                     | mapped today |
# | U+2009 | THIN SPACE                   | yes                     | mapped today |
# | U+200A | HAIR SPACE                   | yes                     | mapped today |
# | U+202F | NARROW NO-BREAK SPACE        | yes                     | mapped today |
# | U+205F | MEDIUM MATHEMATICAL SPACE    | yes                     | mapped today |
# | U+3000 | IDEOGRAPHIC SPACE            | yes                     | mapped today |
#
# DEVELOPER DISCUSSION / FUTURE WORK (Intentionally deferred to avoid complexity):
#
# - Full-document replace via target='search' + old_content=entire body:
#   DO NOT support this with a body offset fallback. Callers (LLM, tests, translation) must use
#   target='full_document' — no old_content, no search. If we ever revisit, git history has
#   _find_range_by_offset / phase-3 offset scan removed after the LO-regex unification commit.
#
# - Format.py search-replace helpers:
#   Functions like format.find_text_ranges are still LO-literal only. apply_document_content search
#   uses _find_chained_range (LO regex + paragraph chaining) with exotic-space flex matching.
#   Unifying format.find_text_ranges with that stack is deferred — callers are internal/benchmark-only.
#
# - Casefolding & Unicode length changes:
#   Chaining compares via .lower(); LO regex uses SearchCaseSensitive=False. German ß (folds to ss)
#   or Turkish I can still mis-match or mis-size cursor ranges. Fixing that needs character mapping
#   tracking beyond goRight — deferred for rare edge cases.
#
# - Headers/footers:
#   doc.findFirst searches the full document model; we rely on LO for header/footer hits rather than
#   enumerating nested XText containers ourselves.
#
# - Markup apply in nested XText:
#   When inserting HTML/markup inside a table cell, the HTML import helper (replace_single_range_with_content)
#   can sometimes jump the cursor to the end of the document body rather than the cell's end. This is a potential
#   real-world bug if the AI attempts to write rich formatting/math inside cells, but we defer it until we
#   receive actual user bug reports due to the complexity of relative cursor mapping in nested XText.
_SPACE_CODEPOINTS = (
    0x00A0,  # NO-BREAK SPACE
    0x202F,  # NARROW NO-BREAK SPACE
    0x2007,  # FIGURE SPACE
    0x2009,  # THIN SPACE
    # Typographic spaces
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
    # CJK space
    0x3000,  # IDEOGRAPHIC SPACE
)
_SPACE_NORMALIZE_MAP = {cp: " " for cp in _SPACE_CODEPOINTS}
# Shared horizontal-space class — must stay aligned with _SPACE_CODEPOINTS.
_HORIZONTAL_SPACE_CLASS = r"[ \t" + "".join("\\u%04x" % cp for cp in _SPACE_CODEPOINTS) + "]"
_HORIZONTAL_SPACE_RE = _HORIZONTAL_SPACE_CLASS + "+"


def _search_try_strings(search_string):
    """Literal search string, then newline-collapsed variant (HTML wrap artifact)."""
    s = search_string or ""
    collapsed = re_mod.sub(r" +", " ", s.replace("\n", " ")).strip()
    for candidate in (s, collapsed):
        if candidate:
            yield candidate


def _escape_for_lo_regex(s):
    """Escape regular expression characters and match any horizontal space sequence."""
    s = (s or "").translate(_SPACE_NORMALIZE_MAP)
    escaped = re_mod.sub(r'([\\^$.|?*+()\[\]{}])', r'\\\1', s)
    return re_mod.sub(r' +', lambda m: _HORIZONTAL_SPACE_CLASS + '+', escaped)


def _compare_normalize(s):
    return normalize_linebreaks(s).translate(_SPACE_NORMALIZE_MAP).strip().lower()


def _paragraph_matches_part(para_text, part, *, head=False, tail=False):
    """Compare a paragraph to a search part; return (ok, offset_len).

    *head*: part must match the start of the paragraph; offset_len is goRight length.
    *tail*: part must match the end; offset_len is goRight start offset from para start.
    Neither flag: full paragraph must equal part (after normalize).
    """
    expected_norm = _compare_normalize(part)
    actual_norm = _compare_normalize(para_text)
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


def _find_lo_regex_ranges(doc, candidate, all_matches=False):
    """LO regex findFirst/findNext for one candidate string."""
    sd = doc.createSearchDescriptor()
    sd.SearchRegularExpression = True
    sd.SearchString = _escape_for_lo_regex(candidate)

    if not all_matches:
        for case_sens in (True, False):
            sd.SearchCaseSensitive = case_sens
            found = doc.findFirst(sd)
            if found is not None:
                return found
        return None

    ranges = []
    for case_sens in (True, False):
        sd.SearchCaseSensitive = case_sens
        found = doc.findFirst(sd)
        while found is not None:
            if len(ranges) >= _MAX_SEARCH_REPLACEMENTS:
                return ranges
            ranges.append(found)
            found = doc.findNext(found, sd)
        if ranges:
            return ranges
    return ranges


def _find_chained_range(doc, search_string, all_matches=False):
    """Find search_string via LO regex (literal + newline-collapsed retry) then paragraph chaining.

    doc.findFirst covers body, table cells, and text frames. Chaining handles real paragraph
    breaks that LO regex cannot cross.

    Not for whole-document replace: use apply_document_content(target='full_document') instead
    of passing the entire body as old_content (see ApplyDocumentContent docstring).
    """
    if not search_string:
        return [] if all_matches else None

    # Phase 1: whole-string LO regex — literal first, then newline-collapsed (HTML wrap artifact).
    for candidate in _search_try_strings(search_string):
        result = _find_lo_regex_ranges(doc, candidate, all_matches=all_matches)
        if all_matches:
            if result:
                return result
        elif result is not None:
            return result

    # Phase 2: paragraph chaining on the original string (real cross-paragraph intent).
    parts = search_string.split('\n')
    if len(parts) <= 1:
        return [] if all_matches else None

    anchor_idx = -1
    for idx, part in enumerate(parts):
        if part.strip():
            anchor_idx = idx
            break
    if anchor_idx == -1:
        return [] if all_matches else None

    sd = doc.createSearchDescriptor()
    sd.SearchRegularExpression = True
    sd.SearchString = _escape_for_lo_regex(parts[anchor_idx])

    matched_ranges = []

    for case_sens in (True, False):
        sd.SearchCaseSensitive = case_sens
        found = doc.findFirst(sd)
        while found is not None:
            text = found.getText()
            chain_ok = True

            forward_cursor = text.createTextCursorByRange(found)
            forward_cursor.gotoRange(found.getEnd(), False)
            last_end_cursor = None

            for i in range(anchor_idx + 1, len(parts)):
                if not forward_cursor.gotoNextParagraph(False):
                    chain_ok = False
                    break

                check_cursor = text.createTextCursorByRange(forward_cursor)
                check_cursor.gotoEndOfParagraph(True)
                para_text = get_string_without_tracked_deletions(check_cursor)

                is_last = i == len(parts) - 1
                ok, offset_len = _paragraph_matches_part(para_text, parts[i], head=is_last)
                if not ok:
                    chain_ok = False
                    break
                if is_last:
                    last_end_cursor = text.createTextCursorByRange(forward_cursor)
                    last_end_cursor.goRight(offset_len, False)

            if not chain_ok:
                found = doc.findNext(found, sd)
                continue

            backward_cursor = text.createTextCursorByRange(found)
            backward_cursor.gotoRange(found.getStart(), False)
            first_start_cursor = None

            for i in range(anchor_idx - 1, -1, -1):
                if not backward_cursor.gotoPreviousParagraph(False):
                    chain_ok = False
                    break

                check_cursor = text.createTextCursorByRange(backward_cursor)
                check_cursor.gotoEndOfParagraph(True)
                para_text = get_string_without_tracked_deletions(check_cursor)

                is_first = i == 0
                ok, offset_len = _paragraph_matches_part(para_text, parts[i], tail=is_first)
                if not ok:
                    chain_ok = False
                    break
                if is_first:
                    first_start_cursor = text.createTextCursorByRange(backward_cursor)
                    first_start_cursor.goRight(offset_len, False)

            if chain_ok:
                start_range = first_start_cursor.getStart() if first_start_cursor else found.getStart()
                end_range = last_end_cursor.getStart() if last_end_cursor else found.getEnd()

                try:
                    result_range = text.createTextCursorByRange(start_range)
                    result_range.gotoRange(end_range, True)
                    if not all_matches:
                        return result_range
                    matched_ranges.append(result_range)
                    if len(matched_ranges) >= _MAX_SEARCH_REPLACEMENTS:
                        return matched_ranges
                except Exception:
                    log.debug("Failed creating combined XTextRange", exc_info=True)

            found = doc.findNext(found, sd)

        if matched_ranges:
            return matched_ranges

    return matched_ranges if all_matches else None


def _find_first_range(doc, search_string):
    """First match: LO native search with chaining fallback."""
    return _find_chained_range(doc, search_string, all_matches=False)


def _normalize_search_string_for_find(s):
    """Collapse horizontal whitespace (incl. NBSP & friends) to a single ASCII
    space; preserve newlines for literal find.
    """
    return re_mod.sub(_HORIZONTAL_SPACE_RE, " ", s).strip()


def _all_start_indices(haystack, needle):
    """Non-overlapping start indices of *needle* in *haystack*."""
    out = []
    if not needle:
        return out
    i = haystack.find(needle)
    while i >= 0:
        out.append(i)
        i = haystack.find(needle, i + len(needle))
    return out


def _find_all_ranges(doc, search_string):
    """All occurrences as TextRanges in document order (NBSP-aware native search with chaining)."""
    return _find_chained_range(doc, search_string, all_matches=True)


# Agent-edit tuning knobs. These have sensible fixed defaults and are NOT settings-UI options (no
# demonstrated need to tune them per document), so they are plain named constants -- not config keys.
# A power user can still override any of them ONCE at startup via an environment variable: a named
# constant with an escape hatch, read at import time.

def _env_num(name, default, cast, ok):
    """Read tuning knob *name* from the environment, *cast* it, and return it only if *ok(v)*;
    otherwise the fixed *default*. Never raises -- a bad env value must never break an edit."""
    try:
        v = cast(os.environ[name])
        return v if ok(v) else default
    except (KeyError, ValueError, TypeError):
        return default


# Changed-word fraction at/under which a tracked replace is split into surgical sub-edits instead of
# one whole-block Delete+Insert. > threshold -> one block change (today's behaviour); <= threshold ->
# per-changed-run redlines. See plugin/writer/word_diff_split.py.
_WORD_DIFF_THRESHOLD = _env_num(
    "WRITERAGENT_AGENT_EDIT_DIFF_THRESHOLD", 0.6, float, lambda v: 0.0 <= v <= 1.0)

# Max per-changed-run sub-edits before a surgical split falls back to ONE whole block. Bounds the
# cost of recording a very scattered edit (each sub-edit re-enumerates redlines).
_MAX_SURGICAL_RUNS = _env_num(
    "WRITERAGENT_AGENT_EDIT_MAX_SURGICAL_RUNS", 40, int, lambda v: v >= 1)

# Whether an agent edit's deletion and insertion are authored separately so LibreOffice's by-author
# coloring renders removed vs new text in two distinct colors (on), or as one author / one color
# (off). Off via the env var = any falsey token ("0", "false", "no", "off", "").
_SPLIT_AUTHOR_COLORS = os.environ.get(
    "WRITERAGENT_AGENT_EDIT_SPLIT_AUTHOR_COLORS", "1").strip().lower() not in ("0", "false", "no", "off", "")


# goRight takes a C++ short (max 32767); chunk like the rest of the codebase (format.py, ops.py,
# document_helpers.py) so a surgical sub-edit deep in a LONG block (the splitter's target case)
# never overflows the count.
_GO_RIGHT_CHUNK = 8192


def _go_right(cursor, n, expand):
    """Move (expand=False) or extend (expand=True) the cursor right by *n* chars, in chunks of
    _GO_RIGHT_CHUNK (UNO caps the count at a C++ short). Returns True only if the FULL *n* was
    consumed; False if goRight stopped early (end of text / an unexpected stop), so the caller can
    refuse to edit at a wrong offset instead of silently landing short."""
    while n > 0:
        step = n if n < _GO_RIGHT_CHUNK else _GO_RIGHT_CHUNK
        if not cursor.goRight(step, expand):
            return False
        n -= step
    return True


# Portion types whose presence keeps getString() char offsets aligned with the live cursor's
# goRight stops, so surgical sub-edits still land correctly. "SoftPageBreak" is an automatic
# (layout-only) page break: it contributes 0 chars to getString() and is not a goRight stop --
# verified that navigating by getString offsets across one lands exactly right. Without it, any
# paragraph that happens to straddle a page boundary was needlessly forced to whole-block. Real
# content portions (fields, footnotes, ruby, redline marks, bookmarks, ...) DO shift offsets and
# must keep returning False.
_OFFSET_SAFE_PORTION_TYPES = frozenset({"Text", "SoftPageBreak"})


def _block_safe_for_surgical(found):
    """True only when *found* is a SINGLE paragraph whose portions are all offset-safe (plain text
    or an automatic page break) and which has no tracked changes -- the case where getString() char
    offsets line up with the live cursor's goRight stops. A multi-paragraph block, a struck
    (tracked-deletion) run, or a real content portion (field/footnote/etc.) makes them diverge, so
    the surgical sub-edits would land in the wrong place. Best-effort: any doubt -> False, and the
    caller falls back to the whole-block replace (which handles every case).
    """
    from plugin.writer.edit_review import _string_skipping_redline
    try:
        # A tracked DELETION leaves struck text in getString() (the diff baseline) that the agent's
        # old_content never matched -> the split would be computed against the wrong text.
        if found.getString() != _string_skipping_redline(found, "Delete"):
            return False
        paras = found.createEnumeration()
        seen = 0
        while paras.hasMoreElements():
            para = paras.nextElement()
            seen += 1
            if seen > 1 or not para.supportsService("com.sun.star.text.Paragraph"):
                return False  # multi-paragraph or a table/other node
            portions = para.createEnumeration()
            while portions.hasMoreElements():
                if str(portions.nextElement().TextPortionType) not in _OFFSET_SAFE_PORTION_TYPES:
                    return False  # field / footnote / ruby / redline mark -> offsets diverge
        return seen == 1
    except Exception:
        log.debug("content: _block_safe_for_surgical check failed; treating as unsafe", exc_info=True)
        return False


# Base title for the grouped undo action wrapping a surgical batch. Each batch gets a UNIQUE title
# (base + a monotonic counter, see _next_surgical_undo_title) which both (a) collapses the batch into
# ONE Ctrl+Z and (b) identifies OUR context on the undo stack before rolling it back on a mid-apply
# failure. Uniqueness is load-bearing across the all_matches loop: an earlier match leaves its
# COMPLETED context titled on top of the stack, so a later match whose EMPTY context is discarded on
# leave must NOT mistake that earlier title for its own and undo a prior good edit (atomicity).
_SURGICAL_UNDO_TITLE = "WriterAgent surgical edit"
_surgical_batch_counter = itertools.count(1)


def _next_surgical_undo_title() -> str:
    """A process-unique grouped-undo title for one surgical batch (base + a monotonic counter)."""
    return "%s#%d" % (_SURGICAL_UNDO_TITLE, next(_surgical_batch_counter))


# Base title for the grouped-undo action wrapping a whole HTML/import edit (replace_full_document,
# replace_single_range_with_content, selection insert). Shares the surgical counter so EVERY
# WriterAgent context gets a globally-unique title -- load-bearing for the rollback identity check in
# _close_surgical_context (it undoes a failed batch only when ITS unique title is on top of the stack).
_AGENT_EDIT_UNDO_TITLE = "WriterAgent edit"


def _next_agent_edit_undo_title() -> str:
    """A process-unique grouped-undo title for one HTML/import edit (shares the monotonic counter)."""
    return "%s#%d" % (_AGENT_EDIT_UNDO_TITLE, next(_surgical_batch_counter))


def _close_surgical_context(undo_mgr, session, changes_before, applied_ok, undo_title):
    """Close the surgical undo context -- pairing the earlier enterUndoContext exactly once -- and,
    on failure, roll the partial batch back.

    leaveUndoContext is ALWAYS attempted: the XUndoManager contract requires every enter to be matched
    by a leave, and skipping it would leave the context open so later edits nest inside our group and
    the undo stack drifts. A leave failure is logged at WARNING (it can leave the stack inconsistent),
    not swallowed at debug. On the SUCCESS path nothing else is needed -- the edits are valid; the
    worst a failed leave does is leave the grouping open, surfaced loudly rather than by undoing
    good changes.

    On the FAILURE path we additionally roll back: only if the leave succeeded AND the top of the undo
    stack is THIS batch's unique ``undo_title`` do we undo it -- reverting even a half-applied
    delete+insert -- then drop the partial change records + anchor bookmarks. The per-batch-unique
    title is essential: when our context was empty (first sub-edit failed before mutating) it is
    discarded on leave, exposing whatever was on top before -- which in an all_matches loop is an
    EARLIER successful surgical batch; a constant title would match it and undo that good edit, so we
    match the unique title and skip undo when it is not ours. Records are KEPT (not orphaned) when
    undo() itself fails. Best-effort throughout; never masks the caller's original error."""
    left = False
    try:
        undo_mgr.leaveUndoContext()
        left = True
    except Exception:
        log.warning("content: leaveUndoContext failed; the undo stack may be inconsistent", exc_info=True)

    if applied_ok:
        return  # success: edits stand; a failed leave was already surfaced above

    undone = False
    if left:
        try:
            titles = undo_mgr.getAllUndoActionTitles()  # newest-first per XUndoManager
            if titles and titles[0] == undo_title:
                undo_mgr.undo()
                undone = True
        except Exception:
            log.warning("content: undo of partial surgical batch failed; document may be partially "
                        "edited", exc_info=True)
    # Trim records when the document mutations were reverted, or when nothing was applied at all
    # (empty context -> changes unchanged). Keep them when undo() demonstrably failed (or couldn't
    # run because the context wouldn't close) so a live partial edit keeps a reviewable record.
    kept = len(session.changes) - changes_before
    if undone or kept == 0:
        try:
            session.discard_changes_since(changes_before)
        except Exception:
            log.debug("content: discarding partial surgical change records failed", exc_info=True)
    else:
        log.warning("content: surgical rollback could not undo a partial batch; keeping %d change "
                    "record(s) so the partial edit stays reviewable", kept)


def _apply_in_undo_context(doc, session, run):
    """Run *run(in_undo_context)* -- which must perform exactly ONE session.record_mutation -- inside a
    fresh grouped-undo context when one can be opened, so a split-author delete+insert stays atomic:
    on failure the whole context is undone (reverting even a half-applied edit) and the partial record
    dropped.

    Mirrors the surgical batch's context handling: opens a PROCESS-UNIQUE context (so an earlier
    batch's completed title on the undo stack is never mistaken for ours when rolling back) only when
    the undo manager is usable and UNLOCKED (a locked manager silently ignores enterUndoContext, so we
    would hold no real context and no rollback), calls ``run(True)``, and ALWAYS pairs
    the enter with exactly one leave via _close_surgical_context. Without a usable, unlocked manager it
    falls back to ``run(False)`` -- the single atomic setString, itself all-or-nothing (so the only
    cost of the fallback is that the edit renders in one color instead of two)."""
    undo_mgr = None
    undo_title = _next_surgical_undo_title()
    try:
        mgr = doc.getUndoManager()
        if mgr.isLocked():
            raise RuntimeError("undo manager is locked; enterUndoContext would be a no-op")
        mgr.enterUndoContext(undo_title)
        undo_mgr = mgr
    except Exception:
        undo_mgr = None
    if undo_mgr is None:
        log.debug("content: no usable/unlocked undo manager; split-author whole-block edit falls back "
                  "to the single atomic op (one color)")
        run(False)
        return

    changes_before = len(session.changes)
    applied_ok = False
    try:
        run(True)
        applied_ok = True
    finally:
        # ALWAYS pair the enterUndoContext with exactly one leave (XUndoManager contract); on failure
        # this also rolls the partial edit back. The original exception (if any) propagates after.
        _close_surgical_context(undo_mgr, session, changes_before, applied_ok, undo_title)


def _record_html_atomically(session, doc, mutate, track_reviewable, **record_kwargs):
    """Record an HTML/import mutation that DELETES before it inserts -- replace_full_document,
    replace_single_range_with_content, and the selection branch of insert_content_at_position all do
    setString("") and THEN run a separate HTML import that can throw -- so it is ATOMIC: the delete
    and the import either both land or neither does.

    record_mutation() opens no undo context, so a throwing import would otherwise strand a bare
    deletion (a half-applied edit) and register no reviewable change. Unlike the plain-text path there
    is no atomic single-op variant of an HTML import, so when recording a reviewable change we wrap the
    whole mutation in ONE grouped undo context and, on failure, undo it (reverting the partial
    deletion) before re-raising. If no usable/unlocked undo manager is available we REFUSE before
    mutating (fail closed) rather than risk a partial edit. When NOT recording (no review contract) the
    mutation runs directly, exactly as before."""
    if not track_reviewable:
        return session.record_mutation(mutate, **record_kwargs)

    undo_title = _next_agent_edit_undo_title()
    try:
        mgr = doc.getUndoManager()
        if mgr.isLocked():
            raise RuntimeError("undo manager is locked; enterUndoContext would be a no-op")
        mgr.enterUndoContext(undo_title)
    except Exception:
        # No rollback available -> we cannot guarantee all-or-nothing, and an HTML import has no atomic
        # single-op fallback. Refuse BEFORE any mutation so the document is never left half-edited.
        raise ToolExecutionError(
            "Cannot apply this content edit atomically in review mode (no usable undo context); "
            "refusing rather than risk a half-applied edit.")

    changes_before = len(session.changes)
    applied_ok = False
    try:
        result = session.record_mutation(mutate, **record_kwargs)
        applied_ok = True
        return result
    finally:
        # Pair the enter with exactly one leave; on failure undo the partial edit and drop its record.
        _close_surgical_context(mgr, session, changes_before, applied_ok, undo_title)


def _record_preserve_replace(session, doc, found, new_text, uno_ctx, split):
    """Record a format-preserving replace as ONE reviewable change, or -- when *split* (review
    recording is on) and only PART of the block changed -- as several SURGICAL sub-changes,
    each its own tracked Delete+Insert with its own accept/reject outcome.

    Splitting keeps a one- or two-word tweak in a long paragraph from rendering (and having to be
    accepted) as a whole-paragraph delete+insert. The agent still issues a single edit; it simply
    gets one outcome per sub-change. A large change (> threshold of words changed) stays a single
    clean block edit, matching today's behaviour.
    """
    from . import format as format_support

    # Split-author coloring (deletion authored distinctly from insertion -> two by-author colors) is
    # applied CONSISTENTLY to both the whole-block and surgical paths, so an agent edit looks the same
    # however it lands. It is meaningful only when recording tracked changes, so it is scoped to
    # *split* (review recording on) -- the non-review path is left exactly as before. Default on;
    # override with WRITERAGENT_AGENT_EDIT_SPLIT_AUTHOR_COLORS.
    split_author = split and _SPLIT_AUTHOR_COLORS

    def _bound(s):  # this path is plain text (use_preserve); just cap the preview length
        s = s or ""
        return s if len(s) <= 300 else s[:299] + "…"

    def _whole():
        # The split-author two-step (delete authored distinctly, then insert) needs an open undo
        # context to stay atomic, so when it's on we wrap the single record_mutation in one
        # (_apply_in_undo_context; it falls back to the atomic single-op if no usable manager). When
        # split-author is off, in_undo_context=False uses the single atomic setString directly (one UNO
        # action, never a partial deletion) -- today's behaviour.
        original = found.getString()

        def _run(in_undo_context):
            session.record_mutation(
                lambda: format_support.replace_preserving_format(
                    doc, found, new_text, uno_ctx,
                    in_undo_context=in_undo_context, split_author=split_author),
                original_preview=_bound(original), proposed_preview=_bound(new_text))

        if split_author:
            _apply_in_undo_context(doc, session, _run)
        else:
            _run(False)

    if not split:
        _whole()
        return

    from plugin.writer.word_diff_split import split_change

    result = split_change(found.getString(), new_text, _WORD_DIFF_THRESHOLD)
    if not result.is_surgical:
        _whole()                       # big change -> single clean block (today's behaviour)
        return
    if not result.sub_edits:
        return                         # old == new: nothing changed, record nothing
    if len(result.sub_edits) > _MAX_SURGICAL_RUNS:
        _whole()                       # too many scattered runs -> one block (bounded cost)
        return
    if not _block_safe_for_surgical(found):
        _whole()                       # not plain single-paragraph text -> offsets unsafe; whole-block
        return

    text = found.getText()
    anchor = found.getStart()

    def _select(se):
        """A fresh cursor selecting old[se.old_start:se.old_end] within the block, navigating from
        the block start by char offset. Returns None if the cursor cannot be positioned EXACTLY
        (goRight stopped early), so the caller never edits the wrong span."""
        sub = text.createTextCursorByRange(anchor)
        if se.old_start and not _go_right(sub, se.old_start, False):
            return None
        if se.old_end > se.old_start and not _go_right(sub, se.old_end - se.old_start, True):
            return None
        return sub

    # Pre-flight: every sub-edit must select EXACTLY its expected old_text on the still-pristine
    # block before we mutate anything. _block_safe_for_surgical already vets the block, but if an
    # offset still doesn't line up (a stop it didn't catch), abort surgical and fall back to the
    # whole-block replace -- never silently edit the wrong characters.
    for se in result.sub_edits:
        sub = _select(se)
        if sub is None or sub.getString() != se.old_text:
            log.debug("content: surgical pre-flight offset mismatch; falling back to whole-block")
            _whole()
            return

    # Validated. Apply right-to-left so each sub-edit's char offsets into the ORIGINAL block stay
    # valid (the text to its left is still pristine when we reach it). goRight is chunked.
    #
    # Atomicity: a sub-edit replace DELETES the old text before inserting the new, and the
    # post-edit redline enumeration can also raise -- so a sub-edit CAN fail mid-apply, after earlier
    # ones already landed. The pre-flight makes that rare, not impossible. To keep the batch
    # all-or-nothing we group every sub-edit in ONE document undo context: on any failure we undo the
    # whole context (reverting even a half-applied delete+insert) and drop the partial change records
    # plus their anchor bookmarks, then re-raise so the tool reports an honest failure instead of a
    # silently half-rewritten paragraph. Without a usable undo manager we can't group a multi-edit
    # batch, so we fall back to the whole-block replace -- which is itself atomic (it passes
    # in_undo_context=False, so format uses the single atomic setString, never a partial deletion).
    undo_mgr = None
    undo_title = _next_surgical_undo_title()
    try:
        mgr = doc.getUndoManager()
        # A LOCKED undo manager silently IGNORES enterUndoContext (and would not record the
        # mutations), so we'd hold no real context and have no rollback. Detect it and fall back
        # rather than apply a multi-edit batch we cannot undo.
        if mgr.isLocked():
            raise RuntimeError("undo manager is locked; enterUndoContext would be a no-op")
        mgr.enterUndoContext(undo_title)
        undo_mgr = mgr
    except Exception:
        undo_mgr = None
    if undo_mgr is None:
        log.debug("content: no usable/unlocked undo manager; surgical edit falls back to whole-block "
                  "for atomicity")
        _whole()
        return

    changes_before = len(session.changes)
    applied_ok = False
    try:
        for se in sorted(result.sub_edits, key=lambda e: e.old_start, reverse=True):
            def apply_se(se=se):
                sub = _select(se)
                # Pre-flight proved these offsets on the pristine block and right-to-left order keeps
                # the left pristine, so this should always match. If it ever doesn't, fail LOUD rather
                # than corrupt the doc or ship a silent partial edit (tripwire).
                if sub is None or sub.getString() != se.old_text:
                    raise RuntimeError(
                        "surgical sub-edit offset drifted at apply time; aborting to avoid corruption")
                # in_undo_context=True: this runs inside the undo context opened below, so the
                # split-author delete+insert is safe -- a failed insert is rolled back by the context
                # (the explicit flag, NOT a guess from the manager state). split_author
                # is threaded through so the surgical path honours the same coloring choice as the
                # whole-block path (off -> the atomic single-op, still inside this context).
                format_support.replace_preserving_format(doc, sub, se.new_text, uno_ctx,
                                                         in_undo_context=True,
                                                         split_author=split_author)

            session.record_mutation(
                apply_se,
                original_preview=_bound(se.old_text),
                proposed_preview=_bound(se.new_text))
        applied_ok = True
    finally:
        # ALWAYS pair the enterUndoContext with exactly one leave (XUndoManager contract); on failure
        # this also rolls the partial batch back. The original exception (if any) propagates after.
        _close_surgical_context(undo_mgr, session, changes_before, applied_ok, undo_title)


# ------------------------------------------------------------------
# GetDocumentContent
# ------------------------------------------------------------------


class GetDocumentContent(ToolBase):
    """Export the document (or a portion) as formatted content."""

    name = "get_document_content"
    description = "Get document (or selection/range) content. Result includes document_length. scope: full, selection, or range (requires start, end)."
    parameters = {
        "type": "object",
        "properties": {
            "scope": {"type": "string", "enum": ["full", "selection", "range"], "description": ("Return full document (default), current selection/cursor region, or a character range (requires start and end).")},
            "max_chars": {"type": "integer", "description": "Maximum characters to return."},
            "start": {"type": "integer", "description": "Start character offset (0-based). Required for scope 'range'."},
            "end": {"type": "integer", "description": "End character offset (exclusive). Required for scope 'range'."},
            "include_images": {"type": "boolean", "description": "Include embedded image data (base64) in export. Default false."},
        },
        "required": [],
    }
    uno_services = ["com.sun.star.text.TextDocument"]
    tier = "core"

    def execute(self, ctx, **kwargs):
        from . import format as format_support
        scope = kwargs.get("scope", "full")
        max_chars = kwargs.get("max_chars")
        range_start = kwargs.get("start") if scope == "range" else None
        range_end = kwargs.get("end") if scope == "range" else None

        if scope == "range" and (range_start is None or range_end is None):
            return self._tool_error("scope 'range' requires start and end.")

        include_images = bool(kwargs.get("include_images", False))
        content = format_support.document_to_content(
            ctx.doc,
            ctx.ctx,
            ctx.services,
            max_chars=max_chars,
            scope=scope,
            range_start=range_start,
            range_end=range_end,
            include_images=include_images,
        )
        doc_len = ctx.services.document.get_document_length(ctx.doc)
        result = {"status": "ok", "content": content, "length": len(content), "document_length": doc_len}
        if scope == "range" and range_start is not None and range_end is not None:
            result["start"] = int(range_start)
            result["end"] = int(range_end)
        return result


# ------------------------------------------------------------------
# ApplyDocumentContent
# ------------------------------------------------------------------

# ------------------------------------------------------------------
# ApplyDocumentContent
# ------------------------------------------------------------------


class ApplyDocumentContent(ToolBase):
    """Insert or replace content in the document.

    Design notes (important for callers and future maintainers):

    - **Two edit paths**:
      - *Import path* (HTML/markup): for structural rewrites (tables, headings,
        page changes) we prepare HTML in `format_support` and import it via
        ``insertDocumentFromURL``. This is what all of the `insert_*` helpers
        use.
      - *Format‑preserving path* (plain text): for small textual corrections
        we avoid HTML entirely and call `format_support.replace_preserving_format`,
        which mutates characters in place so existing character‑level styling
        (bold, colors, background fills, etc.) is preserved even when the
        replacement text length differs.

    - **Decision rule**: we treat content as *plain text* (and thus eligible
      for format‑preserving replacement) only when `content_has_markup` is
      false. Any obvious HTML/Markdown markers force the import path. This
      keeps the heuristic simple and robust: small literal edits naturally
      stay plain text; rich formatting naturally uses HTML.

    - **Raw vs wrapped content**: `raw_content` is captured *before* any HTML
      wrapping or newline normalization and is passed to the preserving path;
      the (possibly HTML‑wrapped) `content` value is passed to the import path.
      Mixing these up will overwrite document text with serialized HTML rather
      than the intended human‑readable string.

    - **Search** (``target='search'`` only): ``old_content`` must be a **substring** to find —
      a phrase, sentence, or multi-paragraph **block**, not the entire document. To replace
      **all** document content, you **must** use ``target='full_document'`` with ``content`` only;
      **never** pass the full body as ``old_content``. Search uses ``_find_chained_range`` (LO
      regex + paragraph chaining). See ``tests/writer/test_content_search_uno.py``.
    """

    name = "apply_document_content"
    description = (
        "Insert or replace content. "
        f"IMPORTANT: {APPLY_DOCUMENT_CONTENT_TOOL_RESEARCH_HINT} "
        "To replace the ENTIRE document use target='full_document' with content only — "
        "do NOT pass the whole document as old_content. "
        "Use target='beginning', 'end', or 'selection' to insert. "
        "Use target='search' with old_content for find-and-replace of a specific substring only."
    )
    parameters = {
        "type": "object",
        "properties": {
            "content": {"type": "array", "items": {"type": "string"}, "description": ("List of HTML fragments or plain-text fragments (one per block); shape and math per system prompt (APPLY_DOCUMENT_CONTENT AND HTML). No Markdown.")},
            "target": {"type": "string", "enum": ["beginning", "end", "selection", "full_document", "search"], "description": "Where to apply the content."},
            "old_content": {"type": "string", "description": ("Substring to find when target='search'. Not for whole-document replace — use target='full_document' instead.")},
            "all_matches": {"type": "boolean", "description": "Replace all occurrences (true) or first only. Default false. Only for target='search'."},
        },
        "required": ["content"],
    }
    uno_services = ["com.sun.star.text.TextDocument"]
    tier = "core"
    is_mutation = True

    def _review_wait_seconds(self, uno_ctx):
        """Max seconds the edit call should block waiting for review; 0 = don't wait."""
        try:
            return edit_review_wait_seconds(uno_ctx)
        except Exception:
            return 0

    def _wait_enabled_globally(self):
        """Config read without a tool context, for long_running/is_async (called by the
        MCP/chat shells before execute). False whenever the context isn't available."""
        try:
            from plugin.framework.uno_context import get_ctx

            return self._review_wait_seconds(get_ctx()) > 0
        except Exception:
            return False

    @property
    def long_running(self) -> bool:  # type: ignore[override]
        # With review-wait on, MCP must run this call on its HTTP thread so the wait can
        # block there (one request, one response -- the response just comes back after the
        # user reviews). With it off, stay a normal synchronous main-thread tool.
        return self._wait_enabled_globally()

    def is_async(self):
        # When review-wait is on, the chat worker / MCP HTTP thread hosts this call (the
        # main-thread guard in execute_safe is bypassed) and every document touch is
        # marshalled via execute_on_main_thread.
        if self._wait_enabled_globally():
            return True
        # Also stay "async" whenever we're ALREADY on a background thread: the chat loop
        # snapshots the async-tool set once per round and MCP reads long_running per call, so
        # review can be toggled OFF between that decision and now. If it was, we're running on a
        # worker thread with the live flag False -- returning True keeps execute_safe's
        # main-thread guard from rejecting us; execute() then runs the (now wait-free) edit on
        # the main thread via marshalling, so the toggle is handled safely instead of erroring.
        return threading.current_thread() is not threading.main_thread()

    def execute(self, ctx, **kwargs):
        wait_seconds = self._review_wait_seconds(ctx.ctx)
        on_main = threading.current_thread() is threading.main_thread()
        if get_agent_edit_review_mode(ctx.ctx) != "wait" or on_main:
            # No review-wait: review is off, it was toggled off after this call was dispatched
            # to a worker thread, or we ARE the main thread (where blocking would freeze the UI
            # and the user could never click accept/reject). Edit once, don't wait -- but UNO is
            # not thread-safe, so when we're on a worker thread (the toggled-off case) the edit
            # and its cleanup run on the main thread via marshalling rather than here.
            # The session is registered in session_box the instant _execute_edit creates it (via
            # session_sink), so its anchor bookmarks are released in `finally` even if the edit
            # raises mid-way (e.g. the 2nd of 3 replace-all matches fails after the 1st).
            session_box = []

            def _do_edit():
                return self._execute_edit(ctx, session_sink=session_box, **kwargs)

            try:
                if on_main:
                    result, _ = _do_edit()
                else:
                    from plugin.framework.queue_executor import execute_on_main_thread
                    result, _ = execute_on_main_thread(_do_edit, timeout=60.0)
                return result
            finally:
                if session_box:
                    if on_main:
                        session_box[0].cleanup()
                    else:
                        from plugin.framework.queue_executor import execute_on_main_thread
                        execute_on_main_thread(session_box[0].cleanup)

        # Review-wait path, on a background (MCP HTTP / chat worker) thread: run the edit
        # on the main thread, then block HERE until the user reviews the tracked changes.
        from plugin.framework.queue_executor import execute_on_main_thread

        # Capture the session as soon as _execute_edit creates it, so its anchor bookmarks can
        # be released even when the edit raises mid-way. The cleanup is itself marshalled, and
        # the queue executor serializes main-thread items, so it runs after the edit settles.
        session_box = []

        def _edit_on_main_thread():
            # session_sink registers the session in session_box the instant it's created, so
            # the `except` below can release its bookmarks even if the edit raises mid-way.
            return self._execute_edit(ctx, session_sink=session_box, **kwargs)

        try:
            # 60s edit budget: matches the synchronous MCP path's processing timeout; the
            # default 30s marshalling timeout would reject large full-document replaces that
            # are fine without review mode.
            result, session = execute_on_main_thread(_edit_on_main_thread, timeout=60.0)
        except Exception:
            if session_box:
                execute_on_main_thread(session_box[0].cleanup)
            raise
        if session is None or not session.changes or result.get("status") != "ok":
            if session is not None:
                execute_on_main_thread(session.cleanup)
            return result

        # In-app chat: surface a sidebar status while we block (MCP has no sidebar -> None, no-op).
        # The callback marshals onto the chat drain queue, so it is safe from this worker thread.
        status_cb = getattr(ctx, "status_callback", None)
        if callable(status_cb):
            try:
                status_cb("Review the agent's changes in the document — accept or reject the tracked changes.")
            except Exception:
                log.debug("apply_document_content: status_callback failed", exc_info=True)

        # Stop waiting early when the review feature is toggled off mid-wait OR the user
        # cancels the chat turn (Stop button); MCP has no stop predicate -> None.
        user_stop = getattr(ctx, "stop_checker", None)

        def _stop():
            if get_agent_edit_review_mode(ctx.ctx) != "wait":
                return True
            try:
                return bool(user_stop()) if callable(user_stop) else False
            except Exception:
                return False

        review = session.wait_for_review(
            timeout=wait_seconds,
            stop_checker=_stop,
            uno_runner=execute_on_main_thread,
        )
        result = dict(result)
        result["review"] = review
        if not review.get("complete"):
            result["message"] = (result.get("message") or "") + (
                " The user has not finished reviewing these tracked changes; ask them to"
                " accept or reject the changes in the document, then continue."
            )
        return result

    def _execute_edit(self, ctx, session_sink=None, **kwargs):
        """Apply the edit and return ``(result_dict, session_or_None)``.

        Runs on the MAIN thread always (directly on the sync path; marshalled via
        execute_on_main_thread on the review-wait path). The caller owns the session's
        wait/cleanup."""
        from . import format as format_support
        content = kwargs.get("content", "")
        old_content = kwargs.get("old_content")
        target = kwargs.get("target")

        if not target and old_content is not None:
            target = "search"
        if not target:
            return self._tool_error("Provide a target ('beginning', 'end', 'selection', 'full_document', 'search') or old_content for find-and-replace."), None

        if target == "search" and old_content is None:
            return self._tool_error("target='search' requires old_content."), None

        # Normalize content:
        # - If the model (or caller) serialized a list as a JSON string,
        #   parse it back to a real list first so commas/brackets do not
        #   become literal document text.
        if isinstance(content, str):
            stripped = content.strip()
            if stripped.startswith("[") and "<" in stripped:
                parsed = safe_json_loads(stripped)
                if isinstance(parsed, list):
                    content = parsed

        # Normalize list input to a single string for HTML import paths.
        if isinstance(content, list):
            _parts = [str(x) for x in content]
            _per_part_nl = [p.count("\n") for p in _parts]
            log.debug(
                "apply_document_content: list join n_parts=%d per_part_newline_counts=%s total_chars_before_join=%d",
                len(_parts),
                _per_part_nl[:20],  # cap log size
                sum(len(p) for p in _parts),
            )
            content = "\n".join(_parts)
            log.debug("apply_document_content: after join newline_count=%d has_math_tag=%s join_preview=%r", content.count("\n"), ("<math" in content.lower()), content[:500])
        # Detect markup BEFORE any HTML wrapping.
        use_preserve = isinstance(content, str) and not format_support.content_has_markup(content)

        if use_preserve and isinstance(content, str):
            _nl_before_esc = content.count("\n")
            content = content.replace("\\n", "\n").replace("\\t", "\t")
            _nl_after_esc = content.count("\n")
            if _nl_after_esc != _nl_before_esc:
                log.debug("apply_document_content: literal \\\\n/\\\\t escape expand (plain text) newline_count %d -> %d", _nl_before_esc, _nl_after_esc)

        raw_content = content

        config_svc = ctx.services.get("config")
        # Opt-in review mode: when doc.agent_edit_review_mode is record/wait, EditReviewSession records
        # the agent's edits as native tracked changes (redlines) the user can accept/reject --
        # tagging each logical change so its outcome can be reported -- and restores the prior
        # recording state. Default off -> the session is inert and behavior is unchanged.
        # get_config_bool_safe tolerates the flag not being registered yet (returns False).
        track_reviewable = review_recording_enabled(ctx.ctx)
        session = EditReviewSession(ctx.doc, ctx.ctx, enabled=track_reviewable)
        # Register with the caller's sink AS SOON AS the session exists, so its anchor
        # bookmarks are released even if the edit below raises mid-way (a replace-all that
        # anchors the 1st match then fails on the 2nd) -- before we ever return the session.
        if session_sink is not None:
            session_sink.append(session)

        def _plain_preview(value):
            s = str(value)
            if format_support.content_has_markup(s):
                try:
                    return format_support.html_to_plain_text(s, ctx.ctx, config_svc)
                except Exception:
                    return s
            return s

        if target == "full_document":
            with session:
                # Delete-then-import: make it atomic so a failed import can't strand a cleared document.
                _record_html_atomically(
                    session, ctx.doc,
                    lambda: format_support.replace_full_document(ctx.doc, ctx.ctx, content, config_svc=config_svc),
                    track_reviewable, proposed_preview=_plain_preview(content))
            return {"status": "ok", "message": "Replaced entire document."}, session
        if target == "end":
            with session:
                session.record_mutation(
                    lambda: format_support.insert_content_at_position(ctx.doc, ctx.ctx, content, "end", config_svc=config_svc),
                    proposed_preview=_plain_preview(content))
            return {"status": "ok", "message": "Inserted content at end."}, session
        if target == "selection":
            with session:
                # Selection insert clears the selection first, then imports -> atomic (full_document above).
                _record_html_atomically(
                    session, ctx.doc,
                    lambda: format_support.insert_content_at_position(ctx.doc, ctx.ctx, content, "selection", config_svc=config_svc),
                    track_reviewable, proposed_preview=_plain_preview(content))
            return {"status": "ok", "message": "Inserted content at selection."}, session
        if target == "beginning":
            with session:
                session.record_mutation(
                    lambda: format_support.insert_content_at_position(ctx.doc, ctx.ctx, content, "beginning", config_svc=config_svc),
                    proposed_preview=_plain_preview(content))
            return {"status": "ok", "message": "Inserted content at beginning."}, session

        # target == "search" from here on — old_content must be a findable substring, not the full body.
        # Whole-document replace: target='full_document' (no search, no old_content).
        old_stripped = str(old_content).strip()

        search_string = old_stripped
        if format_support.content_has_markup(search_string):
            search_string = format_support.html_to_plain_text(search_string, ctx.ctx, config_svc)
        # Collapse exotic horizontal whitespace; preserve newlines for paragraph-aware search.
        search_string = _normalize_search_string_for_find(search_string)
        if not search_string:
            # Parameter error (like old_content=None), not a search no-op: the search never ran,
            # so there's no replaced_count to report — use the standard tool error shape.
            return self._tool_error("old_content is empty after normalization."), session
        doc = ctx.doc
        # replaced_count is the machine-readable success signal: 0 -> status "error" (a silent
        # no-op surfaced as a failure), N>0 -> "ok". No matched_count/warning/partial-replace:
        # if a replace raises mid-all_matches the existing abort behavior stands.
        # TODO(follow-up): share search-path return dicts with string_eval_tools.py to avoid drift.
        all_matches = kwargs.get("all_matches", False)
        if all_matches:
            ranges = _find_all_ranges(doc, search_string)
            count = 0
            # Replace from last to first so earlier character offsets stay valid after edits.
            # One record_mutation per match: each replaced occurrence is its own reviewable
            # change with its own accept/reject outcome.
            with session:
                for found in reversed(ranges):
                    original = found.getString()
                    if use_preserve:
                        _record_preserve_replace(session, doc, found, raw_content, ctx.ctx, track_reviewable)
                    else:
                        _record_html_atomically(
                            session, doc,
                            lambda f=found: format_support.replace_single_range_with_content(doc, f, content, ctx.ctx, config_svc),
                            track_reviewable, original_preview=original, proposed_preview=_plain_preview(content))
                    count += 1
            if count == 0:
                return {"status": "error",
                        "message": "Replaced 0 occurrence(s). No matches found. Try a shorter substring.",
                        "replaced_count": 0}, session
            msg = "Replaced %d occurrence(s)." % count
            if use_preserve:
                msg += " (formatting preserved)"
            return {"status": "ok", "message": msg, "replaced_count": count}, session
        found = _find_first_range(doc, search_string)
        if found is None:
            return {"status": "error", "message": "old_content not found in document. Try a shorter, unique substring.",
                    "replaced_count": 0}, session
        original = found.getString()
        with session:
            if use_preserve:
                _record_preserve_replace(session, doc, found, raw_content, ctx.ctx, track_reviewable)
            else:
                _record_html_atomically(
                    session, doc,
                    lambda: format_support.replace_single_range_with_content(doc, found, content, ctx.ctx, config_svc),
                    track_reviewable, original_preview=original, proposed_preview=_plain_preview(content))
        msg = "Replaced 1 occurrence (by old_content)."
        if use_preserve:
            msg += " (formatting preserved)"
        return {"status": "ok", "message": msg, "replaced_count": 1}, session


# ------------------------------------------------------------------
# CloneHeadingBlock
# ------------------------------------------------------------------


class CloneHeadingBlock(ToolBaseDummy):
    """Clone an entire heading block (heading + all sub-headings + body)."""

    name = "clone_heading_block"
    intent = "edit"
    description = "Clone an entire heading block (heading + all sub-headings + body). The clone is inserted right after the original block."
    parameters = {"type": "object", "properties": {"locator": {"type": "string", "description": ("Locator of the heading to clone (e.g. 'bookmark:_mcp_abc123', 'heading_text:Introduction').")}, "paragraph_index": {"type": "integer", "description": "Paragraph index of the heading (0-based)."}}}
    uno_services = ["com.sun.star.text.TextDocument"]
    is_mutation = True

    def execute(self, ctx, **kwargs):
        from com.sun.star.text.ControlCharacter import PARAGRAPH_BREAK  # type: ignore

        para_index = _resolve_para_index(ctx, kwargs)
        if para_index is None:
            return self._tool_error("Provide locator or paragraph_index.")

        # Use writer_tree service to find the heading node and block size
        tree_svc = ctx.services.get("writer_tree")
        if tree_svc is None:
            return self._tool_error("writer_nav module not loaded; cannot resolve heading block.")

        tree = tree_svc.build_heading_tree(ctx.doc)
        node = tree_svc._find_node_by_para_index(tree, para_index)
        if node is None:
            return self._tool_error("No heading found at paragraph %d." % para_index)

        # Total paragraphs in the block: heading + body + all children
        total = 1 + tree_svc._count_all_children(node)

        # Collect elements for the block
        doc_text = ctx.doc.getText()
        enum = doc_text.createEnumeration()
        elements = []
        idx = 0
        while enum.hasMoreElements():
            el = enum.nextElement()
            if para_index <= idx < para_index + total:
                elements.append(el)
            if idx >= para_index + total - 1:
                break
            idx += 1

        if not elements:
            return self._tool_error("Could not collect heading block paragraphs.")

        # Insert duplicates after the last element of the block
        last = elements[-1]
        cursor = doc_text.createTextCursorByRange(last)
        cursor.gotoEndOfParagraph(False)

        for el in elements:
            txt = el.getString()
            sty = el.getPropertyValue("ParaStyleName")
            doc_text.insertControlCharacter(cursor, PARAGRAPH_BREAK, False)
            doc_text.insertString(cursor, txt, False)
            cursor.gotoStartOfParagraph(False)
            cursor.gotoEndOfParagraph(True)
            cursor.setPropertyValue("ParaStyleName", sty)
            cursor.gotoEndOfParagraph(False)

        return {"status": "ok", "message": "Cloned heading block '%s' (%d paragraphs)." % (node.get("text", ""), total), "heading_text": node.get("text", ""), "block_size": total}


# ------------------------------------------------------------------
# Internal helpers
# ------------------------------------------------------------------


def _resolve_para_index(ctx, kwargs):
    """Resolve locator or paragraph_index from tool kwargs.

    Returns an integer paragraph index, or None if neither is provided.
    """
    locator = kwargs.get("locator")
    para_index = kwargs.get("paragraph_index")

    if locator is not None and para_index is None:
        doc_svc = ctx.services.document
        resolved = doc_svc.resolve_locator(ctx.doc, locator)
        para_index = resolved.get("para_index")

    return para_index


def _resolve_style_name(doc, style_name):
    """Resolve a style name case-insensitively against the document styles."""
    try:
        families = doc.getStyleFamilies()
        para_styles = families.getByName("ParagraphStyles")
        if para_styles.hasByName(style_name):
            return style_name
        lower = style_name.lower()
        for name in para_styles.getElementNames():
            if name.lower() == lower:
                return name
    except Exception:
        pass
    return style_name


def _count_headings(nodes):
    """Recursively count heading nodes in a nested list."""
    count = 0
    for node in nodes:
        count += 1
        count += _count_headings(node.get("children", []))
    return count


def collect_document_stats(doc, doc_svc):
    """Character/word/paragraph/page/heading counts for a Writer document."""
    from plugin.doc.document_helpers import build_heading_tree

    try:
        text_obj = doc.getText()
        cursor = text_obj.createTextCursor()
        cursor.gotoStart(False)
        cursor.gotoEnd(True)
        full_text = get_string_without_tracked_deletions(cursor)
        char_count = len(full_text)
        word_count = len(full_text.split())
    except Exception:
        char_count = doc_svc.get_document_length(doc)
        word_count = 0

    try:
        para_ranges = doc_svc.get_paragraph_ranges(doc)
        para_count = len(para_ranges)
    except Exception:
        para_count = 0

    try:
        tree = build_heading_tree(doc)
        heading_count = _count_headings(tree.get("children", []))
    except Exception:
        heading_count = 0

    page_count = 0
    try:
        page_count = doc_svc.get_page_count(doc)
    except Exception:
        try:
            vc = doc.getCurrentController().getViewCursor()
            vc.jumpToLastPage()
            page_count = vc.getPage()
        except Exception:
            pass

    return {"character_count": char_count, "word_count": word_count, "paragraph_count": para_count, "page_count": page_count, "heading_count": heading_count}
