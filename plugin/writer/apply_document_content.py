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
from dataclasses import dataclass
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
from plugin.writer.table_nesting import _cell_hosted_table_names
from plugin.writer.specialized_base import INSERT_TARGETS, enum_param
from plugin.framework.errors import (
    ToolExecutionError,
    format_error_payload,
    reraise_if_disposed,
    uno_read,
)
from plugin.framework.json_utils import safe_json_loads
from plugin.writer import search as search_mod
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
        if cell is None or _cell_hosted_table_names(cell):
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
) -> dict[str, Any]:
    """Standardized search-replace response dictionary."""
    if replaced_count == 1:
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


# get_document_content reports a paragraph's hand-set formatting as data-lo-para. It is a read
# report, not an instruction: the import path cannot restore Para* when applying a named style,
# so the attribute is dropped. Say so instead of accepting it and doing nothing — a silent no-op
# is exactly the failure this tool's callers get bitten by.
_READ_ONLY_ATTR = "data-lo-para"
# Attribute assignment on a start tag — not body text that happens to mention the name.
_READ_ONLY_ATTR_RE = re.compile(r"""<[A-Za-z][^>]*\bdata-lo-para\s*=""", re.IGNORECASE)


def _note_read_only_attrs(result: Any, content: Any) -> dict[str, Any]:
    """Flag a successful write whose content carried the read-only ``data-lo-para``."""
    if not isinstance(result, dict) or result.get("status") != "ok":
        return result
    items = content if isinstance(content, (list, tuple)) else [content]
    if not any(isinstance(item, str) and _READ_ONLY_ATTR_RE.search(item) for item in items):
        return result
    result = dict(result)
    result["ignored_attributes"] = [_READ_ONLY_ATTR]
    result["message"] = (result.get("message") or "") + (
        " Note: %s in the content was ignored — it is a read-only report of a paragraph's hand-set"
        " formatting. To change an indent or a font, apply a named style (apply_style;"
        " default clear_direct lets the style's font and size show)." % _READ_ONLY_ATTR)
    return result


def _run_on_main_thread(fn: Any) -> Any:
    if main.is_current():
        return fn()
    return main.call(fn, timeout=60.0)


def _search_options(
    kwargs: Any,
    old_stripped: str,
    search_string: str,
) -> tuple[bool, bool, bool, str, str | None]:
    regex = bool(kwargs.get("regex"))
    case = kwargs.get("case_sensitive")
    use_opts = regex or case is not None
    pattern = old_stripped if regex else search_string
    cs = bool(case) if case is not None else False
    rex_err = search_mod.validate_regex_pattern(old_stripped) if regex else None
    return regex, use_opts, cs, pattern, rex_err


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
      **never** pass the full body as ``old_content``. Search uses ``search.find_chained_range`` (LO
      regex + paragraph chaining). See ``tests/writer/test_content_search_uno.py``.
      A match that sits in an outline hyperlink (``#…|outline``) also updates that
      target when the matched text occurs once in the URL outside that suffix, or
      when ``hyperlink_url`` is passed for that one link. ``content`` may equal
      ``old_content`` when only the URL is stale. A repeated title is left
      unchanged. Bookmark targets are not rewritten. See ``hyperlink_fixup``.
    """

    name: str | None = "apply_document_content"
    description: str = (
        "Insert or replace content. "
        f"IMPORTANT: {APPLY_DOCUMENT_CONTENT_TOOL_RESEARCH_HINT} "
        "To replace the ENTIRE document use target='full_document' with content only — "
        "do NOT pass the whole document as old_content. "
        "Use target='beginning', 'end', or 'selection' to insert. "
        "Use target='search' with old_content for find-and-replace of a specific substring only. "
        "An empty replacement that removes the last text in a table deletes that table. "
        "Clearing one cell while another cell still has text leaves the table in place. "
        "Search occurrence is 0-based over replaceable body/table/frame matches only "
        "(not dry_run shape/comment rows); omit it for first-match; do not combine with all_matches=true. "
        "dry_run tags those replaceable rows with occurrence so you can pass the index back. "
        "An outline hyperlink (#…|outline) covering a replaced match is updated when the "
        "matched text occurs once in the target outside that suffix; pass hyperlink_url "
        "to set a different target on that one link, including when content equals "
        "old_content and only the URL is stale. Bookmark links are not rewritten."
    )
    parameters: dict[str, Any] | None = {
        "type": "object",
        "properties": {
            "content": {"type": ["array", "string"], "items": {"type": "string"}, "description": ("List of HTML fragments or plain-text fragments (one per block), or one string. A string is one block; execute still joins a list with newlines. Shape and math per the APPLY_DOCUMENT_CONTENT AND HTML rules — the editing-html guidance covers them if they are not already in your context. No Markdown. An empty string at beginning, end, or selection inserts nothing and returns an error. An empty search replacement deletes the match.")},
            "target": enum_param(INSERT_TARGETS, "Where to apply the content."),
            "old_content": {"type": "string", "description": ("Substring to find when target='search'. Not for whole-document replace — use target='full_document' instead.")},
            "all_matches": {"type": "boolean", "description": "Replace all occurrences (true) or first only. Default false. Only for target='search' with position='replace'."},
            "occurrence": {"type": "integer", "minimum": 0, "description": ("For target='search': 0-based index into replaceable Writer text matches (body/table/frame), not dry_run shape/comment rows. Omit for the existing first-match behavior. Cannot be combined with all_matches=true.")},
            "position": enum_param(
                ["replace", "before", "after"],
                (
                    "For target='search': 'replace' (default) replaces the match; 'before'/'after' INSERT the content next to the match and leave the matched text untouched (result reports inserted=true instead of replaced_count). Inline content (plain text, <b>, <i>, ...) goes at the exact match edge; block content (<p>, headings, lists, tables) goes before/after the whole paragraph that holds the match (result reports snapped_to_paragraph=true when that moved it)."
                ),
            ),
            "dry_run": {"type": "boolean", "description": "For target='search': do NOT edit. Return replaceable matches (each tagged with occurrence) plus shape/comment previews, so you can check before committing."},
            "regex": {"type": "boolean", "description": "For target='search': treat old_content as a regular expression (default false = literal). Regex mode is single-paragraph (no cross-paragraph chaining)."},
            "case_sensitive": {"type": "boolean", "description": "For target='search': force case-sensitive (true) or case-insensitive (false) matching. Omit for the default lenient match."},
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
        changes_before = len(session.changes)
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
            log.exception("apply_document_content: outline hyperlink update failed")
            if outcome.rolled_back or len(session.changes) == changes_before:
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

    def _finish_search_miss(
        self,
        doc: Any,
        search_string: str,
        opts_pattern: str,
        regex_opt: bool,
        opts_cs: bool,
        track_reviewable: bool,
        new_text: str,
        *,
        all_matches: bool,
    ) -> dict[str, Any]:
        shape = search_mod.drawing_shape_object_containing(
            doc, opts_pattern,
            use_regex=regex_opt, case_sensitive=opts_cs)
        if shape is not None:
            shape_name = search_mod._shape_display_name(shape)
            if track_reviewable:
                return {
                    "status": "error",
                    "message": search_mod.shape_only_search_message("old_content", shape_name, review=True),
                    "replaced_count": 0,
                }
            # A False from replace_text_in_shape used to leave a partial setString in
            # the document because the context was closed and never undone.
            def mutate() -> dict[str, Any] | None:
                edited = replace_text_in_shape(shape, search_string, new_text)
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
                    return search_mod.build_search_not_found_response(shape_name=shape_name)
            except UndoUnavailable:
                return self._tool_error(
                    "Cannot edit text inside the drawing shape '%s' atomically "
                    "(no usable undo context); refusing rather than leave an edit "
                    "that is not one Ctrl+Z." % shape_name,
                    code="UNDO_UNAVAILABLE",
                )

            result = attach_edited_context(
                {"status": "ok",
                  "message": ("Replaced 1 occurrence inside drawing shape '%s' (edited the "
                              "shape's own text directly — NOT a tracked change; surrounding "
                              "formatting preserved)." % shape_name),
                  "replaced_count": 1, "review_bypassed": True},
                None)
            return annotate_review_status(result)
        return search_mod.build_search_not_found_response(
            all_matches=all_matches, tracked_deletions=document_has_tracked_deletions(doc))

    def _replace_found(self, session: EditReviewSession, doc: Any, found: Any, *,
                       use_preserve: bool, content: Any,
                       ctx: ToolContext, track_reviewable: bool,
                       override: str | None, batch: bool,
                       plain: str) -> tuple[list[dict[str, Any]] | None, dict[str, Any] | None]:
        """Replace one search match and, when it sits in an outline link, fix that URL.

        *batch* is the all_matches loop, which already holds the undo context. A single
        match opens one when a URL write is needed. Returns ``(reports, error_or_None)``.
        """
        from . import html_import as format_support

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
                    format_support.replace_single_range_with_content(
                        doc, f, content, ctx.ctx)

                session.record_mutation(
                    replace_one,
                    original_preview=original, proposed_preview=plain)
            else:
                record_html_atomically(
                    session, doc,
                    lambda: format_support.replace_single_range_with_content(
                        doc, found, content, ctx.ctx),
                    original_preview=original, proposed_preview=plain)
            if not needs_fix:
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
    def _effective_target(target: Any, old_content: Any) -> Any:
        return "search" if not target and old_content is not None else target

    def _dry_run_preview(self, ctx: ToolContext, **kwargs: Any) -> dict[str, Any]:
        """Resolve old_content matches WITHOUT editing, for target='search'. Reports the count and
        each match's location + a short snippet, so the model can check before committing."""
        old_content = kwargs.get("old_content")
        target = self._effective_target(kwargs.get("target"), old_content)
        if target != "search" or old_content is None:
            return self._tool_error("dry_run only applies to target='search' with old_content.")
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
            return self._tool_error(pos_err)
        override_err = self._override_not_replace(override, position)
        if override_err:
            return self._tool_error(override_err, code="INVALID_PARAM")
        rewrites_outline = position == "replace"
        edit_idx = 0 if occurrence is None else occurrence
        from . import html_import as format_support

        old_stripped = str(old_content).strip()
        s = format_support.plain_text_if_markup(old_stripped)
        s = search_mod.normalize_search_string_for_find(s)
        if not s:
            return self._tool_error("old_content is empty after normalization.")
        regex, use_opts, cs, pattern, rex_err = _search_options(kwargs, old_stripped, s)
        if rex_err:
            return self._invalid_regex(rex_err)
        try:
            ranges = _find_ranges(ctx.doc, pattern, regex, cs, use_opts, all_matches=True)
        except ValueError as e:
            return self._tool_error(str(e), code="INVALID_REGEX")
        except Exception as e:
            log.exception("apply_document_content dry_run search failed")
            return self._tool_error("dry_run search failed: %s" % e, code="SEARCH_FAILED")
        labeler = search_mod.LocationLabeler(ctx.doc)
        matches = []

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

        # occurrence indexes replaceable body/table/frame ranges only — not the
        # mixed matches[] list, which also appends shape/comment previews.
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
            matches.append(row)
        shape_hits = search_mod.sweep_draw_shape_preview_matches(ctx.doc, pattern, regex, cs, limit=search_mod.MAX_SEARCH_HITS)
        comment_hits = search_mod.sweep_comment_preview_matches(ctx.doc, pattern, regex, cs, limit=search_mod.MAX_SEARCH_HITS)
        for item in shape_hits + comment_hits:
            if len(matches) < search_mod.DEFAULT_SEARCH_RESULTS:
                matches.append({"location": item["location"], "text": item["text"][:160]})
        total = len(ranges) + len(shape_hits) + len(comment_hits)
        if occurrence is not None and ranges and occurrence >= len(ranges):
            return self._occurrence_oor(
                occurrence, len(ranges),
                replaceable_count=len(ranges),
                matches=matches,
            )
        result: dict[str, object] = {
            "status": "ok",
            "dry_run": True,
            "count": total,
            "replaceable_count": len(ranges),
            "matches": matches,
            "edit_reach_note": (
                "dry_run counts body/table/frame matches the edit path can replace, plus drawing shapes "
                "and comments (search_in_document uses the same split; only floating shapes are editable "
                "in review-off mode or via the shapes toolset)."),
        }
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

    def execute(self, ctx: ToolContext, **kwargs: Any) -> dict[str, Any]:
        # Thin wrapper so every return path gets the read-only-attribute note, including the
        # review-wait branch that does not go through annotate_review_status.
        return _note_read_only_attrs(self._execute(ctx, **kwargs), kwargs.get("content"))

    def _execute(self, ctx: ToolContext, **kwargs: Any) -> dict[str, Any]:
        on_main = main.is_current()

        if kwargs.get("dry_run"):
            # The preview reads ranges and text through UNO, so a worker-thread call
            # (tool-sync-apply_document_content) marshals it like the edit path does.
            return _run_on_main_thread(lambda: self._dry_run_preview(ctx, **kwargs))
        wait_seconds = self._review_wait_seconds()
        session_box: list[Any] = []

        def _do_edit() -> tuple[dict[str, Any], Any]:
            params, err = self._parse_edit_params(kwargs)
            if err is not None:
                return err, None
            if params is None:
                return self._tool_error("Internal error: missing edit parameters."), None
            session = EditReviewSession(ctx.doc, ctx.ctx, enabled=params.track_reviewable)
            session_box.append(session)
            return self._dispatch_edit(ctx, session, params), session

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
        scope = getattr(ctx, "send_cancellation", None)

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

    def _parse_edit_params(self, kwargs: Any) -> tuple[EditParams | None, dict[str, Any] | None]:
        from . import html_import as format_support
        content = kwargs.get("content", "")
        old_content = kwargs.get("old_content")
        target = self._effective_target(kwargs.get("target"), old_content)
        if not target:
            return None, self._tool_error(
                "Provide a target ('beginning', 'end', 'selection', 'full_document', 'search') "
                "or old_content for find-and-replace.")

        if target == "search" and old_content is None:
            return None, self._tool_error("target='search' requires old_content.")

        occurrence, occurrence_error = self._parse_occurrence(kwargs, target)
        if occurrence_error:
            return None, self._tool_error(occurrence_error, code="INVALID_PARAM")

        # position is a search-only refinement; validate it up front (silently ignoring it on an
        # insert target would teach the model a parameter that "works" by accident).
        position, pos_err = self._position_choice(kwargs)
        if pos_err:
            return None, self._tool_error(pos_err)
        if position != "replace" and target != "search":
            return None, self._tool_error(
                "position='before'/'after' requires target='search' "
                "(it inserts next to an old_content match).")
        if position != "replace" and kwargs.get("all_matches", False):
            return None, self._tool_error(
                "position='before'/'after' inserts at a single match; drop all_matches "
                "or use position='replace'.")

        if isinstance(content, str):
            stripped = content.strip()
            if stripped.startswith("[") and "<" in stripped:
                parsed = safe_json_loads(stripped)
                if isinstance(parsed, list):
                    content = parsed

        if isinstance(content, list):
            if any(not isinstance(x, str) for x in content):
                return None, self._tool_error("content list items must be strings.")
            content = "\n".join(content)

        if not isinstance(content, str):
            return None, self._tool_error("content must be a string or a list of strings.")

        if target in ("beginning", "end", "selection") and content.strip() == "":
            return None, self._tool_error("content is empty; nothing was inserted.")

        # Detect markup BEFORE any HTML wrapping.
        use_preserve = not format_support.content_has_markup(content)

        if use_preserve:
            content = content.replace("\\n", "\n").replace("\\t", "\t")
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

        plain = ""
        if target != "search":
            plain = content if use_preserve else self._replacement_plain(content)

        has_table = bool(re.search(r"<table\b", str(content), re.IGNORECASE))
        all_matches = kwargs.get("all_matches", False)

        if target != "search":
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
            ), None

        old_stripped = str(old_content).strip()
        search_string = format_support.plain_text_if_markup(old_stripped)
        # Collapse exotic horizontal whitespace; preserve newlines for paragraph-aware search.
        search_string = search_mod.normalize_search_string_for_find(search_string)
        if not search_string:
            # Parameter error (like old_content=None), not a search no-op: the search never ran,
            # so there's no replaced_count to report — use the standard tool error shape.
            return None, self._tool_error("old_content is empty after normalization.")
        plain = content if use_preserve else self._replacement_plain(content)
        # The search drops the spaces at old_content's edges. The replacement
        # must drop the same edges, or old_content=" paragrafo" with content=""
        # deletes only "paragrafo" and leaves "Primeiro  inteiro." with a
        # double space (same for " paragrafo inteiro" -> " inteiro"). A
        # deletion takes the space on one side along with the word.
        separator = 0
        # Invariant: branch on use_preserve, not on the decoded content (an entity may decode to markup).
        if (position == "replace" and not kwargs.get("regex")
                and use_preserve
                and not format_support.content_has_markup(str(old_content))):
            content, separator = _match_stripped_edges(str(old_content), content)
            plain = content
        override, override_error = self._parse_hyperlink_url(kwargs)
        if override_error:
            return None, self._tool_error(override_error, code="INVALID_PARAM")
        override_err = self._override_not_replace(override, position)
        if override_err:
            return None, self._tool_error(override_err, code="INVALID_PARAM")

        regex, use_opts, cs, pattern, rex_err = _search_options(kwargs, old_stripped, search_string)
        if rex_err:
            return None, self._invalid_regex(rex_err)

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
        ), None

    def _dispatch_edit(
        self, ctx: ToolContext, session: EditReviewSession, params: EditParams,
    ) -> dict[str, Any]:
        if params.target == "full_document":
            return self._edit_full_document(ctx, session, params)
        if params.target in ("end", "beginning"):
            return self._edit_insert_at(ctx, session, params)
        if params.target == "selection":
            return self._edit_selection(ctx, session, params)
        return self._edit_search(ctx, session, params)

    def _edit_full_document(
        self, ctx: ToolContext, session: EditReviewSession, params: EditParams,
    ) -> dict[str, Any]:
        from . import html_import as format_support
        content = params.content
        plain = params.plain
        has_table = params.has_table
        with session:
            # Delete-then-import: make it atomic so a failed import can't strand a cleared document.
            record_html_atomically(
                session, ctx.doc,
                lambda: format_support.replace_full_document(ctx.doc, ctx.ctx, content),
                proposed_preview=plain)
        err = self._table_insert_error(ctx.doc, has_table, None)
        if err:
            return err
        return {"status": "ok", "message": "Replaced entire document."}

    def _edit_insert_at(
        self, ctx: ToolContext, session: EditReviewSession, params: EditParams,
    ) -> dict[str, Any]:
        from . import html_import as format_support
        content = params.content
        plain = params.plain
        has_table = params.has_table
        target = params.target
        tables_before = _doc_table_count(ctx.doc) if has_table else None
        with session:
            session.record_mutation(
                lambda: format_support.insert_content_at_position(ctx.doc, ctx.ctx, content, target),
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
        return attach_edited_context(
            {"status": "ok", "message": msg}, anchor)

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
        with session:
            # Selection insert clears the selection first, then imports -> atomic (full_document above).
            record_html_atomically(
                session, ctx.doc,
                lambda: format_support.insert_content_at_position(ctx.doc, ctx.ctx, content, "selection"),
                proposed_preview=plain)
        err = self._table_insert_error(ctx.doc, has_table, tables_before)
        if err:
            return err
        return attach_edited_context(
            {"status": "ok", "message": "Inserted content at selection."}, anchor)


    def _edit_replace_all(
        self, ctx: ToolContext, session: EditReviewSession, params: EditParams,
    ) -> dict[str, Any]:
        doc = ctx.doc
        content = params.content
        plain = params.plain
        use_preserve = params.use_preserve
        track_reviewable = params.track_reviewable
        override = params.override
        pattern = params.pattern
        regex = params.regex
        cs = params.case_sensitive
        use_opts = params.use_opts
        search_string = params.search_string
        separator = params.separator
        # replaced_count is the machine-readable success signal: 0 -> status "error" (a silent
        # no-op surfaced as a failure), N>0 -> "ok". No matched_count/warning/partial-replace:
        # if a replace raises mid-all_matches the existing abort behavior stands.
        # Explicit regex / case control (opt-in): a direct SearchDescriptor find that bypasses the
        # default lenient matcher, so "search with options then replace" agrees with search_in_document.
        ranges = _find_ranges(doc, pattern, regex, cs, use_opts, all_matches=True)
        if not ranges:
            return self._finish_search_miss(
                doc, search_string, pattern, regex, cs,
                track_reviewable, plain, all_matches=True)
        max_limit_hit = len(ranges) >= search_mod.MAX_SEARCH_REPLACEMENTS
        # Decide before any replace. An empty replacement that is the last text
        # in a table deletes the table instead of leaving an empty shell; those
        # matches are not also cleared as text (that would add a second redline).
        doomed = writer_tables_emptied_by_matches(ranges, content)
        doomed_names = {name for _table, name in doomed}
        if doomed_names:
            anchor_range = next((item for item in ranges if _table_and_name(item)[1] not in doomed_names), None)
            anchor = collapsed_anchor(anchor_range) if anchor_range is not None else None
        else:
            anchor = collapsed_anchor(ranges[0])
        changes_before = len(session.changes)
        count = 0
        link_failure_holder: list[dict[str, Any] | None] = [None]
        link_reports: list[dict[str, Any]] = []
        tracked_delete_box = [False]

        def mutate() -> None:
            nonlocal count
            with session:
                for found in reversed(ranges):
                    if doomed_names and _table_and_name(found)[1] in doomed_names:
                        continue
                    found = _grow_over_edge_space(found, separator)
                    # batch=True: this loop already holds the undo context, so the
                    # outline URL write stays in the same Ctrl+Z as the text.
                    reports, link_err = self._replace_found(
                        session, doc, found, use_preserve=use_preserve,
                        content=content, ctx=ctx,
                        track_reviewable=track_reviewable, override=override, batch=True,
                        plain=plain)
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
            is_rolled_back = bool(outcome.rolled_back or len(session.changes) == changes_before)
            if is_rolled_back:
                payload["message"] = "all_matches aborted after %d replacement(s); document rolled back (%s)." % (count, e)
            else:
                payload["message"] = "all_matches aborted after %d replacement(s); the document could not be rolled back and may be partially edited (%s)." % (count, e)
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
                "code": "TOOL_EXECUTION_ERROR",
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
            resp = build_search_replace_response(count, use_preserve=use_preserve)
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
        self._attach_hyperlink_reports(resp, link_reports)
        return attach_edited_context(resp, anchor)

    def _edit_search(
        self, ctx: ToolContext, session: EditReviewSession, params: EditParams,
    ) -> dict[str, Any]:
        if params.all_matches:
            return self._edit_replace_all(ctx, session, params)
        doc = ctx.doc
        occurrence = params.occurrence
        pattern = params.pattern
        regex = params.regex
        cs = params.case_sensitive
        use_opts = params.use_opts
        search_string = params.search_string
        plain = params.plain
        track_reviewable = params.track_reviewable
        position = params.position
        if occurrence is not None:
            # Index the same replaceable list the edit path uses. Empty ranges fall
            # through to the existing miss / drawing-shape path (models often send 0).
            ranges = _find_ranges(doc, pattern, regex, cs, use_opts, all_matches=True)
            if ranges and occurrence >= len(ranges):
                return self._occurrence_oor(occurrence, len(ranges))
            found = ranges[occurrence] if ranges else None
        else:
            found = _find_ranges(doc, pattern, regex, cs, use_opts, all_matches=False)
        if found is None:
            return self._finish_search_miss(
                doc, search_string, pattern, regex, cs,
                track_reviewable, plain, all_matches=False)
        if position in ("before", "after"):
            return self._edit_insert_next_to_match(ctx, session, params, found)
        return self._edit_replace_one(ctx, session, params, found)

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
        # INSERT next to the match instead of replacing it: the single most common petition
        # edit ("add a paragraph after clause X") previously forced resending the clause
        # itself in content — which in record/wait rendered as a tracked delete+reinsert of
        # text nobody touched. Collapse a cursor at the match edge and run the normal
        # HTML-import insert there; only the genuinely new text enters the review record.
        #
        # Two guarded gaps (clear error beats an opaque failure; the atomic wrapper would
        # roll back either way): math segments are not wired into the next-to-match insert
        # below, and the HTML import path is not cell-safe (same nested-XText hazard the
        # replace path detects).
        if format_support.html_fragment_contains_mixed_math(str(content)):
            return self._tool_error(
                "position='before'/'after' does not support content with math segments yet; "
                "use position='replace' including the math, or target='end'.")
        # TextTable is None outside a cell. A throw is a closed document;
        # do not treat it as "not a cell" and run the cell-unsafe import.
        in_cell = found.getText().createTextCursorByRange(
            found.getStart()).getPropertyValue("TextTable") is not None
        if in_cell:
            return self._tool_error(
                "position='before'/'after' next to a match inside a table cell is not "
                "supported yet; use position='replace' with plain text, or rewrite the "
                "cell content.")
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
        reports, link_err = self._replace_found(
            session, doc, found, use_preserve=params.use_preserve,
            content=params.content, ctx=ctx,
            track_reviewable=params.track_reviewable, override=params.override, batch=False,
            plain=params.plain)
        if link_err:
            return link_err
        resp = build_search_replace_response(1, use_preserve=params.use_preserve)
        if params.occurrence is not None:
            resp["occurrence"] = params.occurrence
        err = self._table_insert_error(doc, params.has_table, tables_before)
        if err:
            return err
        self._attach_hyperlink_reports(resp, reports or [])
        return attach_edited_context(resp, anchor)
