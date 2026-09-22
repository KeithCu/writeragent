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
"""Writer content tools — read, apply, and paragraph operations.

LO findFirst / chained-regex helpers live in ``plugin.writer.search``.
"""

import logging
import re
import threading
import time

from plugin.framework.tool import ToolBase
from plugin.framework.prompts import APPLY_DOCUMENT_CONTENT_TOOL_RESEARCH_HINT
from plugin.doc.text_helpers import collect_tracked_changes
from plugin.writer.edit_review import (
    EditReviewSession,
    edit_review_wait_seconds,
    review_recording_enabled,
    get_agent_edit_review_mode,
    record_preserve_replace,
    record_html_atomically,
    collapsed_anchor,
    selection_anchor,
    attach_edited_context,
    next_agent_edit_undo_title,
    close_surgical_context,
)
from plugin.writer.specialized.shapes import replace_text_in_shape
from plugin.framework.errors import safe_json_loads, ToolExecutionError
from plugin.writer import search as search_mod
from plugin.writer.hyperlink_fixup import (
    capture_outline_hyperlinks,
    empty_snapshot,
    plan_outline_updates,
    public_hyperlink_reports,
    restore_outline_hyperlinks,
)


log = logging.getLogger("writeragent.writer")




# ------------------------------------------------------------------
# GetDocumentContent
# ------------------------------------------------------------------


class GetDocumentContent(ToolBase):
    """Export the document (or a portion) as formatted content."""

    name = "get_document_content"
    description = (
        "Get document (or selection/range) content. Result includes document_length. "
        "scope: full, selection, or range (requires start, end). "
        "CJK ruby (furigana) is exported as HTML <ruby>base<rt>reading</rt></ruby>."
    )
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
        t0 = time.perf_counter()
        scope = kwargs.get("scope", "full")
        max_chars = kwargs.get("max_chars")
        range_start = kwargs.get("start") if scope == "range" else None
        range_end = kwargs.get("end") if scope == "range" else None
        log.debug("get_document_content: start scope=%r max_chars=%r", scope, max_chars)

        if scope == "range" and (range_start is None or range_end is None):
            return self._tool_error("scope 'range' requires start and end.")

        include_images = bool(kwargs.get("include_images", False))
        walk_warnings: list[str] = []
        content = format_support.document_to_content(
            ctx.doc,
            ctx.ctx,
            ctx.services,
            max_chars=max_chars,
            scope=scope,
            range_start=range_start,
            range_end=range_end,
            include_images=include_images,
            walk_warnings=walk_warnings,
        )
        doc_len = ctx.services.document.get_document_length(ctx.doc)
        result = {"status": "ok", "content": content, "length": len(content), "document_length": doc_len}
        if walk_warnings:
            result["warning"] = walk_warnings[0]
        # Machine-readable truncation signal: without it the only clue was the in-band marker
        # string, which a model must know to look for. (length counts HTML chars; document_length
        # and scope='range' offsets are plain-text chars — use those for follow-up range reads.)
        if max_chars and isinstance(content, str) and content.endswith("[... truncated ...]"):
            result["truncated"] = True
        if scope == "range" and range_start is not None and range_end is not None:
            result["start"] = int(range_start)
            result["end"] = int(range_end)

        # The HTML content above hides tracked deletions and gives no sign that changes are pending.
        # When the document has tracked changes, surface them explicitly (insertion vs deletion, with
        # text) and say they await the user's review — so the model treats them as pending, not errors,
        # and never resolves them itself.
        n_tracked = 0
        try:
            t_tracked = time.perf_counter()
            if hasattr(ctx.doc, "getRedlines") and ctx.doc.getRedlines().getCount() > 0:
                changes = collect_tracked_changes(ctx.doc.getText())
                if changes:
                    n_tracked = len(changes)
                    result["tracked_changes"] = changes
                    result["tracked_changes_note"] = (
                        "This document has %d change(s) recorded as tracked changes (listed in "
                        "tracked_changes as insertions/deletions). They are PENDING the user's review — "
                        "not errors and not yet final. Do NOT accept or reject them yourself; that is the "
                        "user's decision." % len(changes)
                    )
            log.debug(
                "get_document_content: phase=tracked_changes elapsed_ms=%.1f n_tracked=%d",
                (time.perf_counter() - t_tracked) * 1000.0,
                n_tracked,
            )
        except Exception:
            log.debug("get_document_content: could not collect tracked changes", exc_info=True)
        log.debug(
            "get_document_content: done scope=%r content_len=%d document_length=%d n_tracked=%d total_ms=%.1f",
            scope,
            len(content) if isinstance(content, str) else -1,
            doc_len,
            n_tracked,
            (time.perf_counter() - t0) * 1000.0,
        )
        return result


# ------------------------------------------------------------------
# ApplyDocumentContent
# ------------------------------------------------------------------

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


def _note_read_only_attrs(result, content):
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
      **never** pass the full body as ``old_content``. Search uses ``search.find_chained_range`` (LO
      regex + paragraph chaining). See ``tests/writer/test_content_search_uno.py``.
      A match that sits in an outline hyperlink (``#…|outline``) also updates that
      target when the matched text occurs once in the URL outside that suffix, or
      when ``hyperlink_url`` is passed for that one link. ``content`` may equal
      ``old_content`` when only the URL is stale. A repeated title is left
      unchanged. Bookmark targets are not rewritten. See ``hyperlink_fixup``.
    """

    name = "apply_document_content"
    description = (
        "Insert or replace content. "
        f"IMPORTANT: {APPLY_DOCUMENT_CONTENT_TOOL_RESEARCH_HINT} "
        "To replace the ENTIRE document use target='full_document' with content only — "
        "do NOT pass the whole document as old_content. "
        "Use target='beginning', 'end', or 'selection' to insert. "
        "Use target='search' with old_content for find-and-replace of a specific substring only. "
        "Search occurrence is 0-based over replaceable body/table/frame matches only "
        "(not dry_run shape/comment rows); omit it for first-match; do not combine with all_matches=true. "
        "dry_run tags those replaceable rows with occurrence so you can pass the index back. "
        "An outline hyperlink (#…|outline) covering a replaced match is updated when the "
        "matched text occurs once in the target outside that suffix; pass hyperlink_url "
        "to set a different target on that one link, including when content equals "
        "old_content and only the URL is stale. Bookmark links are not rewritten."
    )
    parameters = {
        "type": "object",
        "properties": {
            "content": {"type": "array", "items": {"type": "string"}, "description": ("List of HTML fragments or plain-text fragments (one per block); shape and math per the APPLY_DOCUMENT_CONTENT AND HTML rules — the editing-html guidance covers them if they are not already in your context. No Markdown.")},
            "target": {"type": "string", "enum": ["beginning", "end", "selection", "full_document", "search"], "description": "Where to apply the content."},
            "old_content": {"type": "string", "description": ("Substring to find when target='search'. Not for whole-document replace — use target='full_document' instead.")},
            "all_matches": {"type": "boolean", "description": "Replace all occurrences (true) or first only. Default false. Only for target='search' with position='replace'."},
            "occurrence": {"type": "integer", "minimum": 0, "description": ("For target='search': 0-based index into replaceable Writer text matches (body/table/frame), not dry_run shape/comment rows. Omit for the existing first-match behavior. Cannot be combined with all_matches=true.")},
            "position": {"type": "string", "enum": ["replace", "before", "after"], "description": ("For target='search': 'replace' (default) replaces the match; 'before'/'after' INSERT the content next to the match and leave the matched text untouched (result reports inserted=true instead of replaced_count).")},
            "dry_run": {"type": "boolean", "description": "For target='search': do NOT edit. Return replaceable matches (each tagged with occurrence) plus shape/comment previews, so you can check before committing."},
            "regex": {"type": "boolean", "description": "For target='search': treat old_content as a regular expression (default false = literal). Regex mode is single-paragraph (no cross-paragraph chaining)."},
            "case_sensitive": {"type": "boolean", "description": "For target='search': force case-sensitive (true) or case-insensitive (false) matching. Omit for the default lenient match."},
            "hyperlink_url": {"type": "string", "description": ("For target='search' with position='replace', when the match overlaps exactly one outline hyperlink (#…|outline): set this exact URL instead of substituting the matched text. content may equal old_content when the visible title is already correct and only the URL is stale. Omit it to update that target when the matched text occurs once outside the |outline suffix. Rejected with all_matches=true, with position='before'/'after', and when the match is not exactly one outline link. Bookmark targets are never rewritten.")},
        },
        "required": ["content"],
    }
    uno_services = ["com.sun.star.text.TextDocument"]
    tier = "core"
    is_mutation = True

    @staticmethod
    def _parse_occurrence(kwargs, target):
        """Validate optional 0-based search occurrence selector."""
        raw = kwargs.get("occurrence")
        if raw is None:
            return None, None
        if target != "search":
            return None, "occurrence only applies to target='search'."
        if isinstance(raw, bool) or not isinstance(raw, int) or raw < 0:
            return None, "occurrence must be a non-negative integer."
        if kwargs.get("all_matches", False):
            return None, "occurrence cannot be combined with all_matches=true."
        return raw, None

    @staticmethod
    def _parse_hyperlink_url(kwargs):
        """Explicit outline-target override. None means use substitution when it applies."""
        raw = kwargs.get("hyperlink_url")
        if raw is None:
            return None, None
        if not isinstance(raw, str):
            return None, "hyperlink_url must be a string."
        if not raw.strip():
            return None, None
        if kwargs.get("all_matches", False):
            return None, "hyperlink_url cannot be combined with all_matches=true."
        return raw, None

    def _replacement_plain(self, ctx, content):
        """Visible text a search replace will insert, for outline-URL substitution."""
        from . import format as format_support

        if isinstance(content, list):
            content = "\n".join(str(part) for part in content)
        text = "" if content is None else str(content)
        if format_support.content_has_markup(text):
            return format_support.html_to_plain_text(text, ctx.ctx, ctx.services.get("config"))
        return text.replace("\\n", "\n").replace("\\t", "\t")

    def _outline_override_error(self):
        return self._tool_error(
            "hyperlink_url applies only when the match overlaps exactly one outline hyperlink (|outline).",
            code="INVALID_PARAM")

    @staticmethod
    def _override_span_rejected(fields):
        """True when an override is not aimed at exactly one outline link.

        Zero links omit hyperlink_url. Two links also set hyperlinks, and previewing
        the first of them would look like the override applies to that one alone.
        """
        many = fields.get("hyperlinks")
        if many:
            return len(many) != 1
        return not fields.get("hyperlink_url")

    def _outline_match_fields(self, found, plain_fn, override):
        """dry_run fields for one match. Empty when the match has no outline link.

        *plain_fn* is called only when a preview is actually needed, so a markup dry_run
        that does not touch an outline link does not open a hidden document. An override
        on a non-outline match is not previewed as a new link; the caller rejects it.
        """
        try:
            snapshot = capture_outline_hyperlinks(found)
        except Exception:
            log.debug("apply_document_content: outline hyperlink preview failed", exc_info=True)
            return {}
        if not snapshot.links:
            return {}
        plain = plain_fn()
        plans = plan_outline_updates(snapshot.links, snapshot.matched, plain, override)
        reports = public_hyperlink_reports(plans)
        if not reports:
            return {}
        fields = {
            "hyperlink_url": reports[0]["hyperlink_url"],
            "hyperlink_url_after": reports[0]["hyperlink_url_after"],
            "hyperlink_updated": reports[0]["hyperlink_updated"],
        }
        if len(reports) > 1:
            fields["hyperlinks"] = reports
        return fields

    def _attach_hyperlink_reports(self, resp, reports):
        if not reports:
            return resp
        resp["hyperlinks"] = reports
        if len(reports) == 1:
            resp["hyperlink_url"] = reports[0]["hyperlink_url"]
            resp["hyperlink_url_after"] = reports[0]["hyperlink_url_after"]
            resp["hyperlink_updated"] = reports[0]["hyperlink_updated"]
        return resp

    def _outline_undo(self, doc, session, run):
        """Run *run* inside one undo step so the text replace and the URL write are one Ctrl+Z.

        The HTML path and a surgical review replace each close their own undo context
        before returning. Without this outer step the URL write would be a second undo.
        """
        undo_title = next_agent_edit_undo_title()
        try:
            mgr = doc.getUndoManager()
            if mgr is None or mgr.isLocked():
                raise RuntimeError("undo manager is locked")
            mgr.enterUndoContext(undo_title)
        except Exception:
            log.exception("apply_document_content: outline hyperlink undo context failed")
            return None, self._tool_error(
                "Cannot update the outline hyperlink atomically (no usable undo context); "
                "refusing rather than risk a text change that leaves the link stale.",
                code="UNDO_UNAVAILABLE")
        changes_before = len(session.changes)
        applied_ok = False
        try:
            reports = run()
            applied_ok = True
            return reports, None
        except Exception as exc:
            log.exception("apply_document_content: outline hyperlink update failed")
            return None, self._tool_error(
                "Outline hyperlink update failed; the text change was rolled back (%s)." % exc,
                code="HYPERLINK_UPDATE_FAILED")
        finally:
            close_surgical_context(mgr, session, changes_before, applied_ok, undo_title)

    def _replace_found(self, session, doc, found, *, use_preserve, raw_content, content,
                       ctx, config_svc, track_reviewable, override, batch, plain_preview):
        """Replace one search match and, when it sits in an outline link, fix that URL.

        *batch* is the all_matches loop, which already holds the undo context. A single
        match opens one when a URL write is needed. Returns ``(reports, error_or_None)``.
        """
        from . import format as format_support

        anchor = collapsed_anchor(found)
        try:
            snapshot = capture_outline_hyperlinks(found)
        except Exception:
            log.exception("apply_document_content: outline hyperlink capture failed")
            if override:
                return None, self._tool_error(
                    "Could not read the outline hyperlink on this match.",
                    code="HYPERLINK_UPDATE_FAILED")
            snapshot = empty_snapshot()
        if override and len(snapshot.links) != 1:
            # Before the text edit. One override on two outline links would stamp
            # both with the same target. Zero links would paint a bookmark or plain text.
            return None, self._outline_override_error()
        # preserve_url: a bookmark target covering the match. A same-length setString
        # keeps HyperLinkURL. HTML setString("") and a length-changing preserve-format
        # replace can clear it on the new characters; put that target back unchanged.
        needs_fix = bool(snapshot.links) or bool(snapshot.preserve_url)

        def mutate():
            original = found.getString()
            if use_preserve:
                record_preserve_replace(session, doc, found, raw_content, ctx.ctx, track_reviewable)
            elif batch:
                # The all_matches loop already entered an undo context. A second
                # record_html_atomically would nest another and close it early.
                session.record_mutation(
                    lambda f=found: format_support.replace_single_range_with_content(
                        doc, f, content, ctx.ctx, config_svc),
                    original_preview=original, proposed_preview=plain_preview)
            else:
                record_html_atomically(
                    session, doc,
                    lambda: format_support.replace_single_range_with_content(
                        doc, found, content, ctx.ctx, config_svc),
                    track_reviewable, original_preview=original, proposed_preview=plain_preview)

        def run():
            mutate()
            if not needs_fix:
                return []
            fallback = raw_content if use_preserve else self._replacement_plain(ctx, content)
            return restore_outline_hyperlinks(anchor, snapshot, fallback, override)

        def once():
            # all_matches already entered the review session around the loop.
            # A single match has to enter it here, outline link or not, or record
            # mode would only track replaces that happen to sit in an outline URL.
            if batch:
                return run()
            with session:
                return run()

        if needs_fix and not batch:
            # Group the text replace and the URL write. The session stays around both
            # so review recording still sees the mutation.
            return self._outline_undo(doc, session, once)
        return once(), None

    @staticmethod
    def _occurrence_oor_message(occurrence, count):
        """LLMs guess 1-based; say the valid 0-based range like apply_style."""
        return "occurrence %s out of range (found %d match(es), use 0..%d)." % (
            occurrence, count, count - 1)

    def _occurrence_oor(self, occurrence, count, **details):
        return self._tool_error(
            self._occurrence_oor_message(occurrence, count),
            code="OCCURRENCE_OUT_OF_RANGE",
            count=count,
            **details,
        )

    def _review_wait_seconds(self, uno_ctx):
        """Max seconds the edit call should block waiting for review; 0 = don't wait."""
        try:
            return edit_review_wait_seconds(uno_ctx)
        except Exception:
            return 0

    def _annotate_review_status(self, uno_ctx, result):
        """Tag a successful edit result with the CURRENT review status, so the model gets a fresh,
        per-call signal even if the guidance it read earlier (the connect-time pointer, a pulled
        review-modes topic, or the sidebar prompt) is stale — that text is static while the user
        can toggle the mode mid-session. Only annotates the non-wait path:
        with recording on but no blocking wait, the edit landed as a tracked change the agent must
        not resolve and whose accept/reject outcome it will not be told."""
        if not isinstance(result, dict) or result.get("status") != "ok":
            return result
        try:
            mode = get_agent_edit_review_mode(uno_ctx)
        except Exception:
            return result
        if mode not in ("record", "wait"):
            return result  # off -> edit is live; nothing to add
        result = dict(result)
        result["review_mode"] = mode
        result["pending_review"] = True
        result["message"] = (result.get("message") or "") + (
            " Applied as a tracked change pending the user's review — do not accept or reject it"
            " yourself, and you will not be notified whether it is later accepted or rejected."
        )
        return result

    def _wait_enabled_globally(self):
        """Config read without a tool context, for long_running/is_async (called by the
        MCP/chat shells before execute). False whenever the context isn't available."""
        try:
            from plugin.framework.thread_guard import on_main_thread
            from plugin.framework.uno_context import get_ctx

            ctx = get_ctx() if on_main_thread() else None
            return self._review_wait_seconds(ctx) > 0
        except Exception:
            return False

    @property
    def long_running(self) -> bool:  # type: ignore[override]  # pyright: ignore[reportIncompatibleVariableOverride]
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

    def _dry_run_preview(self, ctx, **kwargs):
        """Resolve old_content matches WITHOUT editing, for target='search'. Reports the count and
        each match's location + a short snippet, so the model can check before committing."""
        target = kwargs.get("target")
        old_content = kwargs.get("old_content")
        if not target and old_content is not None:
            target = "search"
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
        position = str(kwargs.get("position") or "replace").strip().lower()
        if position not in ("replace", "before", "after"):
            return self._tool_error("position must be 'replace', 'before' or 'after'.")
        if override and position != "replace":
            return self._tool_error(
                "hyperlink_url only applies to position='replace'.",
                code="INVALID_PARAM")
        rewrites_outline = position == "replace"
        edit_idx = 0 if occurrence is None else occurrence
        from . import format as format_support

        old_stripped = str(old_content).strip()
        s = old_stripped
        if format_support.content_has_markup(s):
            s = format_support.html_to_plain_text(s, ctx.ctx, ctx.services.get("config"))
        s = search_mod.normalize_search_string_for_find(s)
        if not s:
            return self._tool_error("old_content is empty after normalization.")
        use_regex = bool(kwargs.get("regex"))
        case_opt = kwargs.get("case_sensitive")
        if use_regex:
            rex_err = search_mod.validate_regex_pattern(old_stripped)
            if rex_err:
                return self._tool_error(search_mod.invalid_regex_tool_message(rex_err), code="INVALID_REGEX", count=0)
        try:
            if use_regex or case_opt is not None:
                ranges = search_mod.find_ranges_regex_case(
                    ctx.doc, old_stripped if use_regex else s, use_regex,
                    bool(case_opt) if case_opt is not None else False, all_matches=True)
            else:
                ranges = search_mod.find_all_ranges(ctx.doc, s)
        except ValueError as e:
            return self._tool_error(str(e), code="INVALID_REGEX")
        except Exception as e:
            log.exception("apply_document_content dry_run search failed")
            return self._tool_error("dry_run search failed: %s" % e, code="SEARCH_FAILED")
        label_cache = {}
        matches = []
        plain_box: dict[str, str] = {}

        def _plain():
            if "value" not in plain_box:
                plain_box["value"] = self._replacement_plain(ctx, kwargs.get("content"))
            return plain_box["value"]

        # occurrence indexes replaceable body/table/frame ranges only — not the
        # mixed matches[] list, which also appends shape/comment previews.
        for idx, found in enumerate(ranges[:20]):
            try:
                loc = search_mod.describe_match_location(found, ctx.doc, label_cache=label_cache)
            except Exception:
                loc = "body"
            try:
                snippet = found.getString()
            except Exception:
                snippet = ""
            row = {"occurrence": idx, "location": loc, "text": snippet[:160]}
            if rewrites_outline:
                # The override is one destination for the match execute would edit.
                # Other rows show automatic substitution only.
                row_override = override if idx == edit_idx else None
                fields = self._outline_match_fields(found, _plain, row_override)
                if row_override and self._override_span_rejected(fields):
                    return self._outline_override_error()
                row.update(fields)
            matches.append(row)
        opts_cs = bool(case_opt) if case_opt is not None else False
        pattern = old_stripped if use_regex else s
        shape_hits = search_mod.sweep_draw_shape_preview_matches(ctx.doc, pattern, use_regex, opts_cs, limit=10000)
        comment_hits = search_mod.sweep_comment_preview_matches(ctx.doc, pattern, use_regex, opts_cs, limit=10000)
        for item in shape_hits + comment_hits:
            if len(matches) < 20:
                matches.append({"location": item["location"], "text": item["text"][:160]})
        total = len(ranges) + len(shape_hits) + len(comment_hits)
        if occurrence is not None and ranges and occurrence >= len(ranges):
            return self._occurrence_oor(
                occurrence, len(ranges),
                replaceable_count=len(ranges),
                matches=matches,
            )
        # The preview loop only walks the first 20 matches. A later occurrence is
        # still the one execute would edit, so the override has to be checked there too.
        if (rewrites_outline and override and ranges and edit_idx < len(ranges)
                and edit_idx >= 20):
            later = self._outline_match_fields(ranges[edit_idx], _plain, override)
            if self._override_span_rejected(later):
                return self._outline_override_error()
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
            try:
                selected_location = search_mod.describe_match_location(
                    selected, ctx.doc, label_cache=label_cache)
            except Exception:
                selected_location = "body"
            try:
                selected_text = selected.getString()
            except Exception:
                selected_text = ""
            result["selected_occurrence"] = occurrence
            selected_match = {
                "location": selected_location,
                "text": selected_text[:160],
            }
            if rewrites_outline:
                selected_match.update(self._outline_match_fields(selected, _plain, override))
            result["selected_match"] = selected_match
        return result

    def execute(self, ctx, **kwargs):
        # Thin wrapper so every return path gets the read-only-attribute note, including the
        # review-wait branch that does not go through _annotate_review_status.
        return _note_read_only_attrs(self._execute(ctx, **kwargs), kwargs.get("content"))

    def _execute(self, ctx, **kwargs):
        if kwargs.get("dry_run"):
            return self._dry_run_preview(ctx, **kwargs)
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
                    result, _unused = _do_edit()
                else:
                    from plugin.framework.queue_executor import execute_on_main_thread
                    result, _unused = execute_on_main_thread(_do_edit, timeout=60.0)
                return self._annotate_review_status(ctx.ctx, result)
            except ToolExecutionError as e:
                return self._tool_error(str(e))
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
        except ToolExecutionError as e:
            if session_box:
                execute_on_main_thread(session_box[0].cleanup)
            return self._tool_error(str(e))
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

        occurrence, occurrence_error = self._parse_occurrence(kwargs, target)
        if occurrence_error:
            return self._tool_error(occurrence_error, code="INVALID_PARAM"), None

        # position is a search-only refinement; validate it up front (silently ignoring it on an
        # insert target would teach the model a parameter that "works" by accident).
        position = str(kwargs.get("position") or "replace").strip().lower()
        if position not in ("replace", "before", "after"):
            return self._tool_error("position must be 'replace', 'before' or 'after'."), None
        if position != "replace" and target != "search":
            return self._tool_error(
                "position='before'/'after' requires target='search' (it inserts next to an old_content match)."), None
        if position != "replace" and kwargs.get("all_matches", False):
            return self._tool_error(
                "position='before'/'after' inserts at a single match; drop all_matches or use position='replace'."), None

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
                record_html_atomically(
                    session, ctx.doc,
                    lambda: format_support.replace_full_document(ctx.doc, ctx.ctx, content, config_svc=config_svc),
                    track_reviewable, proposed_preview=_plain_preview(content))
            return {"status": "ok", "message": "Replaced entire document."}, session
        if target == "end":
            with session:
                session.record_mutation(
                    lambda: format_support.insert_content_at_position(ctx.doc, ctx.ctx, content, "end", config_svc=config_svc),
                    proposed_preview=_plain_preview(content))
            return attach_edited_context(
                {"status": "ok", "message": "Inserted content at end."},
                collapsed_anchor(ctx.doc.getText().getEnd())), session
        if target == "selection":
            # Anchor BEFORE the edit: the selection insert clears the selection first, so only a
            # collapsed position (not the selection range itself) survives the replace.
            anchor = selection_anchor(ctx.doc)
            with session:
                # Selection insert clears the selection first, then imports -> atomic (full_document above).
                record_html_atomically(
                    session, ctx.doc,
                    lambda: format_support.insert_content_at_position(ctx.doc, ctx.ctx, content, "selection", config_svc=config_svc),
                    track_reviewable, proposed_preview=_plain_preview(content))
            return attach_edited_context(
                {"status": "ok", "message": "Inserted content at selection."}, anchor), session
        if target == "beginning":
            with session:
                session.record_mutation(
                    lambda: format_support.insert_content_at_position(ctx.doc, ctx.ctx, content, "beginning", config_svc=config_svc),
                    proposed_preview=_plain_preview(content))
            return attach_edited_context(
                {"status": "ok", "message": "Inserted content at beginning."},
                collapsed_anchor(ctx.doc.getText().getStart())), session

        # target == "search" from here on — old_content must be a findable substring, not the full body.
        # Whole-document replace: target='full_document' (no search, no old_content).
        old_stripped = str(old_content).strip()

        search_string = old_stripped
        if format_support.content_has_markup(search_string):
            search_string = format_support.html_to_plain_text(search_string, ctx.ctx, config_svc)
        # Collapse exotic horizontal whitespace; preserve newlines for paragraph-aware search.
        search_string = search_mod.normalize_search_string_for_find(search_string)
        if not search_string:
            # Parameter error (like old_content=None), not a search no-op: the search never ran,
            # so there's no replaced_count to report — use the standard tool error shape.
            return self._tool_error("old_content is empty after normalization."), session
        doc = ctx.doc
        # replaced_count is the machine-readable success signal: 0 -> status "error" (a silent
        # no-op surfaced as a failure), N>0 -> "ok". No matched_count/warning/partial-replace:
        # if a replace raises mid-all_matches the existing abort behavior stands.
        # TODO(follow-up): share search-path return dicts with string_eval_tools.py to avoid drift.
        # Explicit regex / case control (opt-in): a direct SearchDescriptor find that bypasses the
        # default lenient matcher, so "search with options then replace" agrees with search_in_document.
        _regex_opt = bool(kwargs.get("regex"))
        _case_opt = kwargs.get("case_sensitive")
        _use_opts = _regex_opt or _case_opt is not None
        _opts_pattern = old_stripped if _regex_opt else search_string
        _opts_cs = bool(_case_opt) if _case_opt is not None else False
        override, override_error = self._parse_hyperlink_url(kwargs)
        if override_error:
            return self._tool_error(override_error, code="INVALID_PARAM"), session
        if override and position != "replace":
            return self._tool_error(
                "hyperlink_url only applies to position='replace'.",
                code="INVALID_PARAM"), session

        all_matches = kwargs.get("all_matches", False)
        if all_matches:
            ranges = (search_mod.find_ranges_regex_case(doc, _opts_pattern, _regex_opt, _opts_cs, all_matches=True)
                      if _use_opts else search_mod.find_all_ranges(doc, search_string))
            if not ranges:
                return search_mod.build_search_not_found_response(all_matches=True), session
            anchor = collapsed_anchor(ranges[0])
            undo_title = next_agent_edit_undo_title()
            try:
                mgr = doc.getUndoManager()
                if mgr.isLocked():
                    raise ToolExecutionError("undo manager is locked")
                mgr.enterUndoContext(undo_title)
            except Exception:
                return self._tool_error(
                    "Cannot apply all_matches atomically (no usable undo context); "
                    "refusing rather than risk a half-applied edit.",
                    code="UNDO_UNAVAILABLE"), session
            changes_before = len(session.changes)
            applied_ok = False
            count = 0
            link_reports: list[dict] = []
            try:
                with session:
                    for found in reversed(ranges):
                        # batch=True: this loop already holds the undo context, so the
                        # outline URL write stays in the same Ctrl+Z as the text.
                        reports, link_err = self._replace_found(
                            session, doc, found, use_preserve=use_preserve, raw_content=raw_content,
                            content=content, ctx=ctx, config_svc=config_svc,
                            track_reviewable=track_reviewable, override=override, batch=True,
                            plain_preview=_plain_preview(content))
                        if link_err:
                            raise RuntimeError(link_err.get("message") or "outline hyperlink update failed")
                        if reports:
                            link_reports.extend(reports)
                        count += 1
                applied_ok = True
            except Exception as e:
                log.exception("apply_document_content all_matches failed mid-batch")
                close_surgical_context(mgr, session, changes_before, False, undo_title)
                return {"status": "error",
                        "message": ("all_matches aborted after %d replacement(s); document rolled back (%s)."
                                    % (count, e)),
                        "replaced_count": 0, "partial_failure": True, "attempted": count}, session
            finally:
                if applied_ok:
                    close_surgical_context(mgr, session, changes_before, True, undo_title)
            resp = search_mod.build_search_replace_response(count, use_preserve=use_preserve)
            if count > 1:
                resp["message"] += " edited_context shows the first occurrence's neighborhood."
            self._attach_hyperlink_reports(resp, link_reports)
            return attach_edited_context(resp, anchor), session
        if occurrence is not None:
            # Index the same replaceable list the edit path uses. Empty ranges fall
            # through to the existing miss / drawing-shape path (models often send 0).
            ranges = (
                search_mod.find_ranges_regex_case(
                    doc, _opts_pattern, _regex_opt, _opts_cs, all_matches=True)
                if _use_opts
                else search_mod.find_all_ranges(doc, search_string)
            )
            if ranges and occurrence >= len(ranges):
                return self._occurrence_oor(occurrence, len(ranges)), session
            found = ranges[occurrence] if ranges else None
        else:
            found = (
                search_mod.find_ranges_regex_case(
                    doc, _opts_pattern, _regex_opt, _opts_cs, all_matches=False)
                if _use_opts
                else search_mod.find_first_range(doc, search_string)
            )
        if found is None:
            # Search covers body/table cells/text frames but not drawing-layer shapes. If the text
            # lives only inside such a floating box, say so (actionable) instead of a bare not-found
            # -- otherwise the agent retries blindly or assumes failure where a shapes-toolset edit
            # is needed (note 7).
            shape = search_mod.drawing_shape_object_containing(
                doc, _opts_pattern if _regex_opt else search_string,
                use_regex=_regex_opt, case_sensitive=_opts_cs if _use_opts else False)
            if shape is not None:
                shape_name = (getattr(shape, "Name", "") or "").strip() or "(unnamed shape)"
                if track_reviewable:
                    # Review modes (record/wait) require edits to become reviewable tracked changes,
                    # but drawing-shape text can't carry redlines and editing it directly would bypass
                    # the review/session machinery. Route to the shapes toolset rather than silently
                    # applying an UNtracked edit while the caller believes review is engaged.
                    return {"status": "error",
                            "message": ("old_content is only inside a drawing shape / floating text box "
                                        f"('{shape_name}'). In review mode it cannot be edited as a tracked "
                                        "change; edit it via the shapes toolset "
                                        "(delegate_to_specialized_writer_toolset domain='shapes')."),
                            "replaced_count": 0}, session
                # Review off: the text lives only inside a floating drawing shape, which findFirst/
                # replace can't reach. Edit the shape's own text directly (note 7), preserving format.
                new_text = _plain_preview(content)
                undo = None
                undo_title = next_agent_edit_undo_title()
                try:
                    undo = doc.getUndoManager()
                    undo.enterUndoContext(undo_title)
                except Exception:
                    undo = None
                try:
                    edited = replace_text_in_shape(shape, search_string, new_text)
                finally:
                    if undo is not None:
                        try:
                            undo.leaveUndoContext()
                        except Exception:
                            pass
                if edited:
                    result = attach_edited_context(
                        {"status": "ok",
                          "message": ("Replaced 1 occurrence inside drawing shape '%s' (edited the "
                                      "shape's own text directly — NOT a tracked change; surrounding "
                                      "formatting preserved)." % shape_name),
                          "replaced_count": 1, "review_bypassed": True},
                        None)
                    return self._annotate_review_status(ctx.ctx, result), session
                return search_mod.build_search_not_found_response(shape_name=shape_name), session
            return search_mod.build_search_not_found_response(all_matches=False), session
        if position in ("before", "after"):
            # INSERT next to the match instead of replacing it: the single most common petition
            # edit ("add a paragraph after clause X") previously forced resending the clause
            # itself in content — which in record/wait rendered as a tracked delete+reinsert of
            # text nobody touched. Collapse a cursor at the match edge and run the normal
            # HTML-import insert there; only the genuinely new text enters the review record.
            #
            # Two guarded gaps (clear error beats an opaque failure; the atomic wrapper would
            # roll back either way): the mixed-math importer appends later segments at the
            # DOCUMENT END (format.py per-segment goto-end), and the HTML import path is not
            # cell-safe (same nested-XText hazard the replace path detects).
            if format_support.html_fragment_contains_mixed_math(str(content)):
                return self._tool_error(
                    "position='before'/'after' does not support content with math segments yet "
                    "(later segments would land at the document end); use position='replace' "
                    "including the math, or target='end'."), session
            try:
                in_cell = found.getText().createTextCursorByRange(
                    found.getStart()).getPropertyValue("TextTable") is not None
            except Exception:
                in_cell = False
            if in_cell:
                return self._tool_error(
                    "position='before'/'after' next to a match inside a table cell is not "
                    "supported yet; use position='replace' with plain text, or rewrite the "
                    "cell content."), session
            try:
                edge = found.getStart() if position == "before" else found.getEnd()
                insert_cursor = found.getText().createTextCursorByRange(edge)
            except Exception as e:
                return self._tool_error("Could not anchor the %s-match insert: %s" % (position, e)), session
            anchor = collapsed_anchor(found)
            with session:
                record_html_atomically(
                    session, doc,
                    lambda: format_support.insert_html_at_cursor(doc, ctx.ctx, insert_cursor, content, config_svc=config_svc, apply_styles=False),
                    track_reviewable, proposed_preview=_plain_preview(content))
            insert_resp: dict[str, object] = {
                "status": "ok",
                "message": "Inserted content %s the old_content match (matched text left untouched)." % position,
                "inserted": True,
                "position": position,
            }
            if occurrence is not None:
                insert_resp["occurrence"] = occurrence
            return attach_edited_context(insert_resp, anchor), session

        # Anchor BEFORE the mutation: the found range's content is replaced (HTML path even
        # deletes-then-imports). edited_context uses this collapsed start. A preserve-format
        # replace's per-character setString pushes a cursor saved here forward, so the
        # outline URL is painted from the paragraph start (_select_replacement), not from
        # this anchor. Outline capture happens inside _replace_found, also before that delete.
        anchor = collapsed_anchor(found)
        reports, link_err = self._replace_found(
            session, doc, found, use_preserve=use_preserve, raw_content=raw_content,
            content=content, ctx=ctx, config_svc=config_svc,
            track_reviewable=track_reviewable, override=override, batch=False,
            plain_preview=_plain_preview(content))
        if link_err:
            return link_err, session
        resp = search_mod.build_search_replace_response(1, use_preserve=use_preserve)
        if occurrence is not None:
            resp["occurrence"] = occurrence
        self._attach_hyperlink_reports(resp, reports or [])
        return attach_edited_context(resp, anchor), session


