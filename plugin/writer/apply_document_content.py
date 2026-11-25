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
"""Writer apply_content tool — write HTML/text content into document."""

from __future__ import annotations

import contextlib
import functools
import logging
import re
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any

from plugin.framework.queue_executor import main
from plugin.framework.tool import ToolBase

if TYPE_CHECKING:
    from plugin.framework.tool import ToolContext

from plugin.writer.edit_review import (
    EditReviewSession,
    annotate_review_status,
    edit_review_wait_seconds,
    review_recording_enabled,
    get_agent_edit_review_mode,
    record_preserve_replace,
    record_html_atomically,
    collapsed_anchor,
    attach_edited_context,
    next_agent_edit_undo_title,
)
from plugin.writer.target_resolver import request_selection_range
from plugin.doc.compound_undo import UndoStepOrRollback, UndoUnavailable, commit_or_rollback
from plugin.writer.review_scan import document_has_tracked_deletions
from plugin.writer.search import replace_text_in_shape
from plugin.writer.specialized.tables import delete_leading_tables, record_table_deletions
from plugin.writer.table_nesting import cell_hosted_table_names
from plugin.writer.specialized_base import INSERT_TARGETS, enum_param
from plugin.framework.errors import (
    Diagnostic,
    SuggestedCall,
    ToolExecutionError,
    append_warnings,
    format_error_payload,
    reraise_if_disposed,
    suggest_call,
    uno_read,
)
from plugin.doc.text_helpers import KeepViewSelection
from plugin.framework.uno_context import enum_has_more, uno_same
from plugin.framework.json_utils import parse_html_fragment_list
from plugin.writer import (
    html_import,
    html_import_styles,
    image_placeholders,
    search as search_mod,
)
from plugin.writer.heading_links import (
    document_headings,
    headings_if_touched,
    retarget_renamed_links,
)
from plugin.writer.hyperlink_fixup import (
    OutlineSnapshot,
    capture_outline_hyperlinks,
    hyperlink_fields,
    plan_outline_updates,
    public_hyperlink_reports,
    restore_links,
)


log = logging.getLogger("writeragent.writer")


_OVERRIDE_NEEDS_ONE_LINK = (
    "hyperlink_url applies only when the match overlaps exactly one outline hyperlink (|outline)."
)


def _paragraph_boundary_cursor(text_obj: Any, found: Any, position: str) -> tuple[Any, bool]:
    """Collapsed cursor at the paragraph boundary nearest the match, for block content.

    ``before`` -> start of the paragraph holding the match start; ``after`` -> end of the
    paragraph holding the match end. The flag says whether the match edge was not already
    on that boundary (the insert moved away from the exact edge).
    """
    if position == "before":
        cursor = text_obj.createTextCursorByRange(found.getStart())
        moved = not cursor.isStartOfParagraph()
        cursor.gotoStartOfParagraph(False)
        return cursor, moved
    cursor = text_obj.createTextCursorByRange(found.getEnd())
    moved = not cursor.isEndOfParagraph()
    cursor.gotoEndOfParagraph(False)
    return cursor, moved

_EDGE_SPACE = " \t\u00a0"


def _match_stripped_edges(old_content: str, content: str) -> tuple[str, int]:
    """Line *content* up with the search, which drops the spaces at *old_content*'s edges.

    Returns ``(content, separator)``. A replacement loses the same edge spaces the search
    dropped (separator 0). A deletion -- empty content, or only spaces when old_content had edge
    spaces -- returns how many edge spaces old_content named, for the match to take along.
    Replacing a symbol with a space ("_" -> " ") stays a replacement.
    """
    lead = len(old_content) - len(old_content.lstrip(_EDGE_SPACE))
    trail = len(old_content) - len(old_content.rstrip(_EDGE_SPACE))
    if not content.strip(_EDGE_SPACE) and (not content or lead or trail):
        return "", max(lead, trail)
    cut_left = min(lead, len(content) - len(content.lstrip(_EDGE_SPACE)))
    cut_right = min(trail, len(content) - len(content.rstrip(_EDGE_SPACE)))
    return content[cut_left:len(content) - cut_right], 0


def _grow_over_edge_space(found: Any, separator: int) -> Any:
    """*found* grown over up to *separator* spaces on its left, or on its right when the left
    has none (a word at a paragraph start keeps no leading space)."""
    if not separator:
        return found
    text = found.getText()
    start = text.createTextCursorByRange(found.getStart())
    grew = 0
    for _unused in range(separator):
        probe = text.createTextCursorByRange(start.getStart())
        if not probe.goLeft(1, True) or not probe.getString() or probe.getString() not in _EDGE_SPACE:
            break
        start.goLeft(1, False)
        grew += 1
    grown = text.createTextCursorByRange(start.getStart())
    grown.gotoRange(found.getEnd(), True)
    for _unused in range(0 if grew else separator):
        probe = text.createTextCursorByRange(grown.getEnd())
        if not probe.goRight(1, True) or not probe.getString() or probe.getString() not in _EDGE_SPACE:
            break
        grown.goRight(1, True)
    return grown


def _table_deletion_result(names: list[str], tracked: bool) -> dict[str, Any]:
    """Result fields when an empty replacement removed the table, not just its text."""
    label = ", ".join("'%s'" % name for name in names)
    if tracked:
        message = (
            "Table %s marked for deletion as a tracked change: it stays in the document, "
            "struck through, until the user accepts the change. Do not accept or reject it yourself."
            % label
        )
    elif len(names) == 1:
        message = "Table %s deleted." % label
    else:
        message = "Tables deleted: %s." % label
    result: dict[str, Any] = {
        "status": "ok",
        "message": message,
        "table_deleted": names[0] if len(names) == 1 else names,
    }
    if tracked:
        result["pending_review"] = True
    return result


def _table_and_name(found: Any) -> tuple[Any, str | None]:
    """The TextTable and name for a search match, or (None, None) when not in a cell."""
    table = uno_read(lambda: found.getText().createTextCursorByRange(found.getStart()).getPropertyValue("TextTable"), None)
    if table is None:
        return None, None
    return table, uno_read(lambda: str(table.getName() or ""), "") or None


def _range_in_cell(found: Any, cell: Any) -> bool:
    """True when *found* lies in *cell*.

    PyUNO hands out distinct wrappers for one cell, so identity is checked with
    ``uno_same`` and then by asking the cell to build a cursor at the match.
    A match in a different cell raises instead of building that cursor.
    """

    def same_text() -> bool:
        return bool(uno_same(found.getText(), cell))

    def cursor_fits() -> bool:
        cell.createTextCursorByRange(found.getStart())
        return True

    return uno_read(same_text, False) or uno_read(cursor_fits, False)


def _match_is_whole_cell(found: Any, cell: Any, cell_text: str) -> bool:
    if not _range_in_cell(found, cell):
        return False
    return uno_read(lambda: str(found.getString() or "").strip() == cell_text, False)


def _empty_replacement_clears_table(table: Any, matches: list[Any]) -> bool:
    """True when every cell is empty or fully covered by *matches*.

    Unreadable cells and hosted nested tables return False so the caller edits
    text instead of guessing which table to remove.
    """
    cell_names: list[Any] = uno_read(lambda: list(table.getCellNames()), [])
    if not cell_names:
        return False

    for cell_name in cell_names:
        cell = uno_read(lambda: table.getCellByName(cell_name), None)
        if cell is None or cell_hosted_table_names(cell):
            return False
        # Raw text, pending deletions included: a cell that still holds a pending change is
        # not an empty shell, so an empty replacement must not delete the whole table.
        text = uno_read(lambda: cell.getString().strip(), None)
        if text is None:
            return False
        if not text:
            continue
        if not any(_match_is_whole_cell(found, cell, text) for found in matches):
            return False
    return True


def writer_tables_emptied_by_matches(ranges: list[Any], content: Any) -> list[tuple[Any, str]]:
    """Tables whose last text this empty replacement removes.

    Asked to delete a table, agents empty its text with
    apply_content and get status ok — the shell stays and
    the agent reports success. A hint on every empty cell is the
    wrong signal: clearing one cell of a fee table is a normal
    edit. The table goes only when every cell is already empty or
    one of *ranges* is that cell's entire text. A table that hosts
    a nested table is left alone (the host-cell wipe refusal still
    applies). A non-empty replacement returns [].
    """
    if str(content or "").strip():
        return []
    grouped: dict[str, tuple[Any, list[Any]]] = {}
    for found in ranges:
        table, name = _table_and_name(found)
        if not name:
            continue
        grouped.setdefault(name, (table, []))[1].append(found)
    emptied: list[tuple[Any, str]] = []
    for name, (table, matches) in grouped.items():
        if _empty_replacement_clears_table(table, matches):
            emptied.append((table, name))
    return emptied


def _range_contains_table(text_range: Any) -> bool:
    """True when text_range contains a TextTable between its endpoints."""
    enum = uno_read(lambda: text_range.createEnumeration(), None)
    if enum is None:
        return False
    while uno_read(lambda: enum_has_more(enum), False):
        elem = uno_read(lambda: enum.nextElement(), None)
        if elem is not None and uno_read(lambda: elem.supportsService("com.sun.star.text.TextTable"), False):
            return True
    return False


def build_search_replace_response(
    replaced_count: int,
    *,
    use_preserve: bool = False,
    message: str | None = None,
) -> dict[str, Any]:
    """Standardized search-replace response dictionary."""
    if message is not None:
        msg = message
    elif replaced_count == 1:
        msg = "Replaced 1 occurrence (by old_content)."
    else:
        msg = f"Replaced {replaced_count} occurrence(s)."
    if use_preserve:
        msg += " (formatting preserved)"
    return {"status": "ok", "message": msg, "replaced_count": replaced_count}


def _find_ranges(
    doc: Any, pattern: str, regex: bool, cs: bool | None, *, limit: int
) -> tuple[list[Any], search_mod.MatchMode]:
    if regex:
        resolved_cs = search_mod.resolve_case_sensitive(cs)
        if limit > 1:
            hits = search_mod.find_ranges_regex_case(doc, pattern, True, resolved_cs, all_matches=True)
        else:
            first = search_mod.find_ranges_regex_case(doc, pattern, True, resolved_cs, all_matches=False)
            hits = [] if first is None else [first]
        return hits, search_mod.MatchMode(resolved_cs, False)
    return search_mod.find_literal_ranges(doc, pattern, case_sensitive=cs, limit=limit)


# ------------------------------------------------------------------
# ApplyDocumentContent
# ------------------------------------------------------------------


def _partial_match_style_suggestion(
    doc: Any,
    params: EditParams,
    style_token: str | None,
    total_matches: int,
) -> SuggestedCall | None:
    """Build a suggested_call for apply_style after a partial-match style skip.

    Follows the nine safety rules from section 3.5 of structured-edit-diagnostics-plan.md:
    1. Target new visible text (params.plain), not the original search string.
    2. Single paragraph check: params.plain must be a single paragraph (no newlines).
    3. Exactly one distinct non-empty data-lo-style token.
    4. Resolved style token must not fall back to Standard unless it was Standard.
    5. Exact post-write match count in document matches expected count.
    6. Case sensitivity: case_sensitive=True.
    7. Host paragraph restyling notice: caller's warning message explicitly notifies
       that apply_style restyles the entire host paragraph.
    8. all_matches: True included if params.all_matches is True.
    9. target != "selection": selection cursors do not reliably span the new text afterwards.
    """
    if params.target == "selection":
        return None

    plain = params.plain
    if not plain or "\n" in plain or "\r" in plain:
        return None

    if not style_token:
        return None

    fam = uno_read(lambda: doc.getStyleFamilies().getByName("ParagraphStyles"), None)
    if fam is None:
        # Without the style family we cannot verify the style exists or resolve tokens.
        return None
    element_names: list[str] = uno_read(lambda: list(fam.getElementNames()), [])
    resolved_style = html_import_styles.resolve_paragraph_style_token(
        fam, style_token, element_names=element_names
    )
    if resolved_style == "Standard" and style_token != "Standard":
        return None

    try:
        matches = search_mod.find_all_ranges(doc, plain, case_sensitive=True)
        actual_count = len(matches) if matches else 0
    except Exception as exc:
        reraise_if_disposed(exc)
        return None

    expected_count = total_matches if params.all_matches else 1
    if actual_count != expected_count:
        return None

    suggestion_args: dict[str, Any] = {
        "target": "search",
        "old_content": plain,
        "style": resolved_style,
        "case_sensitive": True,
    }
    if params.all_matches:
        suggestion_args["all_matches"] = True
    return suggest_call("apply_style", **suggestion_args)


def _run_on_main_thread(fn: Any) -> Any:
    if main.is_current():
        return fn()
    return main.call(fn, timeout=60.0)


def _search_options(
    ctx: ToolContext,
    kwargs: Any,
    old_stripped: str,
    search_string: str,
) -> tuple[bool, bool | None, str, str | None]:
    regex = bool(kwargs.get("regex"))
    cs = search_mod.explicit_case_sensitive(kwargs.get("case_sensitive"))
    pattern = old_stripped if regex else search_string
    rex_err = search_mod.validate_icu_regex(ctx.ctx, old_stripped) if regex else None
    return regex, cs, pattern, rex_err


@dataclass(frozen=True)
class AnchorSpec:
    start: str
    end: str
    case_sensitive: bool | None = None


@dataclass(frozen=True)
class EditParams:
    content: str
    plain: str
    use_preserve: bool
    has_table: bool
    target: str
    position: str
    occurrence: int | None
    all_matches: Any
    track_reviewable: bool
    search_string: str = ""
    pattern: str = ""
    regex: bool = False
    case_sensitive: bool | None = None
    separator: int = 0
    override: str | None = None
    anchor: AnchorSpec | None = None


_ANCHOR_MISS_CODES: dict[search_mod.AnchorMissKind, str] = {
    "start": "ANCHOR_START_NOT_FOUND",
    "end": "ANCHOR_END_NOT_FOUND",
    "different_text": "ANCHOR_TEXT_DIFFERENT",
}


class ApplyDocumentContent(ToolBase):
    """Insert or replace content in the document.

    Design notes (important for callers and future maintainers):

    - **Two edit paths**:
      - *Import path* (HTML/markup): for structural rewrites (tables, headings,
        page changes) we prepare HTML in `html_import` and import it via
        ``insertDocumentFromURL``. This is what all of the `insert_*` helpers
        use.
      - *Format‑preserving path* (plain text): for small textual corrections
        we avoid HTML entirely and call `html_import.replace_preserving_format`,
        which mutates characters in place so existing character‑level styling
        (bold, colors, background fills, etc.) is preserved even when the
        replacement text length differs.

    - **Decision rule**: we treat content as *plain text* (and thus eligible
      for format‑preserving replacement) only when `content_has_markup` is
      false. Any obvious HTML/Markdown markers force the import path. This
      keeps the heuristic simple and robust: small literal edits naturally
      stay plain text; rich formatting naturally uses HTML.

    - **Search** (``target='search'`` only): ``old_content`` must be a **substring** to find —
      a phrase, sentence, or multi-paragraph **block**, not the entire document. To replace
      **all** document content, you **must** use ``target='full_document'`` with ``content`` only;
      **never** pass the full body as ``old_content``. Search uses ``search.find_literal_ranges`` (LO
      regex search, then paragraph chaining). See ``tests/writer/test_content_search_uno.py``.
      A match that sits in an outline hyperlink (``#…|outline``) also updates that
      target when the matched text occurs once in the URL outside that suffix, or
      when ``hyperlink_url`` is passed for that one link. ``content`` may equal
      ``old_content`` when only the URL is stale. A repeated title is left
      unchanged. Bookmark targets are not rewritten. See ``hyperlink_fixup``.
      In ``hyperlinks``, ``hyperlink_url_after`` is empty when the replace left no link.
      To replace a range between landmarks without copying the middle, use
      target='search' with start_anchor and end_anchor instead of old_content.
      The range runs from the start of start_anchor through the end of the first
      end_anchor beginning at or after that start match in the same text.
      occurrence selects the start match; regex, all_matches, position,
      dry_run, and hyperlink_url do not apply to anchor spans.
      Multi-line plain text becomes new paragraphs; single-line plain text replaces the span
      inline and keeps the host paragraph and its style. A renamed heading's links follow it.
    """

    name: str | None = "apply_content"
    description: str = (
        "Insert or replace content. "
        "To replace the ENTIRE document use target='full_document' with content only — "
        "do NOT pass the whole document as old_content. "
        "Use target='beginning', 'end', or 'selection' to insert. "
        "Use target='search' with old_content or both start_anchor and end_anchor for find-and-replace of a specific substring or range. "
        "An empty replacement that removes the last text in a table deletes that table."
    )
    parameters: dict[str, Any] | None = {
        "type": "object",
        "properties": {
            "content": {"type": ["array", "string"], "items": {"type": "string"}, "description": ("HTML or plain-text fragments: prefer a list with one block per item; one string is also accepted and is read the same way (a list is joined with newlines). Shape and math per the APPLY_DOCUMENT_CONTENT AND HTML rules — the editing-html guidance covers them if they are not already in your context. No Markdown. An empty search replacement deletes the match.")},
            "target": enum_param(INSERT_TARGETS, "Where to apply the content."),
            "old_content": {"type": "string", "description": ("Substring to find when target='search'; may span paragraphs (newlines). Not for whole-document replace — use target='full_document' instead.")},
            "start_anchor": {"type": "string", "description": "Literal substring marking the start of a range to replace (target='search'); requires end_anchor. The replaced range includes both anchors and the text between them, so the middle is not copied into the call; it ends at the first end_anchor match that begins at or after the start match in the same text. Multi-line plain text becomes new paragraphs; single-line plain text replaces the span inline and keeps the host paragraph and its style; a renamed heading's links follow it."},
            "end_anchor": {"type": "string", "description": "Literal substring marking the end of a range to replace; requires start_anchor, matching the first occurrence that begins at or after the start match in the same text and the middle is not copied."},
            "all_matches": {"type": "boolean", "default": False, "description": "Replace all occurrences (true) or first only. Only for target='search' with position='replace' and old_content."},
            "occurrence": {"type": "integer", "minimum": 0, "description": ("For target='search': 0-based index into replaceable Writer text matches (body/table/frame) or the start_anchor match (not dry_run shape/comment rows). Omit for the existing first-match behavior.")},
            "position": enum_param(
                ["replace", "before", "after"],
                (
                    "For target='search': 'replace' replaces the match; 'before'/'after' INSERT the content next to the match and leave the matched text untouched, so content must not repeat the matched text (result reports inserted=true instead of replaced_count). Inline content (plain text, <b>, <i>, math, ...) goes at the exact match edge; block content (<p>, headings, lists, tables) goes before/after the whole paragraph that holds the match (result reports snapped_to_paragraph=true when that moved it)."
                ),
                default="replace",
            ),
            "dry_run": {"type": "boolean", "description": "For target='search' with old_content: do NOT edit. Return replaceable matches (each tagged with occurrence) plus shape/comment previews, so you can check before committing."},
            "regex": {"type": "boolean", "default": False, "description": "For target='search': treat old_content as a regular expression (false = literal). Regex mode is single-paragraph (no cross-paragraph chaining). Patterns use ICU regex syntax (LibreOffice's engine): (?<name>...) for named groups, not (?P<name>...); \\p{Lu} classes are allowed. Floating drawing shapes are matched with Python re instead, so ICU-only syntax does not match inside shapes."},
            "case_sensitive": {"type": "boolean", "description": f"For target='search': {search_mod.LADDER_CASE_SENSITIVE_DESCRIPTION}"},
            "hyperlink_url": {"type": "string", "description": ("For target='search' with position='replace' when the match overlaps one heading link (an outline hyperlink, #Heading|outline): the exact URL to set on that link. Omitted, the link's URL follows the new text when the matched text appears once in it. content may equal old_content when only the URL is stale.")},
        },
        "required": ["content"],
    }
    uno_services: list[str] | None = ["com.sun.star.text.TextDocument"]
    tier: str = "core"
    is_mutation: bool | None = True

    @staticmethod
    def _position_choice(kwargs: Any) -> tuple[str, str | None]:
        position = str(kwargs.get("position") or "replace").strip().lower()
        if position not in ("replace", "before", "after"):
            return position, "position must be 'replace', 'before' or 'after'."
        return position, None

    @staticmethod
    def _override_not_replace(override: Any, position: str) -> str | None:
        if override and position != "replace":
            return "hyperlink_url only applies to position='replace'."
        return None

    def _invalid_regex(self, rex_err: str) -> dict[str, Any]:
        return self._tool_error(search_mod.invalid_regex_tool_message(rex_err), code="INVALID_REGEX", count=0)

    @staticmethod
    def _parse_occurrence(kwargs: Any, target: str) -> tuple[int | None, str | None]:
        """Validate optional 0-based search occurrence selector."""
        raw = kwargs.get("occurrence")
        if raw is None:
            return None, None
        if target != "search":
            return None, "occurrence only applies to target='search'."
        if kwargs.get("all_matches", False):
            return None, "occurrence cannot be combined with all_matches=true."
        return search_mod.parse_non_negative_int(raw, name="occurrence")

    @staticmethod
    def _parse_hyperlink_url(kwargs: Any) -> tuple[str | None, str | None]:
        """Explicit outline-target override. None means use substitution when it applies."""
        raw = kwargs.get("hyperlink_url")
        if not raw or not raw.strip():
            return None, None
        if kwargs.get("all_matches", False):
            return None, "hyperlink_url cannot be combined with all_matches=true."
        return raw, None

    @staticmethod
    def _content_text(content: Any) -> tuple[str, str | None]:
        """Normalize ``content`` (str, list[str], or serialized list) to one string, or ``("", error)``.

        A model sometimes sends the list as a JSON string; without the parse its brackets and commas
        become document text.
        """
        if isinstance(content, str):
            parsed = parse_html_fragment_list(content)
            if parsed is not None:
                content = parsed
        if isinstance(content, list):
            if any(not isinstance(x, str) for x in content):
                return "", "content list items must be strings."
            content = "\n".join(content)
        if not isinstance(content, str):
            return "", "content must be a string or a list of strings."
        return content, None

    @staticmethod
    def _replacement_plain(content: str) -> str:
        """Visible text a search replace will insert, for outline-URL substitution."""
        if html_import.content_has_markup(content):
            return html_import.plain_text_if_markup(content)
        return html_import_styles.expand_prose_escapes(content)

    def _read_outline_snapshot(self, found: Any, log_message: str) -> OutlineSnapshot:
        try:
            return capture_outline_hyperlinks(found)
        except Exception as exc:
            reraise_if_disposed(exc)
            log.exception(log_message)
            raise ToolExecutionError(
                "Could not read the outline hyperlink on this match.",
                code="HYPERLINK_UPDATE_FAILED",
            ) from exc

    def _outline_match_fields(self, found: Any, plain_fn: Any, override: str | None) -> dict[str, Any]:
        """dry_run fields for one match. Empty fields when the match has no outline link.

        A failed outline read or a rejected override raises; the dry-run branch of `_execute`
        turns it into the error payload. *plain_fn* is called only when a preview is
        actually needed, so a markup dry_run that does not touch an outline link does not open
        a hidden document. An override on a non-outline match is not previewed as a new link;
        the caller rejects it.
        """
        # Invariant: a failed outline read is HYPERLINK_UPDATE_FAILED (never "no link"); disposal propagates.
        snapshot = self._read_outline_snapshot(found, "apply_content: outline hyperlink preview failed")
        if not snapshot.links:
            if override:
                raise ToolExecutionError(_OVERRIDE_NEEDS_ONE_LINK, code="INVALID_PARAM")
            return {}
        plain = plain_fn()
        plans = plan_outline_updates(snapshot.links, snapshot.matched, plain, override)
        reports = public_hyperlink_reports(plans)
        if not reports:
            if override:
                raise ToolExecutionError(_OVERRIDE_NEEDS_ONE_LINK, code="INVALID_PARAM")
            return {}
        if override and len(reports) != 1:
            raise ToolExecutionError(_OVERRIDE_NEEDS_ONE_LINK, code="INVALID_PARAM")
        return hyperlink_fields(reports)

    def _outline_undo(self, doc: Any, session: EditReviewSession, run: Any) -> list[dict[str, Any]]:
        """Run *run* inside one undo step so the text replace and the URL write are one Ctrl+Z.

        The HTML path and a surgical review replace each close their own undo context
        before returning. Without this outer step the URL write would be a second undo.
        Raises ToolExecutionError when the URL write fails; the text change is rolled back first
        when possible.
        """
        reports_holder: list[list[dict[str, Any]]] = []

        def mutate() -> None:
            reports = run()
            if reports is not None:
                reports_holder.append(reports)

        try:
            outcome = commit_or_rollback(
                doc,
                next_agent_edit_undo_title(),
                mutate,
                session=session,
            )
        except UndoUnavailable as exc:
            raise ToolExecutionError(
                "Cannot update the outline hyperlink atomically (no usable undo context); "
                "refusing rather than risk a text change that leaves the link stale.",
                code="UNDO_UNAVAILABLE",
            ) from exc
        if outcome.mutate_error is not None:
            mutate_err = outcome.mutate_error
            reraise_if_disposed(mutate_err)
            if isinstance(mutate_err, ToolExecutionError) and mutate_err.code == "TABLE_INSERT_FAILED":
                raise mutate_err
            log.exception("apply_content: outline hyperlink update failed")
            if outcome.nothing_landed:
                msg = f"Outline hyperlink update failed; the text change was rolled back ({mutate_err})."
            else:
                msg = f"Outline hyperlink update failed; the text change could not be rolled back and may still be in the document ({mutate_err})."
            raise ToolExecutionError(msg, code="HYPERLINK_UPDATE_FAILED") from mutate_err

        err = outcome.error
        if err is not None and err.get("code") == "ROLLBACK_FAILED":
            msg = f"Outline hyperlink update failed; the text change could not be rolled back and may still be in the document ({err.get('message')})."
            raise ToolExecutionError(msg, code="HYPERLINK_UPDATE_FAILED")

        return reports_holder[0] if reports_holder else []

    def _finish_search_miss(self, doc: Any, params: EditParams) -> dict[str, Any]:
        shape_cs = search_mod.resolve_case_sensitive(params.case_sensitive)
        shape = search_mod.drawing_shape_object_containing(
            doc, params.pattern,
            use_regex=params.regex, case_sensitive=shape_cs)
        # Review mode cannot track a shape edit, and a failed shape replace is rolled back.
        # Both fall through to build_search_not_found_response, whose sweep finds the same
        # shape and returns the delegation (review wording via review=).
        if shape is not None and not params.track_reviewable:
            shape_name = search_mod.shape_display_name(shape)
            # commit_or_rollback wraps shape replacement so a failed replace_text_in_shape
            # rolls back instead of leaving a partial setString in the document.
            def mutate() -> dict[str, Any] | None:
                edited = replace_text_in_shape(
                    shape, params.pattern, params.plain, case_sensitive=shape_cs, use_regex=params.regex
                )
                if not edited:
                    return {"status": "error", "code": "SHAPE_REPLACE_FAILED"}
                return None

            try:
                outcome = commit_or_rollback(
                    doc,
                    next_agent_edit_undo_title(),
                    mutate,
                )
                err = outcome.error
                if outcome.mutate_error is not None:
                    exc = outcome.mutate_error
                    return self._tool_error(
                        f"Failed to update shape text: {exc}",
                        code="TOOL_EXECUTION_ERROR",
                    )
                if err is not None and err.get("code") == "ROLLBACK_FAILED":
                    return err
                if err is not None and err.get("code") == "SHAPE_REPLACE_FAILED":
                    pass  # Failed shape replacement rolled back; fall through to delegation response below
                else:
                    result = attach_edited_context(
                        {"status": "ok",
                          "message": ("Replaced 1 occurrence inside drawing shape '%s' (edited the "
                                      "shape's own text directly — NOT a tracked change; surrounding "
                                      "formatting preserved)." % shape_name),
                          "replaced_count": 1, "review_bypassed": True},
                        None)
                    return result
            except UndoUnavailable:
                return self._tool_error(
                    "Cannot edit text inside the drawing shape '%s' atomically "
                    "(no usable undo context); refusing rather than leave an edit "
                    "that is not one Ctrl+Z." % shape_name,
                    code="UNDO_UNAVAILABLE",
                )

        return search_mod.build_search_not_found_response(
            doc,
            params.pattern,
            case_sensitive=shape_cs,
            use_regex=params.regex,
            all_matches=bool(params.all_matches),
            lenient=not params.regex and params.case_sensitive is None,
            refuse_oversized=True,
            review=params.track_reviewable,
        )

    def _replace_found(self, session: EditReviewSession, doc: Any, found: Any, *,
                       use_preserve: bool, content: Any,
                       ctx: ToolContext, track_reviewable: bool,
                       override: str | None, batch: bool,
                       plain: str,
                       report: html_import_styles.WriteReport,
                       fix_outline: bool = True,
                       has_table: bool = False) -> list[dict[str, Any]]:
        """Replace one search match and, when it sits in an outline link, fix that URL.

        *batch* is the all_matches loop, which already holds the undo context. A single
        match opens one when a URL write is needed. *has_table* verifies that a table
        import added a table inside the undo group. Returns the hyperlink reports;
        failures raise ToolExecutionError with the code the caller publishes.
        """
        # Anchor spans skip this because their text never equals one link's title;
        # renamed headings are handled separately below.
        anchor = None
        snapshot = None
        needs_fix = False
        if fix_outline:
            anchor = collapsed_anchor(found)
            # Invariant: a failed capture refuses the edit (HYPERLINK_UPDATE_FAILED) before any text change; disposal propagates.
            snapshot = self._read_outline_snapshot(found, "apply_content: outline hyperlink capture failed")
            if override and len(snapshot.links) != 1:
                # Before the text edit. One override on two outline links would stamp
                # both with the same target. Zero links would paint a bookmark or plain text.
                raise ToolExecutionError(_OVERRIDE_NEEDS_ONE_LINK, code="INVALID_PARAM")
            # preserve_url: a bookmark target covering the match. An untracked
            # preserve-format replace keeps HyperLinkURL except on a one-character
            # run; HTML replaces and recorded replaces can clear it on the new
            # characters, so put that target back unchanged.
            needs_fix = bool(snapshot.links) or bool(snapshot.preserve_url)

        headings = None if batch else headings_if_touched(doc, [found])

        def run() -> list[dict[str, Any]]:
            original = found.getString()
            if use_preserve:
                record_preserve_replace(session, doc, found, content, ctx.ctx, track_reviewable)
            elif batch:
                # The all_matches loop already entered an undo context. A second
                # record_html_atomically would nest another and close it early.
                def replace_one(f: Any = found) -> None:
                    rep = html_import.replace_single_range_with_content(
                        doc, f, content, ctx.ctx)
                    if has_table:
                        html_import.raise_if_no_table_added(rep.tables_added)
                    report.extend(rep)

                session.record_mutation(
                    replace_one,
                    original_preview=original, proposed_preview=plain)
            else:
                def replace_single() -> None:
                    rep = html_import.replace_single_range_with_content(
                        doc, found, content, ctx.ctx)
                    if has_table:
                        html_import.raise_if_no_table_added(rep.tables_added)
                    report.extend(rep)

                record_html_atomically(
                    session, doc,
                    replace_single,
                    original_preview=original, proposed_preview=plain)
            reports = (
                restore_links(doc, anchor, snapshot, plain, override)
                if needs_fix and snapshot is not None
                else []
            )
            report.links_retargeted, report.stale_links = retarget_renamed_links(session, doc, headings)
            return reports

        if batch:
            return run()
        # A single match has to enter the review session here, outline link or not
        # (all_matches already entered it around the loop), or record mode would
        # only track replaces that happen to sit in an outline URL.
        if not needs_fix:
            if headings is None:
                with session:
                    return run()
            with UndoStepOrRollback(doc, next_agent_edit_undo_title()), session:
                return run()

        def once() -> list[dict[str, Any]]:
            with session:
                return run()

        return self._outline_undo(doc, session, once)

    def _occurrence_oor(self, occurrence: int, count: int, **details: Any) -> dict[str, Any]:
        # LLMs guess 1-based; say the valid 0-based range like apply_style.
        return self._tool_error(
            "occurrence %s out of range (found %d match(es), use 0..%d)." % (
                occurrence, count, count - 1),
            code="OCCURRENCE_OUT_OF_RANGE",
            count=count,
            **details,
        )

    def _review_wait_seconds(self) -> int:
        """Max seconds the edit call should block waiting for review; 0 = don't wait."""
        try:
            return edit_review_wait_seconds()
        except Exception:
            log.exception("apply_content: could not read review wait seconds")
            return 0

    def _wait_enabled_globally(self) -> bool:
        """Config read without a tool context, for long_running/is_async (called by the
        MCP/chat shells before execute)."""
        return self._review_wait_seconds() > 0

    @property
    def long_running(self) -> bool:  # type: ignore[override]  # ty: ignore[invalid-property-type-override]  # pyright: ignore[reportIncompatibleVariableOverride]
        # With review-wait on, MCP must run this call on its HTTP thread so the wait can
        # block there (one request, one response -- the response just comes back after the
        # user reviews). With it off, stay a normal synchronous main-thread tool.
        return self._wait_enabled_globally()

    def is_async(self) -> bool:
        # When review-wait is on, the chat worker / MCP HTTP thread hosts this call (the
        # main-thread guard in execute_safe is bypassed) and every document touch is
        # marshalled via main.call.
        if self._wait_enabled_globally():
            return True
        # Also stay "async" whenever we're ALREADY on a background thread: the chat loop
        # snapshots the async-tool set once per round and MCP reads long_running per call, so
        # review can be toggled OFF between that decision and now. If it was, we're running on a
        # worker thread with the live flag False -- returning True keeps execute_safe's
        # main-thread guard from rejecting us; execute() then runs the (now wait-free) edit on
        # the main thread via marshalling, so the toggle is handled safely instead of erroring.
        return not main.is_current()

    @staticmethod
    def _effective_target(
        target: Any,
        old_content: Any,
        start_anchor: Any = None,
        end_anchor: Any = None,
    ) -> Any:
        if not target and (old_content is not None or start_anchor is not None or end_anchor is not None):
            return "search"
        return target

    def _dry_run_preview(self, ctx: ToolContext, **kwargs: Any) -> dict[str, Any]:
        """Resolve old_content matches WITHOUT editing, for target='search'. Reports the count and
        each match's location + a short snippet, so the model can check before committing."""
        if kwargs.get("start_anchor") is not None or kwargs.get("end_anchor") is not None:
            rejected = self._reject_unsupported_with_anchors(kwargs)
            assert rejected is not None
            return rejected
        old_content = kwargs.get("old_content")
        target = self._effective_target(
            kwargs.get("target"),
            old_content,
            kwargs.get("start_anchor"),
            kwargs.get("end_anchor"),
        )
        if target != "search" or old_content is None:
            return self._tool_error("dry_run only applies to target='search' with old_content.", code="INVALID_PARAM")
        occurrence, occurrence_error = self._parse_occurrence(kwargs, target)
        if occurrence_error:
            return self._tool_error(occurrence_error, code="INVALID_PARAM")
        override, override_error = self._parse_hyperlink_url(kwargs)
        if override_error:
            return self._tool_error(override_error, code="INVALID_PARAM")
        # Same position guard as execute. before/after insert beside the match and
        # leave its outline URL alone, so a preview must not say the target will change.
        position, pos_err = self._position_choice(kwargs)
        if pos_err:
            return self._tool_error(pos_err, code="INVALID_PARAM")
        override_err = self._override_not_replace(override, position)
        if override_err:
            return self._tool_error(override_err, code="INVALID_PARAM")
        content, content_err = self._content_text(kwargs.get("content", ""))
        if content_err:
            return self._tool_error(content_err, code="INVALID_PARAM")
        # Empty replacements delete the match without painting a put-back URL.
        rewrites_outline = position == "replace" and content.strip() != ""
        edit_idx = 0 if occurrence is None else occurrence

        old_stripped = str(old_content).strip() if old_content is not None else ""
        s = search_mod.prepare_old_content(old_content)
        if not s:
            return self._tool_error("old_content is empty after normalization.", code="INVALID_PARAM")
        regex, cs, pattern, rex_err = _search_options(ctx, kwargs, old_stripped, s)
        if rex_err:
            return self._invalid_regex(rex_err)
        try:
            ranges, mode = _find_ranges(
                ctx.doc, pattern, regex, cs, limit=search_mod.MAX_SEARCH_REPLACEMENTS
            )
        except Exception as e:
            log.exception("apply_content dry_run search failed")
            return self._tool_error("dry_run search failed: %s" % e, code="SEARCH_FAILED")
        labeler = search_mod.LocationLabeler(ctx.doc)
        replaceable: list[dict[str, Any]] = []

        @functools.cache
        def _plain() -> str:
            return self._replacement_plain(content)

        def _match_snippet(found: Any) -> tuple[str, str]:
            try:
                loc = labeler.describe(found)
            except Exception:
                loc = "body"
            try:
                text = (found.getString() or "")[:160]
            except Exception:
                text = ""
            return loc, text

        # occurrence is the index into the same body, table, and frame ranges the
        # edit path uses. Shape and comment previews sit in not_replaceable, so a
        # position in that list is not an occurrence.
        for idx, found in enumerate(ranges[:search_mod.DEFAULT_SEARCH_RESULTS]):
            loc, text = _match_snippet(found)
            row = {"occurrence": idx, "location": loc, "text": text}
            if rewrites_outline:
                # The override is one destination for the match execute would edit.
                # Other rows show automatic substitution only.
                row_override = override if idx == edit_idx else None
                row.update(self._outline_match_fields(found, _plain, row_override))
            replaceable.append(row)
        shape_comment_warning = search_mod.shape_sweep_skip_reason(pattern, regex)
        if shape_comment_warning is not None:
            shape_hits = []
            comment_hits = []
        else:
            shape_hits = search_mod.sweep_draw_shape_preview_matches(
                ctx.doc, pattern, regex, mode.case_sensitive, limit=search_mod.MAX_SEARCH_HITS
            )
            comment_hits = search_mod.sweep_comment_preview_matches(
                ctx.doc, pattern, regex, mode.case_sensitive, limit=search_mod.MAX_SEARCH_HITS
            )
        not_replaceable: list[dict[str, Any]] = []
        for item in shape_hits + comment_hits:
            if len(replaceable) + len(not_replaceable) >= search_mod.DEFAULT_SEARCH_RESULTS:
                break
            not_replaceable.append({"location": item["location"], "text": item["text"][:160]})
        total = len(ranges) + len(shape_hits) + len(comment_hits)
        if occurrence is not None and ranges and occurrence >= len(ranges):
            oor = self._occurrence_oor(
                occurrence, len(ranges),
                replaceable_count=len(ranges),
                replaceable=replaceable,
                not_replaceable=not_replaceable,
            )
            oor.update(mode.result_fields())
            return oor
        result: dict[str, object] = {
            "status": "ok",
            "dry_run": True,
            "count": total,
            "replaceable_count": len(ranges),
            "replaceable": replaceable,
            "not_replaceable": not_replaceable,
            "edit_reach_note": (
                "count includes body, table, frame, shapes, and comments. replaceable is the edit-path preview. "
                "replaceable_count is the full index space and can exceed the preview. not_replaceable is the "
                "shape and comment preview. search uses the same split. Only floating shapes are "
                "editable in review-off mode or via the shapes toolset."
            ),
        }
        result.update(mode.result_fields())
        if shape_comment_warning is not None:
            result["warning"] = shape_comment_warning
        if not ranges:
            res = search_mod.build_search_not_found_response(
                ctx.doc,
                pattern,
                case_sensitive=mode.case_sensitive,
                use_regex=regex,
                lenient=not regex and cs is None,
                refuse_oversized=True,
            )
            result.update({k: res[k] for k in ("message", "closest_match", "diff_at") if k in res})
        result["normalized_old_content"] = s
        if occurrence is not None and ranges:
            selected = ranges[occurrence]
            selected_location, selected_text = _match_snippet(selected)
            result["selected_occurrence"] = occurrence
            selected_match = {
                "location": selected_location,
                "text": selected_text,
            }
            if rewrites_outline:
                selected_match.update(self._outline_match_fields(selected, _plain, override))
            result["selected_match"] = selected_match
        return result

    def _release_review_session(self, session: Any) -> None:
        try:
            _run_on_main_thread(session.cleanup)
        except Exception:
            log.exception("apply_content: review session cleanup failed")

    def _resolve_heading_links(
        self, doc: Any, params: EditParams
    ) -> tuple[EditParams, list[Diagnostic]]:
        """Resolve bare heading links in written HTML against document headings."""
        if not html_import_styles.has_bare_fragment_links(params.content):
            return params, []

        doc_headings: list[str] = (
            []
            if params.target == "full_document"
            else [text for _level, text in document_headings(doc)]
        )

        def is_bookmark(name: str) -> bool:
            return bool(uno_read(lambda: doc.getBookmarks().hasByName(name), False))

        new_content, diags = html_import_styles.resolve_heading_links(
            params.content,
            doc_headings,
            is_bookmark=is_bookmark,
        )
        return replace(params, content=new_content), diags

    def execute(self, ctx: ToolContext, **kwargs: Any) -> dict[str, Any]:
        result = self._execute(ctx, **kwargs)
        if self._effective_target(kwargs.get("target"), kwargs.get("old_content"), kwargs.get("start_anchor"), kwargs.get("end_anchor")) == "search":
            result.setdefault("matched_case_sensitive", search_mod.resolve_case_sensitive(kwargs.get("case_sensitive")))
        if kwargs.get("target") == "search" and "normalized_old_content" not in result:
            result["normalized_old_content"] = search_mod.prepare_old_content(kwargs.get("old_content"))
        return result

    def _execute(self, ctx: ToolContext, **kwargs: Any) -> dict[str, Any]:
        on_main = main.is_current()

        if kwargs.get("dry_run"):
            # The preview reads ranges and text through UNO, so a worker-thread call
            # (tool-sync-apply_content) marshals it like the edit path does.
            # The preview's outline helpers raise like the edit path; this is the one
            # place the dry-run turns them into the payload.
            try:
                return _run_on_main_thread(lambda: self._dry_run_preview(ctx, **kwargs))
            except ToolExecutionError as e:
                return self._tool_error(str(e), code=e.code)
        wait_seconds = self._review_wait_seconds()
        session_box: list[Any] = []

        def _do_edit() -> tuple[dict[str, Any], Any, bool]:
            parsed = self._parse_edit_params(ctx, kwargs)
            if isinstance(parsed, dict):
                return parsed, None, False
            parsed, diags = self._resolve_heading_links(ctx.doc, parsed)
            session = EditReviewSession(ctx.doc, ctx.ctx, enabled=parsed.track_reviewable)
            session_box.append(session)
            result = self._dispatch_edit(ctx, session, parsed)
            result = append_warnings(result, diags)
            pending = result.get("status") == "ok" and session.left_tracked_changes()
            return result, session, pending

        if get_agent_edit_review_mode() != "wait" or on_main:
            # No review-wait: review is off, it was toggled off after this call was dispatched
            # to a worker thread, or we ARE the main thread (where blocking would freeze the UI
            # and the user could never click accept/reject). Edit once, don't wait -- but UNO is
            # not thread-safe, so when we're on a worker thread (the toggled-off case) the edit
            # and its cleanup run on the main thread via marshalling rather than here.
            # The session is appended to session_box in _do_edit before dispatch, so anchor
            # bookmarks are released in `finally` even if the edit raises mid-way (e.g. the 2nd
            # of 3 replace-all matches fails after the 1st).
            try:
                result, _session, pending = _run_on_main_thread(_do_edit)
                return annotate_review_status(result) if pending else result
            except ToolExecutionError as e:
                return self._tool_error(str(e), code=getattr(e, "code", "TOOL_EXECUTION_ERROR"))
            finally:
                if session_box:
                    self._release_review_session(session_box[0])

        # Review-wait path, on a background (MCP HTTP / chat worker) thread: run the edit
        # on the main thread, then block HERE until the user reviews the tracked changes.
        try:
            # 60s edit budget: matches the synchronous MCP path's processing timeout; the
            # default 30s marshalling timeout would reject large full-document replaces that
            # are fine without review mode.
            result, session, pending = _run_on_main_thread(_do_edit)
        except Exception as e:
            if session_box:
                self._release_review_session(session_box[0])
            if isinstance(e, ToolExecutionError):
                return self._tool_error(str(e), code=getattr(e, "code", "TOOL_EXECUTION_ERROR"))
            raise
        if session is None or not session.changes or result.get("status") != "ok":
            if session is not None:
                self._release_review_session(session)
            return annotate_review_status(result) if pending else result

        # In-app chat: surface a sidebar status while we block (MCP has no sidebar -> None, no-op).
        # The callback marshals onto the chat drain queue, so it is safe from this worker thread.
        status_cb = ctx.status_callback
        if callable(status_cb):
            try:
                status_cb("Review the agent's changes in the document — accept or reject the tracked changes.")
            except Exception:
                log.debug("apply_content: status_callback failed", exc_info=True)

        # Stop comes from ctx.send_cancellation; ToolContext has no stop_checker. MCP has no scope (None).
        # still_reviewing: review mode toggled off aborts the wait; that is not a user Stop.
        scope = ctx.send_cancellation

        review = session.wait_for_review(
            timeout=wait_seconds,
            uno_runner=main.call,
            cancel=scope,
            still_reviewing=lambda: get_agent_edit_review_mode() == "wait",
        )
        result = dict(result)
        result["review"] = review
        if not review.get("complete"):
            result["message"] = (result.get("message") or "") + (
                " The user has not finished reviewing these tracked changes; ask them to"
                " accept or reject the changes in the document, then continue."
            )
        return result

    def _reject_unsupported_with_anchors(self, kwargs: Any) -> dict[str, Any] | None:
        """Reject parameters that are incompatible with start_anchor/end_anchor range replacement."""
        if kwargs.get("regex"):
            return self._tool_error("regex does not apply to start_anchor and end_anchor.", code="INVALID_PARAM")
        if kwargs.get("all_matches"):
            return self._tool_error("all_matches cannot be combined with start_anchor and end_anchor.", code="INVALID_PARAM")
        if kwargs.get("dry_run"):
            return self._tool_error("dry_run does not apply to start_anchor and end_anchor.", code="INVALID_PARAM")
        if kwargs.get("position") in ("before", "after"):
            return self._tool_error("position='before'/'after' does not apply to an anchor range.", code="INVALID_PARAM")
        if kwargs.get("hyperlink_url") is not None:
            return self._tool_error("hyperlink_url does not apply to start_anchor and end_anchor.", code="INVALID_PARAM")
        return None

    def _parse_anchor_params(
        self, kwargs: Any, start_anchor: Any, end_anchor: Any
    ) -> AnchorSpec | dict[str, Any]:
        """Validate and normalize start_anchor and end_anchor; returns AnchorSpec or error dict."""
        unsupported = self._reject_unsupported_with_anchors(kwargs)
        if unsupported is not None:
            return unsupported

        if start_anchor is None or end_anchor is None:
            return self._tool_error("start_anchor and end_anchor are both required.", code="INVALID_PARAM")
        if not isinstance(start_anchor, str) or not isinstance(end_anchor, str):
            return self._tool_error("start_anchor and end_anchor must both be strings.", code="INVALID_PARAM")
        norm_start = search_mod.normalize_search_string_for_find(html_import.plain_text_if_markup(start_anchor))
        if not norm_start:
            return self._tool_error("start_anchor is empty after normalization.", code="INVALID_PARAM")
        norm_end = search_mod.normalize_search_string_for_find(html_import.plain_text_if_markup(end_anchor))
        if not norm_end:
            return self._tool_error("end_anchor is empty after normalization.", code="INVALID_PARAM")
        cs = search_mod.explicit_case_sensitive(kwargs.get("case_sensitive"))
        return AnchorSpec(start=norm_start, end=norm_end, case_sensitive=cs)

    def _parse_edit_params(self, ctx: ToolContext, kwargs: Any) -> EditParams | dict[str, Any]:
        content = kwargs.get("content", "")
        old_content = kwargs.get("old_content")
        start_anchor = kwargs.get("start_anchor")
        end_anchor = kwargs.get("end_anchor")
        has_anchors = start_anchor is not None or end_anchor is not None

        if old_content is not None and has_anchors:
            return self._tool_error("Pass old_content or start_anchor and end_anchor, not both.", code="INVALID_PARAM")

        target = self._effective_target(kwargs.get("target"), old_content, start_anchor, end_anchor)
        if not target:
            return self._tool_error(
                "Provide a target ('beginning', 'end', 'selection', 'full_document', 'search') "
                "or old_content for find-and-replace.",
                code="INVALID_PARAM")

        if target != "search" and has_anchors:
            return self._tool_error("start_anchor and end_anchor require target='search'.", code="INVALID_PARAM")

        if target == "search" and old_content is None and not has_anchors:
            return self._tool_error("target='search' requires old_content or start_anchor and end_anchor.", code="INVALID_PARAM")

        anchor: AnchorSpec | None = None
        if has_anchors:
            anchor_res = self._parse_anchor_params(kwargs, start_anchor, end_anchor)
            if isinstance(anchor_res, dict):
                return anchor_res
            anchor = anchor_res

        occurrence, occurrence_error = self._parse_occurrence(kwargs, target)
        if occurrence_error:
            return self._tool_error(occurrence_error, code="INVALID_PARAM")

        # position is a search-only refinement; validate it up front (silently ignoring it on an
        # insert target would teach the model a parameter that "works" by accident).
        position, pos_err = self._position_choice(kwargs)
        if pos_err:
            return self._tool_error(pos_err, code="INVALID_PARAM")
        if position != "replace" and target != "search":
            return self._tool_error(
                "position='before'/'after' requires target='search' "
                "(it inserts next to an old_content match).",
                code="INVALID_PARAM")
        if position != "replace" and kwargs.get("all_matches", False):
            return self._tool_error(
                "position='before'/'after' inserts at a single match; drop all_matches "
                "or use position='replace'.",
                code="INVALID_PARAM")

        content, content_err = self._content_text(content)
        if content_err:
            return self._tool_error(content_err, code="INVALID_PARAM")

        if target in ("beginning", "end", "selection") and content.strip() == "":
            return self._tool_error("content is empty; nothing was inserted.", code="INVALID_PARAM")

        # Plain anchor text with newlines is imported as new paragraphs so existing character-level
        # styles and headings across the span do not bleed into the new text. Single-line plain
        # anchor text remains unwrapped so mid-paragraph edits stay on the inline path without splitting
        # the host paragraph. Entities are left intact here because the HTML parser decodes them during import.
        use_preserve = (not html_import.content_needs_html_import(content)) and anchor is None
        decoded = ""

        if not html_import.content_needs_html_import(content):
            content = html_import_styles.expand_escapes(content)
            if anchor is not None and not html_import.html_fragment_contains_mixed_math(content):
                if "\n" in content:
                    lines = [line for line in content.split("\n") if line.strip()]
                    content = "\n".join("<p>%s</p>" % line for line in lines)
            elif use_preserve:
                # Decode complete references once for plain text: literal search-replace
                # writes via setString (no HTML parser), while other targets go through
                # html_import where < > & " ' must stay escaped so they are not parsed as markup.
                decoded = html_import_styles.decode_complete_character_references(content)
                if target == "search" and position == "replace":
                    content = decoded
                else:
                    content = html_import_styles.normalize_character_references(content)

        # Opt-in review mode: when doc.agent_edit_review_mode is record/wait, EditReviewSession records
        # the agent's edits as native tracked changes (redlines) the user can accept/reject --
        # tagging each logical change so its outcome can be reported -- and restores the prior
        # recording state. Default off -> the session is inert and behavior is unchanged.
        # review_recording_enabled tolerates the flag not being registered yet (returns False).
        track_reviewable = review_recording_enabled()

        old_stripped = ""
        search_string = ""
        pattern = ""
        regex = False
        cs = anchor.case_sensitive if anchor is not None else None
        separator = 0
        override: str | None = None

        if target == "search" and not has_anchors:
            old_stripped = str(old_content).strip() if old_content is not None else ""
            search_string = search_mod.prepare_old_content(old_content)
            if not search_string:
                # Parameter error (like old_content=None), not a search no-op: the search never ran,
                # so there's no replaced_count to report — use the standard tool error shape.
                return self._tool_error("old_content is empty after normalization.", code="INVALID_PARAM")

        plain = decoded if use_preserve else self._replacement_plain(content)
        has_table = not use_preserve and bool(re.search(r"<table\b", str(content), re.IGNORECASE))
        all_matches = bool(kwargs.get("all_matches", False))

        if target == "search" and not has_anchors:
            # The search drops the spaces at old_content's edges. The replacement
            # must drop the same edges, or old_content=" paragrafo" with content=""
            # deletes only "paragrafo" and leaves "Primeiro  inteiro." with a
            # double space (same for " paragrafo inteiro" -> " inteiro"). A
            # deletion takes the space on one side along with the word.
            # Invariant: branch on use_preserve, not on the decoded content (an entity may decode to markup).
            if (position == "replace" and not kwargs.get("regex")
                    and use_preserve
                    and not html_import.content_has_markup(str(old_content))):
                content, separator = _match_stripped_edges(str(old_content), content)
                plain = content

            regex, cs, pattern, rex_err = _search_options(ctx, kwargs, old_stripped, search_string)
            if rex_err:
                return self._invalid_regex(rex_err)

            override, override_error = self._parse_hyperlink_url(kwargs)
            if override_error:
                return self._tool_error(override_error, code="INVALID_PARAM")
            override_err = self._override_not_replace(override, position)
            if override_err:
                return self._tool_error(override_err, code="INVALID_PARAM")

        return EditParams(
            content=content,
            plain=plain,
            use_preserve=use_preserve,
            has_table=has_table,
            target=target,
            position=position,
            occurrence=occurrence,
            all_matches=all_matches,
            track_reviewable=track_reviewable,
            search_string=search_string,
            pattern=pattern,
            regex=regex,
            case_sensitive=cs,
            separator=separator,
            override=override,
            anchor=anchor,
        )

    def _dispatch_edit(
        self, ctx: ToolContext, session: EditReviewSession, params: EditParams,
    ) -> dict[str, Any]:
        if params.target == "full_document":
            return self._edit_full_document(ctx, session, params)
        if params.target in ("end", "beginning"):
            return self._edit_insert_at(ctx, session, params)
        if params.target == "selection":
            return self._edit_selection(ctx, session, params)
        res = self._edit_search(ctx, session, params)
        res["normalized_old_content"] = params.search_string
        return res

    def _edit_full_document(
        self, ctx: ToolContext, session: EditReviewSession, params: EditParams,
    ) -> dict[str, Any]:
        html_import.raise_if_hidden_section(ctx.doc)
        html_import.raise_if_protected_sections(ctx.doc)
        content = params.content
        plain = params.plain
        has_table = params.has_table
        # Capture data-less image placeholders BEFORE clear_first deletes leading
        # tables: with review off (or own pending-insertion tables), deleting the
        # table disposes its pictures so they can no longer be read.
        captured = image_placeholders.capture_placeholder_images(ctx.doc, content)
        headings = document_headings(ctx.doc)
        # Outer UndoStepOrRollback joins the link rewrite to the import as one Ctrl+Z.
        with UndoStepOrRollback(ctx.doc, next_agent_edit_undo_title()), session:
            # Delete-then-import inside one undo group: a failed import or a missing
            # table rolls the cleared document back.
            def mutate() -> html_import_styles.WriteReport:
                rep = html_import.replace_full_document(
                    ctx.doc, ctx.ctx, content, kept_images=captured
                )
                if has_table:
                    html_import.raise_if_no_table_added(rep.tables_added)
                return rep

            report = record_html_atomically(
                session,
                ctx.doc,
                mutate,
                clear_first=lambda: delete_leading_tables(session, ctx.doc, ctx.ctx),
                proposed_preview=plain,
            )
            report.links_retargeted, report.stale_links = retarget_renamed_links(session, ctx.doc, headings)
        summary = html_import_styles.summarize_written_blocks(content)
        res: dict[str, Any] = {
            "status": "ok",
            "message": "Replaced entire document.",
            "paragraph_count": summary["paragraph_count"],
            "headings": summary["headings"],
        }
        return html_import_styles.attach_write_report(res, report)

    def _edit_insert_at(
        self, ctx: ToolContext, session: EditReviewSession, params: EditParams,
    ) -> dict[str, Any]:
        content = params.content
        plain = params.plain
        has_table = params.has_table
        target = params.target
        # The insert can land at the end of a selection at the document edge; Writer
        # stretches the view selection over text inserted at its end, so put it back.
        with KeepViewSelection(uno_read(lambda: ctx.doc.getCurrentController(), None)), session:
            def mutate() -> html_import_styles.WriteReport:
                rep = html_import.insert_content_at_position(
                    ctx.doc, ctx.ctx, content, target
                )
                if has_table:
                    html_import.raise_if_no_table_added(rep.tables_added)
                return rep

            report = record_html_atomically(
                session, ctx.doc, mutate, proposed_preview=plain)
        if target == "end":
            msg = "Inserted content at end."
            anchor = collapsed_anchor(ctx.doc.getText().getEnd())
        else:
            msg = "Inserted content at beginning."
            anchor = collapsed_anchor(ctx.doc.getText().getStart())
        res = attach_edited_context(
            {"status": "ok", "message": msg}, anchor)
        return html_import_styles.attach_write_report(res, report)

    def _edit_selection(
        self, ctx: ToolContext, session: EditReviewSession, params: EditParams,
    ) -> dict[str, Any]:
        content = params.content
        plain = params.plain
        has_table = params.has_table
        # Anchor BEFORE the edit: the selection insert clears the selection first, so only a
        # collapsed position (not the selection range itself) survives the replace.
        rng = request_selection_range(ctx)
        anchor = collapsed_anchor(rng) if rng is not None else None
        headings = headings_if_touched(ctx.doc, [rng]) if rng is not None else None
        with (UndoStepOrRollback(ctx.doc, next_agent_edit_undo_title()) if headings is not None else contextlib.nullcontext()), session:
            # Selection insert clears the selection first, then imports -> atomic (full_document above).
            def mutate() -> html_import_styles.WriteReport:
                rep = html_import.insert_content_at_position(
                    ctx.doc, ctx.ctx, content, "selection", selection=rng
                )
                if has_table:
                    html_import.raise_if_no_table_added(rep.tables_added)
                return rep

            report = record_html_atomically(
                session, ctx.doc, mutate, proposed_preview=plain)
            report.links_retargeted, report.stale_links = retarget_renamed_links(session, ctx.doc, headings)
        res: dict[str, Any] = {"status": "ok", "message": "Inserted content at selection."}
        if report.snapped_to_paragraph:
            res["message"] = (
                "Inserted content after the paragraph that holds the caret "
                "(block content cannot go mid-paragraph without splitting it)."
            )
            res["snapped_to_paragraph"] = True
        res = attach_edited_context(res, anchor)
        return html_import_styles.attach_write_report(
            res, report,
            suggest_style_fix=functools.partial(_partial_match_style_suggestion, ctx.doc, params))


    def _edit_replace_all(
        self, ctx: ToolContext, session: EditReviewSession, params: EditParams, ranges: list[Any],
    ) -> dict[str, Any]:
        doc = ctx.doc
        # replaced_count is the machine-readable success signal: 0 -> status "error" (a silent
        # no-op surfaced as a failure), N>0 -> "ok". No matched_count/warning/partial-replace:
        # if a replace raises mid-all_matches the existing abort behavior stands.
        max_limit_hit = len(ranges) >= search_mod.MAX_SEARCH_REPLACEMENTS
        # Decide before any replace. An empty replacement that is the last text
        # in a table deletes the table instead of leaving an empty shell; those
        # matches are not also cleared as text (that would add a second redline).
        doomed = writer_tables_emptied_by_matches(ranges, params.content)
        doomed_names = {name for _table, name in doomed}
        if doomed_names:
            anchor_range = next((item for item in ranges if _table_and_name(item)[1] not in doomed_names), None)
            anchor = collapsed_anchor(anchor_range) if anchor_range is not None else None
        else:
            anchor = collapsed_anchor(ranges[0])
        count = 0
        report = html_import_styles.WriteReport()
        link_reports: list[dict[str, Any]] = []
        tracked_delete_box = [False]
        headings = headings_if_touched(doc, ranges)

        def mutate() -> None:
            nonlocal count
            with session:
                for found in reversed(ranges):
                    if doomed_names and _table_and_name(found)[1] in doomed_names:
                        continue
                    found = _grow_over_edge_space(found, params.separator)
                    # batch=True: this loop already holds the undo context, so the
                    # outline URL write stays in the same Ctrl+Z as the text.
                    reports = self._replace_found(
                        session, doc, found, use_preserve=params.use_preserve,
                        content=params.content, ctx=ctx,
                        track_reviewable=params.track_reviewable, override=params.override, batch=True,
                        plain=params.plain, report=report, has_table=params.has_table)
                    if reports:
                        link_reports.extend(reports)
                    count += 1
                if doomed:
                    tracked_delete_box[0] = record_table_deletions(session, doc, ctx, doomed)
                report.links_retargeted, report.stale_links = retarget_renamed_links(session, doc, headings)

        try:
            outcome = commit_or_rollback(
                doc,
                next_agent_edit_undo_title(),
                mutate,
                session=session,
            )
        except UndoUnavailable:
            return self._tool_error(
                "Cannot apply all_matches atomically (no usable undo context); "
                "refusing rather than risk a half-applied edit.",
                code="UNDO_UNAVAILABLE")
        if outcome.mutate_error is not None:
            e = outcome.mutate_error
            log.exception("apply_content all_matches failed mid-batch")
            payload: dict[str, Any]
            if isinstance(e, ToolExecutionError):
                payload = format_error_payload(e)
            else:
                payload = {"status": "error", "code": "TOOL_EXECUTION_ERROR"}
            if outcome.nothing_landed:
                payload["message"] = "all_matches aborted after %d replacement(s); document rolled back (%s)." % (count, e)
            else:
                payload["message"] = "all_matches aborted after %d replacement(s); the document could not be rolled back and may be partially edited (%s)." % (count, e)
                payload["code"] = "ROLLBACK_FAILED"
            payload["replaced_count"] = 0
            payload["partial_failure"] = True
            payload["attempted"] = count
            return payload

        err = outcome.error
        if err is not None and err.get("code") == "ROLLBACK_FAILED":
            cause_msg = err.get("message", "unknown error")
            msg = (
                "all_matches aborted after %d replacement(s); the document could not be "
                "rolled back and may be partially edited (%s)."
            ) % (count, cause_msg)
            payload = {
                "status": "error",
                "code": "ROLLBACK_FAILED",
                "message": msg,
                "replaced_count": count,
                "partial_failure": True,
                "attempted": count,
            }
            return payload

        tracked_delete = tracked_delete_box[0]
        if doomed and count == 0:
            resp = _table_deletion_result([name for _table, name in doomed], tracked_delete)
        else:
            resp = build_search_replace_response(count, use_preserve=params.use_preserve)
            if max_limit_hit:
                resp["message"] += f" Note: Replacement stopped at the safety limit of {search_mod.MAX_SEARCH_REPLACEMENTS} to avoid excessive document changes. The document is not fully updated."
            if count > 1:
                resp["message"] += " edited_context shows the first occurrence's neighborhood."
            if doomed:
                deletion = _table_deletion_result([name for _table, name in doomed], tracked_delete)
                resp["table_deleted"] = deletion["table_deleted"]
                if "pending_review" in deletion:
                    resp["pending_review"] = deletion["pending_review"]
                resp["message"] = (resp.get("message") or "").rstrip() + " " + deletion["message"]
        html_import_styles.attach_write_report(
            resp, report, total_matches=count,
            suggest_style_fix=functools.partial(_partial_match_style_suggestion, doc, params))
        resp.update(hyperlink_fields(link_reports))
        return attach_edited_context(resp, anchor)

    def _anchor_miss(
        self, doc: Any, anchor: AnchorSpec, miss_kind: search_mod.AnchorMissKind
    ) -> dict[str, Any]:
        # Anchors spanning text across document structure cannot be applied as shape-text
        # replacements, so matches found in drawing shapes are only reported to the user
        # without mutating shape text. Review-mode wording is omitted because shapes are
        # never edited on the anchor path.
        if miss_kind in ("start", "end"):
            anchor_text = anchor.start if miss_kind == "start" else anchor.end
            subject = "start_anchor" if miss_kind == "start" else "end_anchor"
            cs = search_mod.resolve_case_sensitive(anchor.case_sensitive)
            shape_name = search_mod.drawing_shape_containing(doc, anchor_text, case_sensitive=cs)
            if shape_name:
                return search_mod.shape_only_search_response(subject, shape_name)
        return {
            "status": "error",
            "code": _ANCHOR_MISS_CODES[miss_kind],
            "message": search_mod.anchor_miss_message(
                miss_kind, tracked_deletions=document_has_tracked_deletions(doc)
            ),
            "replaced_count": 0,
        }

    def _edit_search(
        self, ctx: ToolContext, session: EditReviewSession, params: EditParams,
    ) -> dict[str, Any]:
        doc = ctx.doc
        if params.anchor is not None:
            try:
                found, miss_kind, mode = search_mod.find_anchor_span(
                    doc,
                    params.anchor.start,
                    params.anchor.end,
                    occurrence=0 if params.occurrence is None else params.occurrence,
                    case_sensitive=params.anchor.case_sensitive,
                )
            except search_mod.OccurrenceOutOfRange as e:
                res = self._tool_error(str(e), code="OCCURRENCE_OUT_OF_RANGE")
                res.update(e.mode.result_fields())
                return res
            except IndexError as e:
                return self._tool_error(str(e), code="OCCURRENCE_OUT_OF_RANGE")
            if found is None:
                assert miss_kind is not None
                res = self._anchor_miss(doc, params.anchor, miss_kind)
            elif _range_contains_table(found):
                res = self._tool_error(
                    "The range from start_anchor through end_anchor contains a table; delete the table first with table_delete.",
                    code="TABLE_IN_ANCHOR_RANGE",
                )
            else:
                res = self._edit_replace_one(ctx, session, params, found)
            res.update(mode.result_fields())
            return res

        many = bool(params.all_matches) or params.occurrence is not None
        ranges, mode = _find_ranges(
            doc,
            params.pattern,
            params.regex,
            params.case_sensitive,
            limit=search_mod.MAX_SEARCH_REPLACEMENTS if many else 1,
        )
        # Empty ranges fall through to the existing miss / drawing-shape path (models often send occurrence=0).
        if not ranges:
            res = self._finish_search_miss(doc, params)
        elif params.all_matches:
            res = self._edit_replace_all(ctx, session, params, ranges)
        elif params.occurrence is not None and params.occurrence >= len(ranges):
            res = self._occurrence_oor(params.occurrence, len(ranges))
        else:
            found = ranges[params.occurrence or 0]
            if params.position in ("before", "after"):
                res = self._edit_insert_next_to_match(ctx, session, params, found)
            else:
                res = self._edit_replace_one(ctx, session, params, found)
        res.update(mode.result_fields())
        return res

    def _edit_insert_next_to_match(
        self, ctx: ToolContext, session: EditReviewSession, params: EditParams, found: Any,
    ) -> dict[str, Any]:
        doc = ctx.doc
        plain = params.plain
        use_preserve = params.use_preserve
        # Plain content was normalized in _parse_edit_params. Markup is normalized here, before the
        # math guard and the block test, because the inserts below do not decode HTML5-only names.
        content = (
            params.content if use_preserve
            else html_import_styles.normalize_character_references(params.content)
        )
        has_table = params.has_table
        occurrence = params.occurrence
        position = params.position
        # INSERT beside the match collapses a cursor at the match edge and runs HTML import
        # there so review mode records only new text, not a tracked delete+reinsert of the
        # unchanged clause.
        #
        # One guarded gap remains: the HTML import path is not cell-safe (same nested-XText
        # hazard the replace path detects), so a match in a table cell is refused below.
        # TextTable is None outside a cell. A throw is a closed document;
        # do not treat it as "not a cell" and run the cell-unsafe import.
        in_cell = found.getText().createTextCursorByRange(
            found.getStart()).getPropertyValue("TextTable") is not None
        if in_cell:
            return self._tool_error(
                "position='before'/'after' next to a match inside a table cell is not "
                "supported yet; use position='replace' with plain text, or rewrite the "
                "cell content.",
                code="INVALID_PARAM")
        # Inline content goes in at the exact match edge without a <p>.
        # Block content (<p>, headings, tables) imported mid-paragraph
        # splits the host paragraph, and LibreOffice merges the first
        # imported block into the text before the cursor: anchoring on
        # the end of "4. CABIMENTO ... EM DOBRO" leaves "4. CABIMENTO
        # NOVA SECAO A" / "NOVA SECAO B" / "EM DOBRO", and even
        # position='after' on the last words of a paragraph glues the
        # new paragraph onto it. Plain text wrapped in <p> first splits
        # the paragraph too. Block content goes between paragraphs --
        # before the anchor's paragraph, or into a fresh paragraph
        # opened right after it. A line break is a paragraph too: plain
        # "Novo A\n\nNovo B" on the inline path goes in as manual line
        # breaks glued onto the matched paragraph.
        if use_preserve:
            is_block = "\n" in content
        else:
            is_block = html_import.content_is_block(str(content))
        text_obj = found.getText()
        if is_block:
            insert_cursor, snapped = _paragraph_boundary_cursor(text_obj, found, position)
        else:
            edge = found.getStart() if position == "before" else found.getEnd()
            insert_cursor, snapped = text_obj.createTextCursorByRange(edge), False

        report = html_import_styles.WriteReport()

        def _insert_next_to_match() -> None:
            if not is_block:
                report.math.extend(html_import.insert_inline_at_cursor(doc, ctx.ctx, insert_cursor, str(content)))
                report.unapplied_para.extend(html_import_styles.unapplied_para_names_for_content(str(content)))
            elif position != "after":
                # A block import at the start of the match's paragraph adds paragraphs before it
                # without merging, the same as target='beginning'.
                rep = html_import.insert_html_at_cursor(doc, ctx.ctx, insert_cursor, content)
                if has_table:
                    html_import.raise_if_no_table_added(rep.tables_added)
                report.extend(rep)
            else:
                # Importing at a paragraph end merges into it; insert_html_after_paragraph
                # opens a fresh paragraph after it and cleans up the leftover break.
                rep = html_import.insert_html_after_paragraph(
                    doc, ctx.ctx, text_obj, insert_cursor, content, apply_styles=True
                )
                if has_table:
                    html_import.raise_if_no_table_added(rep.tables_added)
                report.extend(rep)

        anchor = collapsed_anchor(found)
        # The insert lands at the end of a match the user often has selected; Writer
        # stretches the view selection over text inserted at its end, so put it back.
        with KeepViewSelection(uno_read(lambda: doc.getCurrentController(), None)), session:
            record_html_atomically(
                session, doc, _insert_next_to_match,
                proposed_preview=plain)
        where = ("%s the paragraph that contains the match (block content cannot go mid-paragraph "
                 "without splitting it)" % position) if snapped else "%s the old_content match" % position
        insert_resp: dict[str, object] = {
            "status": "ok",
            "message": "Inserted content %s (matched text left untouched)." % where,
            "inserted": True,
            "position": position,
        }
        if snapped:
            insert_resp["snapped_to_paragraph"] = True
        if occurrence is not None:
            insert_resp["occurrence"] = occurrence
        html_import_styles.attach_write_report(insert_resp, report)
        return attach_edited_context(insert_resp, anchor)


    def _edit_replace_one(
        self, ctx: ToolContext, session: EditReviewSession, params: EditParams, found: Any,
    ) -> dict[str, Any]:
        doc = ctx.doc
        found = _grow_over_edge_space(found, params.separator)
        # Anchor BEFORE the mutation: the found range's content is replaced (HTML path even
        # deletes-then-imports). edited_context uses this collapsed start.
        # replace_preserving_format pushes a cursor saved here forward, so the
        # outline URL is painted from the paragraph start (_select_replacement), not from
        # this anchor. Outline capture happens inside _replace_found, also before that delete.
        anchor = collapsed_anchor(found)
        # An empty replacement that is the last text in a table deletes
        # the table. Clearing only that text and returning success leaves
        # the empty shell: the table's name is a CSS class in the HTML,
        # and the agent edited text instead of calling table_delete. A
        # partial cell clear (other cells still have text, or the match
        # is only part of its cell) falls through to the text replace
        # below. Same delete path as table_delete, inside this edit's
        # review session.
        doomed = writer_tables_emptied_by_matches([found], params.content)
        if doomed:
            # The undo step is outermost, so a rollback runs after the session restores RecordChanges and the author.
            with UndoStepOrRollback(doc, next_agent_edit_undo_title()), session:
                tracked_delete = record_table_deletions(session, doc, ctx, doomed)
            return _table_deletion_result([name for _table, name in doomed], tracked_delete)
        report = html_import_styles.WriteReport()
        reports = self._replace_found(
            session, doc, found, use_preserve=params.use_preserve,
            content=params.content, ctx=ctx,
            track_reviewable=params.track_reviewable, override=params.override, batch=False,
            plain=params.plain, report=report,
            fix_outline=params.anchor is None,
            has_table=params.has_table)
        if params.anchor is not None:
            resp = build_search_replace_response(
                1,
                use_preserve=params.use_preserve,
                message="Replaced the range from start_anchor through end_anchor.",
            )
        else:
            resp = build_search_replace_response(1, use_preserve=params.use_preserve)
        if params.occurrence is not None:
            resp["occurrence"] = params.occurrence
        html_import_styles.attach_write_report(
            resp, report, total_matches=1,
            suggest_style_fix=functools.partial(_partial_match_style_suggestion, doc, params))
        resp.update(hyperlink_fields(reports))
        return attach_edited_context(resp, anchor)
