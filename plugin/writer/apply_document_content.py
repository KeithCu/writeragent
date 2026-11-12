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
"""Writer apply_document_content tool — write HTML/text content into document."""

from __future__ import annotations

import functools
import html as html_mod
import logging
import re
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any

from plugin.framework.queue_executor import main
from plugin.framework.tool import ToolBase

if TYPE_CHECKING:
    from plugin.framework.tool import ToolContext

from plugin.framework.prompts import APPLY_DOCUMENT_CONTENT_TOOL_RESEARCH_HINT
from plugin.writer.edit_review import (
    EditReviewSession,
    annotate_review_status,
    edit_review_wait_seconds,
    review_recording_enabled,
    get_agent_edit_review_mode,
    record_preserve_replace,
    record_html_atomically,
    collapsed_anchor,
    selection_anchor,
    attach_edited_context,
    next_agent_edit_undo_title,
)
from plugin.doc.compound_undo import UndoUnavailable, commit_or_rollback
from plugin.writer.review_scan import document_has_tracked_deletions
from plugin.writer.specialized.shapes import replace_text_in_shape
from plugin.writer.specialized.tables import record_table_deletions
from plugin.writer.table_nesting import cell_hosted_table_names
from plugin.writer.specialized_base import INSERT_TARGETS, enum_param
from plugin.framework.errors import (
    Diagnostic,
    SuggestedCall,
    ToolExecutionError,
    format_error_payload,
    reraise_if_disposed,
    suggest_call,
    uno_read,
)
from plugin.framework.uno_context import enum_has_more
from plugin.framework.json_utils import safe_json_loads
from plugin.writer import search as search_mod
from plugin.writer import html_import_styles
from plugin.writer.hyperlink_fixup import (
    OutlineSnapshot,
    capture_outline_hyperlinks,
    plan_outline_updates,
    public_hyperlink_reports,
    restore_outline_hyperlinks,
)


log = logging.getLogger("writeragent.writer")


# com.sun.star.text.ControlCharacter.PARAGRAPH_BREAK
_PARAGRAPH_BREAK = 0
_TABLE_INSERT_FAILED_MSG = "Table insert produced no table in document."


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


# Named (``&amp;``), decimal (``&#36;``) and hex (``&#x24;``) character references.
# Guards the plain-text unescape so a bare "&" is never touched.
_ENTITY_RE = re.compile(r"&(?:#[0-9]+|#[xX][0-9a-fA-F]+|[A-Za-z][A-Za-z0-9]{1,31});")


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
        from plugin.framework.uno_context import uno_same

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
    apply_document_content and get status ok — the shell stays and
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


def _doc_table_count(doc: Any) -> int | None:
    """Table count from doc.getTextTables(); None when unreadable (a mock). Disposal propagates."""
    # Invariant: uno_read raises DocumentDisposedError, so table verification never reports success on a closed doc.
    tables = uno_read(lambda: doc.getTextTables(), None)
    if tables is None:
        return None
    count = uno_read(lambda: tables.getCount(), None)
    return count if isinstance(count, int) else None


def _table_insert_failed(doc: Any, before: int | None) -> bool:
    """True when an HTML import that carries a <table> produced none.

    *before* is the table count captured before the mutation. None means the
    whole document was replaced first, so any surviving table came from the import.
    """
    after = _doc_table_count(doc)
    if after is None:
        return False
    return after == 0 if before is None else after <= before


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
    doc: Any, pattern: str, regex: bool, cs: bool, use_opts: bool, *, all_matches: bool
) -> Any:
    if use_opts:
        return search_mod.find_ranges_regex_case(doc, pattern, regex, cs, all_matches=all_matches)
    return (search_mod.find_all_ranges if all_matches else search_mod.find_first_range)(doc, pattern)


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

    Follows the ten safety rules from section 3.5 of structured-edit-diagnostics-plan.md:
    1. Target new visible text (params.plain), not the original search string.
    2. Single paragraph check: params.plain must be a single paragraph (no newlines).
    3. Exactly one distinct non-empty data-lo-style token.
    4. Resolved style token must not fall back to Standard unless it was Standard.
    5. Exact post-write match count in document matches expected count.
    6. Case sensitivity: case_sensitive=True.
    7. Host paragraph restyling notice: caller's warning message explicitly notifies
       that apply_style restyles the entire host paragraph.
    8. all_matches: True included if params.all_matches is True.
    9. Not recording changes: redline recording skips apply_style because it is not tracked.
    10. target != "selection": selection cursors do not reliably span the new text afterwards.
    """
    if params.target == "selection":
        return None

    from . import record_changes

    if record_changes.is_recording_changes(doc):
        return None

    plain = params.plain
    if not plain or "\n" in plain or "\r" in plain:
        return None

    if not style_token:
        return None

    from . import html_import_styles

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

    from . import search as search_mod

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


def _attach_style_notes(
    result: dict[str, Any],
    notes: list[str],
    *,
    total_matches: int = 1,
    doc: Any,
    params: EditParams,
) -> dict[str, Any]:
    """Append unapplied data-lo-style diagnostic warnings to result's warnings and message fields."""
    if not isinstance(result, dict) or result.get("status") != "ok" or not notes:
        return result

    partial_count = notes.count("STYLE_IGNORED_PARTIAL_MATCH")
    recording_count = notes.count("STYLE_IGNORED_RECORDING")
    if partial_count == 0 and recording_count == 0:
        return result

    if not params.content or "data-lo-style" not in params.content:
        return result

    from . import html_import_styles

    _clean, styles, _paras, _skipped = html_import_styles.extract_block_lo_styles(params.content)
    distinct_styles = list(dict.fromkeys(s for s in styles if s))
    if not distinct_styles:
        return result
    style_token = distinct_styles[0] if len(distinct_styles) == 1 else None

    subject = f"data-lo-style='{style_token}'" if style_token else "data-lo-style"
    diagnostics: list[Diagnostic] = []

    if partial_count > 0:
        scope_clause = (
            f" to {partial_count} of {total_matches} matches because they do not cover whole paragraphs"
            if total_matches > 1
            else " because the replaced range does not cover a whole paragraph"
        )
        msg = (
            f"{subject} was not applied{scope_clause}; "
            "applying it would restyle neighboring text in the host paragraph. "
            "apply_style restyles the whole paragraph containing the match. "
            "Restyle with apply_style(target='search', old_content=...)."
        )
        partial_diag: Diagnostic = {
            "code": "STYLE_IGNORED_PARTIAL_MATCH",
            "message": msg,
            "matches_skipped": partial_count,
            "matches_total": total_matches,
        }
        suggested = _partial_match_style_suggestion(doc, params, style_token, total_matches)
        if suggested is not None:
            partial_diag["suggested_call"] = suggested
        diagnostics.append(partial_diag)

    if recording_count > 0:
        rec_scope = f" to {recording_count} of {total_matches} matches" if total_matches > 1 else ""
        rec_msg = (
            f"{subject} was not applied{rec_scope} while tracked changes are recording, "
            "because a paragraph style on this replace would turn the deletion into a format change. "
            "Restyle with apply_style(target='search', old_content=...) after the change is accepted."
        )
        rec_diag: Diagnostic = {
            "code": "STYLE_IGNORED_RECORDING",
            "message": rec_msg,
            "matches_skipped": recording_count,
            "matches_total": total_matches,
        }
        diagnostics.append(rec_diag)

    if not diagnostics:
        return result

    existing_warnings = result.get("warnings")
    if isinstance(existing_warnings, list):
        existing_warnings.extend(diagnostics)
    else:
        result["warnings"] = diagnostics

    note_text = " ".join(d["message"] for d in diagnostics)
    existing_message = result.get("message") or ""
    if existing_message:
        result["message"] = f"{existing_message} {note_text}"
    else:
        result["message"] = note_text

    return result


def _run_on_main_thread(fn: Any) -> Any:
    if main.is_current():
        return fn()
    return main.call(fn, timeout=60.0)


def _search_options(
    ctx: ToolContext,
    kwargs: Any,
    old_stripped: str,
    search_string: str,
) -> tuple[bool, bool, bool, str, str | None]:
    regex = bool(kwargs.get("regex"))
    raw = kwargs.get("case_sensitive")
    explicit = raw is not None
    use_opts = regex or explicit
    cs = search_mod.resolve_case_sensitive(raw)
    pattern = old_stripped if regex else search_string
    rex_err = search_mod.validate_icu_regex(ctx.ctx, old_stripped) if regex else None
    return regex, use_opts, cs, pattern, rex_err


@dataclass(frozen=True)
class AnchorSpec:
    start: str
    end: str
    case_sensitive: bool = search_mod.SEARCH_CASE_SENSITIVE_DEFAULT


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
    use_opts: bool = False
    case_sensitive: bool = False
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

    - **Search** (``target='search'`` only): ``old_content`` must be a **substring** to find —
      a phrase, sentence, or multi-paragraph **block**, not the entire document. To replace
      **all** document content, you **must** use ``target='full_document'`` with ``content`` only;
      **never** pass the full body as ``old_content``. Search uses ``search.iter_literal_ranges`` (LO
      regex search, then paragraph chaining). See ``tests/writer/test_content_search_uno.py``.
      A match that sits in an outline hyperlink (``#…|outline``) also updates that
      target when the matched text occurs once in the URL outside that suffix, or
      when ``hyperlink_url`` is passed for that one link. ``content`` may equal
      ``old_content`` when only the URL is stale. A repeated title is left
      unchanged. Bookmark targets are not rewritten. See ``hyperlink_fixup``.
      To replace a range between landmarks without copying the middle, use
      target='search' with start_anchor and end_anchor instead of old_content.
      The range runs from the start of start_anchor through the end of the first
      end_anchor beginning at or after that start match in the same text.
      occurrence selects the start match; regex, all_matches, position,
      dry_run, and hyperlink_url do not apply to anchor spans.
      Multi-line plain text becomes new paragraphs; single-line plain text replaces the span
      inline and keeps the host paragraph and its style. Anchor replacements skip outline URL fixup.
    """

    name: str | None = "apply_document_content"
    description: str = (
        "Insert or replace content. "
        f"IMPORTANT: {APPLY_DOCUMENT_CONTENT_TOOL_RESEARCH_HINT} "
        "To replace the ENTIRE document use target='full_document' with content only — "
        "do NOT pass the whole document as old_content. "
        "Use target='beginning', 'end', or 'selection' to insert. "
        "Use target='search' with old_content or both start_anchor and end_anchor for find-and-replace of a specific substring or range. "
        "An empty replacement that removes the last text in a table deletes that table. "
        "Clearing one cell while another cell still has text leaves the table in place."
    )
    parameters: dict[str, Any] | None = {
        "type": "object",
        "properties": {
            "content": {"type": ["array", "string"], "items": {"type": "string"}, "description": ("List of HTML fragments or plain-text fragments (one per block), or one string. A string is one block; execute still joins a list with newlines. Shape and math per the APPLY_DOCUMENT_CONTENT AND HTML rules — the editing-html guidance covers them if they are not already in your context. No Markdown. An empty string at beginning, end, or selection inserts nothing and returns an error. An empty search replacement deletes the match.")},
            "target": enum_param(INSERT_TARGETS, "Where to apply the content."),
            "old_content": {"type": "string", "description": ("Substring to find when target='search'. Not for whole-document replace — use target='full_document' instead.")},
            "start_anchor": {"type": "string", "description": "Literal substring marking the start of a range to replace; requires end_anchor, where the end is the first match that begins at or after the start match in the same text and the middle is not copied. Multi-line plain text becomes new paragraphs; single-line plain text replaces the span inline and keeps the host paragraph and its style; anchor replacements skip outline URL fixup."},
            "end_anchor": {"type": "string", "description": "Literal substring marking the end of a range to replace; requires start_anchor, matching the first occurrence that begins at or after the start match in the same text and the middle is not copied."},
            "all_matches": {"type": "boolean", "description": "Replace all occurrences (true) or first only. Default false. Only for target='search' with position='replace' and old_content (cannot be combined with start_anchor and end_anchor)."},
            "occurrence": {"type": "integer", "minimum": 0, "description": ("For target='search': 0-based index into replaceable Writer text matches (body/table/frame) or the start_anchor match (not dry_run shape/comment rows). Omit for the existing first-match behavior. Cannot be combined with all_matches=true.")},
            "position": enum_param(
                ["replace", "before", "after"],
                (
                    "For target='search': 'replace' (default) replaces the match; 'before'/'after' INSERT the content next to the match and leave the matched text untouched (result reports inserted=true instead of replaced_count). Inline content (plain text, <b>, <i>, ...) goes at the exact match edge; block content (<p>, headings, lists, tables) goes before/after the whole paragraph that holds the match (result reports snapped_to_paragraph=true when that moved it)."
                ),
            ),
            "dry_run": {"type": "boolean", "description": "For target='search' with old_content: do NOT edit. Return replaceable matches (each tagged with occurrence) plus shape/comment previews, so you can check before committing. Does not apply to start_anchor and end_anchor."},
            "regex": {"type": "boolean", "description": "For target='search': treat old_content as a regular expression (default false = literal). Regex mode is single-paragraph (no cross-paragraph chaining). Does not apply to start_anchor and end_anchor. Patterns use ICU regex syntax (LibreOffice's engine): (?<name>...) for named groups, not (?P<name>...); \\p{Lu} classes are allowed. Floating drawing shapes are matched with Python re instead, so ICU-only syntax does not match inside shapes."},
            "case_sensitive": {"type": "boolean", "description": f"For target='search': {search_mod.CASE_SENSITIVE_DESCRIPTION}"},
            "hyperlink_url": {"type": "string", "description": ("For target='search' with position='replace', when the match overlaps exactly one outline hyperlink (#…|outline): set this exact URL instead of substituting the matched text. content may equal old_content when the visible title is already correct and only the URL is stale. Omit it to update that target when the matched text occurs once outside the |outline suffix. Rejected with all_matches=true, with position='before'/'after', and when the match is not exactly one outline link. Bookmark targets are never rewritten.")},
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

    def _table_insert_error(self, doc: Any, has_table: bool, before: int | None) -> dict[str, Any] | None:
        if has_table and _table_insert_failed(doc, before):
            return self._tool_error(_TABLE_INSERT_FAILED_MSG, code="TABLE_INSERT_FAILED")
        return None

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
    def _replacement_plain(content: Any) -> str:
        """Visible text a search replace will insert, for outline-URL substitution."""
        from . import html_import as format_support

        if isinstance(content, list):
            content = "\n".join(str(part) for part in content)
        text = "" if content is None else str(content)
        if format_support.content_has_markup(text):
            return format_support.plain_text_if_markup(text)
        return text.replace("\\n", "\n").replace("\\t", "\t")

    def _outline_override_error(self) -> dict[str, Any]:
        return self._tool_error(
            "hyperlink_url applies only when the match overlaps exactly one outline hyperlink (|outline).",
            code="INVALID_PARAM")

    def _read_outline_snapshot(self, found: Any, log_message: str) -> tuple[OutlineSnapshot | None, dict[str, Any] | None]:
        try:
            return capture_outline_hyperlinks(found), None
        except Exception as exc:
            reraise_if_disposed(exc)
            log.exception(log_message)
            return None, self._tool_error(
                "Could not read the outline hyperlink on this match.",
                code="HYPERLINK_UPDATE_FAILED")

    def _outline_match_fields(self, found: Any, plain_fn: Any, override: str | None) -> tuple[dict[str, Any], dict[str, Any] | None]:
        """dry_run fields for one match. Empty fields when the match has no outline link.

        A failed outline read is returned as the error tuple, not empty fields.
        *plain_fn* is called only when a preview is actually needed, so a markup dry_run
        that does not touch an outline link does not open a hidden document. An override
        on a non-outline match is not previewed as a new link; the caller rejects it.
        """
        # Invariant: a failed outline read is HYPERLINK_UPDATE_FAILED (never "no link"); disposal propagates.
        snapshot, err = self._read_outline_snapshot(found, "apply_document_content: outline hyperlink preview failed")
        if err:
            return {}, err
        assert snapshot is not None
        if not snapshot.links:
            if override:
                return {}, self._outline_override_error()
            return {}, None
        plain = plain_fn()
        plans = plan_outline_updates(snapshot.links, snapshot.matched, plain, override)
        reports = public_hyperlink_reports(plans)
        if not reports:
            if override:
                return {}, self._outline_override_error()
            return {}, None
        if override and len(reports) != 1:
            return {}, self._outline_override_error()
        fields = dict(reports[0])
        if len(reports) > 1:
            fields["hyperlinks"] = reports
        return fields, None

    def _attach_hyperlink_reports(self, resp: Any, reports: list[dict[str, Any]]) -> dict[str, Any]:
        if not reports:
            return resp
        resp["hyperlinks"] = reports
        if len(reports) == 1:
            resp.update(reports[0])
        return resp

    def _outline_undo(self, doc: Any, session: EditReviewSession, run: Any) -> tuple[list[dict[str, Any]] | None, dict[str, Any] | None]:
        """Run *run* inside one undo step so the text replace and the URL write are one Ctrl+Z.

        The HTML path and a surgical review replace each close their own undo context
        before returning. Without this outer step the URL write would be a second undo.
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
        except UndoUnavailable:
            return None, self._tool_error(
                "Cannot update the outline hyperlink atomically (no usable undo context); "
                "refusing rather than risk a text change that leaves the link stale.",
                code="UNDO_UNAVAILABLE",
            )
        if outcome.mutate_error is not None:
            exc = outcome.mutate_error
            reraise_if_disposed(exc)
            log.exception("apply_document_content: outline hyperlink update failed")
            if outcome.nothing_landed:
                msg = f"Outline hyperlink update failed; the text change was rolled back ({exc})."
            else:
                msg = f"Outline hyperlink update failed; the text change could not be rolled back and may still be in the document ({exc})."
            return None, self._tool_error(msg, code="HYPERLINK_UPDATE_FAILED")

        err = outcome.error
        if err is not None and err.get("code") == "ROLLBACK_FAILED":
            msg = f"Outline hyperlink update failed; the text change could not be rolled back and may still be in the document ({err.get('message')})."
            return None, self._tool_error(msg, code="HYPERLINK_UPDATE_FAILED")

        reports = reports_holder[0] if reports_holder else []
        return reports, None

    def _finish_search_miss(self, doc: Any, params: EditParams) -> dict[str, Any]:
        shape = search_mod.drawing_shape_object_containing(
            doc, params.pattern,
            use_regex=params.regex, case_sensitive=params.case_sensitive)
        if shape is not None and not params.track_reviewable:
            shape_name = search_mod.shape_display_name(shape)
            # commit_or_rollback wraps shape replacement so a failed replace_text_in_shape
            # rolls back instead of leaving a partial setString in the document.
            def mutate() -> dict[str, Any] | None:
                edited = replace_text_in_shape(
                    shape, params.pattern, params.plain, case_sensitive=params.case_sensitive, use_regex=params.regex
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
                if err is None or err.get("code") != "SHAPE_REPLACE_FAILED":
                    result = attach_edited_context(
                        {"status": "ok",
                          "message": ("Replaced 1 occurrence inside drawing shape '%s' (edited the "
                                      "shape's own text directly — NOT a tracked change; surrounding "
                                      "formatting preserved)." % shape_name),
                          "replaced_count": 1, "review_bypassed": True},
                        None)
                    return annotate_review_status(result)
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
            case_sensitive=params.case_sensitive,
            use_regex=params.regex,
            all_matches=bool(params.all_matches),
            lenient=not params.use_opts,
            review=params.track_reviewable,
        )

    def _replace_found(self, session: EditReviewSession, doc: Any, found: Any, *,
                       use_preserve: bool, content: Any,
                       ctx: ToolContext, track_reviewable: bool,
                       override: str | None, batch: bool,
                       plain: str,
                       style_notes: list[str],
                       para_failures: list[str],
                       fix_outline: bool = True,
                       math_out: list[dict[str, Any]] | None = None) -> tuple[list[dict[str, Any]] | None, dict[str, Any] | None]:
        """Replace one search match and, when it sits in an outline link, fix that URL.

        *batch* is the all_matches loop, which already holds the undo context. A single
        match opens one when a URL write is needed. Returns ``(reports, error_or_None)``.
        """
        from . import html_import as format_support

        # An anchor span's text never equals one link title, so outline fixup has nothing to match.
        anchor = None
        snapshot = None
        needs_fix = False
        if fix_outline:
            anchor = collapsed_anchor(found)
            # Invariant: a failed capture refuses the edit (HYPERLINK_UPDATE_FAILED) before any text change; disposal propagates.
            snapshot, err = self._read_outline_snapshot(found, "apply_document_content: outline hyperlink capture failed")
            if err:
                return None, err
            assert snapshot is not None
            if override and len(snapshot.links) != 1:
                # Before the text edit. One override on two outline links would stamp
                # both with the same target. Zero links would paint a bookmark or plain text.
                return None, self._outline_override_error()
            # preserve_url: a bookmark target covering the match. A same-length setString
            # keeps HyperLinkURL. HTML setString("") and a length-changing preserve-format
            # replace can clear it on the new characters; put that target back unchanged.
            needs_fix = bool(snapshot.links) or bool(snapshot.preserve_url)

        def run() -> list[dict[str, Any]]:
            original = found.getString()
            if use_preserve:
                record_preserve_replace(session, doc, found, content, ctx.ctx, track_reviewable)
            elif batch:
                # The all_matches loop already entered an undo context. A second
                # record_html_atomically would nest another and close it early.
                def replace_one(f: Any = found) -> None:
                    failed, note = format_support.replace_single_range_with_content(
                        doc, f, content, ctx.ctx, math_out=math_out)
                    para_failures.extend(failed)
                    if note:
                        style_notes.append(note)

                session.record_mutation(
                    replace_one,
                    original_preview=original, proposed_preview=plain)
            else:
                def replace_single() -> None:
                    failed, note = format_support.replace_single_range_with_content(
                        doc, found, content, ctx.ctx, math_out=math_out)
                    para_failures.extend(failed)
                    if note:
                        style_notes.append(note)

                record_html_atomically(
                    session, doc,
                    replace_single,
                    original_preview=original, proposed_preview=plain)
            if not needs_fix or anchor is None or snapshot is None:
                return []
            return restore_outline_hyperlinks(anchor, snapshot, plain, override)

        if batch:
            return run(), None
        # A single match has to enter the review session here, outline link or not
        # (all_matches already entered it around the loop), or record mode would
        # only track replaces that happen to sit in an outline URL.
        if not needs_fix:
            with session:
                return run(), None

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
            log.exception("apply_document_content: could not read review wait seconds")
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
        content = kwargs.get("content")
        if isinstance(content, list):
            content = "\n".join(str(part) for part in content)
        # Empty replacements delete the match without painting a put-back URL.
        rewrites_outline = position == "replace" and str(content or "").strip() != ""
        edit_idx = 0 if occurrence is None else occurrence

        old_stripped = str(old_content).strip() if old_content is not None else ""
        s = search_mod.prepare_old_content(old_content)
        if not s:
            return self._tool_error("old_content is empty after normalization.", code="INVALID_PARAM")
        regex, use_opts, cs, pattern, rex_err = _search_options(ctx, kwargs, old_stripped, s)
        if rex_err:
            return self._invalid_regex(rex_err)
        try:
            ranges = _find_ranges(ctx.doc, pattern, regex, cs, use_opts, all_matches=True)
        except Exception as e:
            log.exception("apply_document_content dry_run search failed")
            return self._tool_error("dry_run search failed: %s" % e, code="SEARCH_FAILED")
        labeler = search_mod.LocationLabeler(ctx.doc)
        replaceable: list[dict[str, Any]] = []

        @functools.cache
        def _plain() -> str:
            return self._replacement_plain(kwargs.get("content"))

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
                fields, outline_err = self._outline_match_fields(found, _plain, row_override)
                if outline_err:
                    return outline_err
                row.update(fields)
            replaceable.append(row)
        shape_comment_warning: str | None = None
        if regex and search_mod.validate_regex_pattern(pattern) is not None:
            shape_hits = []
            comment_hits = []
            shape_comment_warning = "shape/comment text not searched: pattern uses ICU-only syntax"
        else:
            shape_hits = search_mod.sweep_draw_shape_preview_matches(ctx.doc, pattern, regex, cs, limit=search_mod.MAX_SEARCH_HITS)
            comment_hits = search_mod.sweep_comment_preview_matches(ctx.doc, pattern, regex, cs, limit=search_mod.MAX_SEARCH_HITS)
        not_replaceable: list[dict[str, Any]] = []
        for item in shape_hits + comment_hits:
            if len(replaceable) + len(not_replaceable) >= search_mod.DEFAULT_SEARCH_RESULTS:
                break
            not_replaceable.append({"location": item["location"], "text": item["text"][:160]})
        total = len(ranges) + len(shape_hits) + len(comment_hits)
        if occurrence is not None and ranges and occurrence >= len(ranges):
            return self._occurrence_oor(
                occurrence, len(ranges),
                replaceable_count=len(ranges),
                replaceable=replaceable,
                not_replaceable=not_replaceable,
            )
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
                "shape and comment preview. search_in_document uses the same split. Only floating shapes are "
                "editable in review-off mode or via the shapes toolset."
            ),
        }
        if shape_comment_warning is not None:
            result["warning"] = shape_comment_warning
        if not ranges:
            res = search_mod.build_search_not_found_response(
                ctx.doc, pattern, case_sensitive=cs, use_regex=regex, lenient=not use_opts
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
                fields, outline_err = self._outline_match_fields(selected, _plain, override)
                if outline_err:
                    return outline_err
                selected_match.update(fields)
            result["selected_match"] = selected_match
        return result

    def _release_review_session(self, session: Any) -> None:
        try:
            _run_on_main_thread(session.cleanup)
        except Exception:
            log.exception("apply_document_content: review session cleanup failed")

    def _resolve_heading_links(
        self, doc: Any, params: EditParams
    ) -> tuple[EditParams, list[Diagnostic]]:
        """Resolve bare heading links in written HTML against document headings."""
        if not html_import_styles.has_bare_fragment_links(params.content):
            return params, []

        from plugin.doc.text_helpers import build_heading_tree, iter_heading_nodes

        document_headings: list[str] = (
            []
            if params.target == "full_document"
            else [
                str(node.get("text", ""))
                for node in iter_heading_nodes(build_heading_tree(doc))
            ]
        )

        def is_bookmark(name: str) -> bool:
            return bool(uno_read(lambda: doc.getBookmarks().hasByName(name), False))

        new_content, diags = html_import_styles.resolve_heading_links(
            params.content,
            document_headings,
            is_bookmark=is_bookmark,
        )
        return replace(params, content=new_content), diags

    def execute(self, ctx: ToolContext, **kwargs: Any) -> dict[str, Any]:
        result = self._execute(ctx, **kwargs)
        if self._effective_target(kwargs.get("target"), kwargs.get("old_content")) == "search":
            result["matched_case_sensitive"] = search_mod.resolve_case_sensitive(kwargs.get("case_sensitive"))
        if kwargs.get("target") == "search" and "normalized_old_content" not in result:
            result["normalized_old_content"] = search_mod.prepare_old_content(kwargs.get("old_content"))
        return result

    def _execute(self, ctx: ToolContext, **kwargs: Any) -> dict[str, Any]:
        on_main = main.is_current()

        if kwargs.get("dry_run"):
            # The preview reads ranges and text through UNO, so a worker-thread call
            # (tool-sync-apply_document_content) marshals it like the edit path does.
            return _run_on_main_thread(lambda: self._dry_run_preview(ctx, **kwargs))
        wait_seconds = self._review_wait_seconds()
        session_box: list[Any] = []

        def _do_edit() -> tuple[dict[str, Any], Any]:
            parsed = self._parse_edit_params(ctx, kwargs)
            if isinstance(parsed, dict):
                return parsed, None
            parsed, diags = self._resolve_heading_links(ctx.doc, parsed)
            session = EditReviewSession(ctx.doc, ctx.ctx, enabled=parsed.track_reviewable)
            session_box.append(session)
            result = self._dispatch_edit(ctx, session, parsed)
            result = html_import_styles.attach_link_warnings(result, diags)
            return result, session

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
                result = _run_on_main_thread(_do_edit)[0]
                return annotate_review_status(result)
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
            result, session = _run_on_main_thread(_do_edit)
        except Exception as e:
            if session_box:
                self._release_review_session(session_box[0])
            if isinstance(e, ToolExecutionError):
                return self._tool_error(str(e), code=getattr(e, "code", "TOOL_EXECUTION_ERROR"))
            raise
        if session is None or not session.changes or result.get("status") != "ok":
            if session is not None:
                self._release_review_session(session)
            return result

        # In-app chat: surface a sidebar status while we block (MCP has no sidebar -> None, no-op).
        # The callback marshals onto the chat drain queue, so it is safe from this worker thread.
        status_cb = ctx.status_callback
        if callable(status_cb):
            try:
                status_cb("Review the agent's changes in the document — accept or reject the tracked changes.")
            except Exception:
                log.debug("apply_document_content: status_callback failed", exc_info=True)

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
        from . import html_import as format_support

        unsupported = self._reject_unsupported_with_anchors(kwargs)
        if unsupported is not None:
            return unsupported

        if start_anchor is None or end_anchor is None:
            return self._tool_error("start_anchor and end_anchor are both required.", code="INVALID_PARAM")
        if not isinstance(start_anchor, str) or not isinstance(end_anchor, str):
            return self._tool_error("start_anchor and end_anchor must both be strings.", code="INVALID_PARAM")
        norm_start = search_mod.normalize_search_string_for_find(format_support.plain_text_if_markup(start_anchor))
        if not norm_start:
            return self._tool_error("start_anchor is empty after normalization.", code="INVALID_PARAM")
        norm_end = search_mod.normalize_search_string_for_find(format_support.plain_text_if_markup(end_anchor))
        if not norm_end:
            return self._tool_error("end_anchor is empty after normalization.", code="INVALID_PARAM")
        cs = search_mod.resolve_case_sensitive(kwargs.get("case_sensitive"))
        return AnchorSpec(start=norm_start, end=norm_end, case_sensitive=cs)

    def _parse_edit_params(self, ctx: ToolContext, kwargs: Any) -> EditParams | dict[str, Any]:
        from . import html_import as format_support
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

        # If the model serialized a list as a JSON string, parse it back so commas/brackets
        # do not become literal document text.
        if isinstance(content, str):
            stripped = content.strip()
            if stripped.startswith("[") and "<" in stripped:
                parsed = safe_json_loads(stripped)
                if isinstance(parsed, list):
                    content = parsed

        if isinstance(content, list):
            if any(not isinstance(x, str) for x in content):
                return self._tool_error("content list items must be strings.", code="INVALID_PARAM")
            content = "\n".join(content)

        if not isinstance(content, str):
            return self._tool_error("content must be a string or a list of strings.", code="INVALID_PARAM")

        if target in ("beginning", "end", "selection") and content.strip() == "":
            return self._tool_error("content is empty; nothing was inserted.", code="INVALID_PARAM")

        # Plain anchor text with newlines is imported as new paragraphs so existing character-level
        # styles and headings across the span do not bleed into the new text. Single-line plain
        # anchor text remains unwrapped so mid-paragraph edits stay on the inline path without splitting
        # the host paragraph. Entities are left intact here because the HTML parser decodes them during import.
        use_preserve = (not format_support.content_needs_html_import(content)) and anchor is None

        if not format_support.content_needs_html_import(content):
            content = content.replace("\\n", "\n").replace("\\t", "\t")
            if anchor is not None and not format_support.html_fragment_contains_mixed_math(content):
                if "\n" in content:
                    lines = [line for line in content.split("\n") if line.strip()]
                    content = "\n".join("<p>%s</p>" % line for line in lines)
            elif use_preserve:
                # HTML entry points decode complete references once, and the StarWriter
                # filter decodes what remains in markup. This format-preserving path must
                # do the same, or content lands verbatim. content_has_markup() sees no tag
                # in "R&#36;5.000,00", so an escaped dollar sign takes the plain path. Decode
                # ONLY complete references (_ENTITY_RE, ";" required); unescaping the whole
                # string also expands semicolon-less names ("&sect 2o" -> "§ 2o"). A bare "&"
                # in "Banco & Cia" is never touched.
                if _ENTITY_RE.search(content):
                    content = _ENTITY_RE.sub(lambda m: html_mod.unescape(m.group(0)), content)

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
        use_opts = False
        cs = anchor.case_sensitive if anchor is not None else False
        separator = 0
        override: str | None = None

        if target == "search" and not has_anchors:
            old_stripped = str(old_content).strip() if old_content is not None else ""
            search_string = search_mod.prepare_old_content(old_content)
            if not search_string:
                # Parameter error (like old_content=None), not a search no-op: the search never ran,
                # so there's no replaced_count to report — use the standard tool error shape.
                return self._tool_error("old_content is empty after normalization.", code="INVALID_PARAM")

        plain = content if use_preserve else self._replacement_plain(content)
        has_table = bool(re.search(r"<table\b", str(content), re.IGNORECASE))
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
                    and not format_support.content_has_markup(str(old_content))):
                content, separator = _match_stripped_edges(str(old_content), content)
                plain = content

            regex, use_opts, cs, pattern, rex_err = _search_options(ctx, kwargs, old_stripped, search_string)
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
            use_opts=use_opts,
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
        from . import html_import as format_support
        content = params.content
        plain = params.plain
        has_table = params.has_table
        math_objects: list[dict[str, Any]] = []
        with session:
            # Delete-then-import: make it atomic so a failed import can't strand a cleared document.
            failed = record_html_atomically(
                session, ctx.doc,
                lambda: format_support.replace_full_document(
                    ctx.doc, ctx.ctx, content, math_out=math_objects
                ),
                proposed_preview=plain) or []
        err = self._table_insert_error(ctx.doc, has_table, None)
        if err:
            return err
        summary = html_import_styles.summarize_written_blocks(content)
        res: dict[str, Any] = {
            "status": "ok",
            "message": "Replaced entire document.",
            "paragraph_count": summary["paragraph_count"],
            "headings": summary["headings"],
        }
        res = html_import_styles.attach_para_warnings(res, failed)
        return html_import_styles.attach_math_objects(res, math_objects)

    def _edit_insert_at(
        self, ctx: ToolContext, session: EditReviewSession, params: EditParams,
    ) -> dict[str, Any]:
        from . import html_import as format_support
        content = params.content
        plain = params.plain
        has_table = params.has_table
        target = params.target
        tables_before = _doc_table_count(ctx.doc) if has_table else None
        math_objects: list[dict[str, Any]] = []
        with session:
            failed, _note = session.record_mutation(
                lambda: format_support.insert_content_at_position(
                    ctx.doc, ctx.ctx, content, target, math_out=math_objects
                ),
                proposed_preview=plain)
        err = self._table_insert_error(ctx.doc, has_table, tables_before)
        if err:
            return err
        if target == "end":
            msg = "Inserted content at end."
            anchor = collapsed_anchor(ctx.doc.getText().getEnd())
        else:
            msg = "Inserted content at beginning."
            anchor = collapsed_anchor(ctx.doc.getText().getStart())
        res = attach_edited_context(
            {"status": "ok", "message": msg}, anchor)
        res = html_import_styles.attach_para_warnings(res, failed)
        return html_import_styles.attach_math_objects(res, math_objects)

    def _edit_selection(
        self, ctx: ToolContext, session: EditReviewSession, params: EditParams,
    ) -> dict[str, Any]:
        from . import html_import as format_support
        content = params.content
        plain = params.plain
        has_table = params.has_table
        # Anchor BEFORE the edit: the selection insert clears the selection first, so only a
        # collapsed position (not the selection range itself) survives the replace.
        anchor = selection_anchor(ctx.doc)
        tables_before = _doc_table_count(ctx.doc) if has_table else None
        math_objects: list[dict[str, Any]] = []
        with session:
            # Selection insert clears the selection first, then imports -> atomic (full_document above).
            failed, note = record_html_atomically(
                session, ctx.doc,
                lambda: format_support.insert_content_at_position(
                    ctx.doc, ctx.ctx, content, "selection", math_out=math_objects
                ),
                proposed_preview=plain) or ([], None)
        err = self._table_insert_error(ctx.doc, has_table, tables_before)
        if err:
            return err
        res = attach_edited_context(
            {"status": "ok", "message": "Inserted content at selection."}, anchor)
        if isinstance(note, str):
            _attach_style_notes(res, [note], total_matches=1, doc=ctx.doc, params=params)
        res = html_import_styles.attach_para_warnings(
            res, failed, reason=note if isinstance(note, str) else None
        )
        return html_import_styles.attach_math_objects(res, math_objects)


    def _edit_replace_all(
        self, ctx: ToolContext, session: EditReviewSession, params: EditParams,
    ) -> dict[str, Any]:
        doc = ctx.doc
        # replaced_count is the machine-readable success signal: 0 -> status "error" (a silent
        # no-op surfaced as a failure), N>0 -> "ok". No matched_count/warning/partial-replace:
        # if a replace raises mid-all_matches the existing abort behavior stands.
        # An explicit regex or case_sensitive uses the literal descriptor so that call matches
        # search_in_document; an omitted flag stays on the chained matcher.
        ranges = _find_ranges(
            doc, params.pattern, params.regex, params.case_sensitive, params.use_opts, all_matches=True)
        if not ranges:
            return self._finish_search_miss(doc, params)
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
        style_notes: list[str] = []
        para_failures: list[str] = []
        math_objects: list[dict[str, Any]] = []
        link_failure_holder: list[dict[str, Any] | None] = [None]
        link_reports: list[dict[str, Any]] = []
        tracked_delete_box = [False]

        def mutate() -> None:
            nonlocal count
            with session:
                for found in reversed(ranges):
                    if doomed_names and _table_and_name(found)[1] in doomed_names:
                        continue
                    found = _grow_over_edge_space(found, params.separator)
                    # batch=True: this loop already holds the undo context, so the
                    # outline URL write stays in the same Ctrl+Z as the text.
                    reports, link_err = self._replace_found(
                        session, doc, found, use_preserve=params.use_preserve,
                        content=params.content, ctx=ctx,
                        track_reviewable=params.track_reviewable, override=params.override, batch=True,
                        plain=params.plain, style_notes=style_notes,
                        para_failures=para_failures,
                        math_out=math_objects)
                    if link_err:
                        link_failure_holder[0] = link_err
                        raise RuntimeError(link_err.get("message") or "outline hyperlink update failed")
                    if reports:
                        link_reports.extend(reports)
                    count += 1
                if doomed:
                    tracked_delete_box[0] = record_table_deletions(session, doc, ctx, doomed)

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
            log.exception("apply_document_content all_matches failed mid-batch")
            payload: dict[str, Any]
            if isinstance(e, ToolExecutionError):
                payload = format_error_payload(e)
            elif link_failure_holder[0] is not None:
                payload = dict(link_failure_holder[0])
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
        _attach_style_notes(resp, style_notes, total_matches=count, doc=doc, params=params)
        reason = next((n for n in style_notes if n in ("STYLE_IGNORED_PARTIAL_MATCH", "STYLE_IGNORED_RECORDING")), None)
        html_import_styles.attach_para_warnings(resp, para_failures, reason=reason)
        self._attach_hyperlink_reports(resp, link_reports)
        res = attach_edited_context(resp, anchor)
        return html_import_styles.attach_math_objects(res, math_objects)

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
            cs = anchor.case_sensitive
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
        if params.anchor is not None:
            doc = ctx.doc
            try:
                found, miss_kind = search_mod.find_anchor_span(
                    doc,
                    params.anchor.start,
                    params.anchor.end,
                    occurrence=0 if params.occurrence is None else params.occurrence,
                    case_sensitive=params.anchor.case_sensitive,
                )
            except IndexError as e:
                return self._tool_error(str(e), code="OCCURRENCE_OUT_OF_RANGE")
            if found is None:
                assert miss_kind is not None
                return self._anchor_miss(doc, params.anchor, miss_kind)
            if _range_contains_table(found):
                return self._tool_error(
                    "The range from start_anchor through end_anchor contains a table; delete the table first with table_delete.",
                    code="TABLE_IN_ANCHOR_RANGE",
                )
            return self._edit_replace_one(ctx, session, params, found)
        if params.all_matches:
            res = self._edit_replace_all(ctx, session, params)
        else:
            doc = ctx.doc
            if params.occurrence is not None:
                # Index the same replaceable list the edit path uses. Empty ranges fall
                # through to the existing miss / drawing-shape path (models often send 0).
                ranges = _find_ranges(
                    doc, params.pattern, params.regex, params.case_sensitive, params.use_opts, all_matches=True)
                if ranges and params.occurrence >= len(ranges):
                    return self._occurrence_oor(params.occurrence, len(ranges))
                found = ranges[params.occurrence] if ranges else None
            else:
                found = _find_ranges(
                    doc, params.pattern, params.regex, params.case_sensitive, params.use_opts, all_matches=False)
            if found is None:
                return self._finish_search_miss(doc, params)
            if params.position in ("before", "after"):
                res = self._edit_insert_next_to_match(ctx, session, params, found)
            else:
                res = self._edit_replace_one(ctx, session, params, found)

        return res

    def _edit_insert_next_to_match(
        self, ctx: ToolContext, session: EditReviewSession, params: EditParams, found: Any,
    ) -> dict[str, Any]:
        from . import html_import as format_support
        doc = ctx.doc
        content = params.content
        plain = params.plain
        use_preserve = params.use_preserve
        has_table = params.has_table
        occurrence = params.occurrence
        position = params.position
        # INSERT beside the match collapses a cursor at the match edge and runs HTML import
        # there so review mode records only new text, not a tracked delete+reinsert of the
        # unchanged clause.
        #
        # Two guarded gaps (clear error beats an opaque failure; the atomic wrapper would
        # roll back either way): math segments are not wired into the next-to-match insert
        # below, and the HTML import path is not cell-safe (same nested-XText hazard the
        # replace path detects).
        if format_support.html_fragment_contains_mixed_math(str(content)):
            return self._tool_error(
                "position='before'/'after' does not support content with math segments yet; "
                "use position='replace' including the math, or target='end'.",
                code="INVALID_PARAM")
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
        # Invariant: only real newlines make plain content a block; literal backslash-n in markup does not.
        if use_preserve:
            is_block = "\n" in content
        else:
            is_block = format_support._content_has_block_markup(str(content)) or "\n" in str(content)
        text_obj = found.getText()
        if is_block:
            insert_cursor, snapped = _paragraph_boundary_cursor(text_obj, found, position)
        else:
            edge = found.getStart() if position == "before" else found.getEnd()
            insert_cursor, snapped = text_obj.createTextCursorByRange(edge), False

        tables_before = _doc_table_count(doc) if has_table else None

        def _insert_next_to_match() -> None:
            if not is_block:
                format_support.insert_inline_at_cursor(doc, ctx.ctx, insert_cursor, str(content))
            elif position != "after":
                format_support.insert_html_at_cursor(doc, ctx.ctx, insert_cursor, content, apply_styles=False)
            else:
                # Importing at a paragraph END merges the first block into that paragraph; at a
                # paragraph START it does not. Open an empty paragraph and import at its start.
                text_obj.insertControlCharacter(insert_cursor, _PARAGRAPH_BREAK, False)
                # The import puts every block (each with its own break) BEFORE that paragraph,
                # which is then left over empty. This cursor stays at its start through the
                # import (checked live, also with a table next and at the end of the text).
                leftover = text_obj.createTextCursorByRange(insert_cursor.getStart())
                format_support.insert_html_at_cursor(doc, ctx.ctx, insert_cursor, content, apply_styles=False)
                leftover.gotoEndOfParagraph(True)
                if leftover.getString() == "":
                    # Delete the last imported block's break; a tracked insertion swallows it.
                    # Only drop a real break: after a table, goLeft(1) selects the table.
                    leftover.collapseToStart()
                    if leftover.goLeft(1, True):
                        if leftover.getString() == "\n":
                            leftover.setString("")
                        else:
                            leftover.collapseToEnd()
            if self._table_insert_error(doc, has_table, tables_before):
                raise ToolExecutionError(_TABLE_INSERT_FAILED_MSG, code="TABLE_INSERT_FAILED")

        anchor = collapsed_anchor(found)
        with session:
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
        html_import_styles.attach_para_warnings(
            insert_resp, html_import_styles.unapplied_para_names_for_content(str(content))
        )
        return attach_edited_context(insert_resp, anchor)


    def _edit_replace_one(
        self, ctx: ToolContext, session: EditReviewSession, params: EditParams, found: Any,
    ) -> dict[str, Any]:
        doc = ctx.doc
        found = _grow_over_edge_space(found, params.separator)
        # Anchor BEFORE the mutation: the found range's content is replaced (HTML path even
        # deletes-then-imports). edited_context uses this collapsed start. A preserve-format
        # replace's per-character setString pushes a cursor saved here forward, so the
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
        tables_before = _doc_table_count(doc) if params.has_table else None
        doomed = writer_tables_emptied_by_matches([found], params.content)
        if doomed:
            with session:
                tracked_delete = record_table_deletions(session, doc, ctx, doomed)
            return _table_deletion_result([name for _table, name in doomed], tracked_delete)
        style_notes: list[str] = []
        para_failures: list[str] = []
        math_objects: list[dict[str, Any]] = []
        reports, link_err = self._replace_found(
            session, doc, found, use_preserve=params.use_preserve,
            content=params.content, ctx=ctx,
            track_reviewable=params.track_reviewable, override=params.override, batch=False,
            plain=params.plain, style_notes=style_notes,
            para_failures=para_failures,
            fix_outline=params.anchor is None,
            math_out=math_objects)
        if link_err:
            return link_err
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
        err = self._table_insert_error(doc, params.has_table, tables_before)
        if err:
            return err
        _attach_style_notes(resp, style_notes, total_matches=1, doc=doc, params=params)
        reason = next((n for n in style_notes if n in ("STYLE_IGNORED_PARTIAL_MATCH", "STYLE_IGNORED_RECORDING")), None)
        html_import_styles.attach_para_warnings(resp, para_failures, reason=reason)
        self._attach_hyperlink_reports(resp, reports or [])
        res = attach_edited_context(resp, anchor)
        return html_import_styles.attach_math_objects(res, math_objects)
