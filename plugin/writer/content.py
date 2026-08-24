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


log = logging.getLogger("writeragent.writer")




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
        t0 = time.perf_counter()
        scope = kwargs.get("scope", "full")
        max_chars = kwargs.get("max_chars")
        range_start = kwargs.get("start") if scope == "range" else None
        range_end = kwargs.get("end") if scope == "range" else None
        log.debug("get_document_content: start scope=%r max_chars=%r", scope, max_chars)

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
            "content": {"type": "array", "items": {"type": "string"}, "description": ("List of HTML fragments or plain-text fragments (one per block); shape and math per the APPLY_DOCUMENT_CONTENT AND HTML rules — the editing-html guidance covers them if they are not already in your context. No Markdown.")},
            "target": {"type": "string", "enum": ["beginning", "end", "selection", "full_document", "search"], "description": "Where to apply the content."},
            "old_content": {"type": "string", "description": ("Substring to find when target='search'. Not for whole-document replace — use target='full_document' instead.")},
            "all_matches": {"type": "boolean", "description": "Replace all occurrences (true) or first only. Default false. Only for target='search' with position='replace'."},
            "position": {"type": "string", "enum": ["replace", "before", "after"], "description": ("For target='search': 'replace' (default) replaces the match; 'before'/'after' INSERT the content next to the match and leave the matched text untouched (result reports inserted=true instead of replaced_count).")},
            "dry_run": {"type": "boolean", "description": "For target='search': do NOT edit. Return how many times old_content matches and where each match lives, so you can check before committing."},
            "regex": {"type": "boolean", "description": "For target='search': treat old_content as a regular expression (default false = literal). Regex mode is single-paragraph (no cross-paragraph chaining)."},
            "case_sensitive": {"type": "boolean", "description": "For target='search': force case-sensitive (true) or case-insensitive (false) matching. Omit for the default lenient match."},
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
        for found in ranges[:20]:
            try:
                loc = search_mod.describe_match_location(found, ctx.doc, label_cache=label_cache)
            except Exception:
                loc = "body"
            try:
                snippet = found.getString()
            except Exception:
                snippet = ""
            matches.append({"location": loc, "text": snippet[:160]})
        opts_cs = bool(case_opt) if case_opt is not None else False
        pattern = old_stripped if use_regex else s
        shape_hits = search_mod.sweep_draw_shape_preview_matches(ctx.doc, pattern, use_regex, opts_cs, limit=10000)
        comment_hits = search_mod.sweep_comment_preview_matches(ctx.doc, pattern, use_regex, opts_cs, limit=10000)
        for item in shape_hits + comment_hits:
            if len(matches) < 20:
                matches.append({"location": item["location"], "text": item["text"][:160]})
        total = len(ranges) + len(shape_hits) + len(comment_hits)
        return {
            "status": "ok", "dry_run": True, "count": total, "matches": matches,
            "edit_reach_note": (
                "dry_run counts body/table/frame matches the edit path can replace, plus drawing shapes "
                "and comments (search_in_document uses the same split; only floating shapes are editable "
                "in review-off mode or via the shapes toolset)."),
        }

    def execute(self, ctx, **kwargs):
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
            try:
                with session:
                    for found in reversed(ranges):
                        original = found.getString()
                        if use_preserve:
                            record_preserve_replace(session, doc, found, raw_content, ctx.ctx, track_reviewable)
                        else:
                            session.record_mutation(
                                lambda f=found: format_support.replace_single_range_with_content(
                                    doc, f, content, ctx.ctx, config_svc),
                                original_preview=original, proposed_preview=_plain_preview(content))
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
            return attach_edited_context(resp, anchor), session
        found = (search_mod.find_ranges_regex_case(doc, _opts_pattern, _regex_opt, _opts_cs, all_matches=False)
                 if _use_opts else search_mod.find_first_range(doc, search_string))
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
            return attach_edited_context(
                {"status": "ok",
                 "message": "Inserted content %s the old_content match (matched text left untouched)." % position,
                 "inserted": True, "position": position}, anchor), session

        original = found.getString()
        # Anchor BEFORE the mutation: the found range's content is replaced (HTML path even
        # deletes-then-imports), but a collapsed position at its start survives.
        anchor = collapsed_anchor(found)
        with session:
            if use_preserve:
                record_preserve_replace(session, doc, found, raw_content, ctx.ctx, track_reviewable)
            else:
                record_html_atomically(
                    session, doc,
                    lambda: format_support.replace_single_range_with_content(doc, found, content, ctx.ctx, config_svc),
                    track_reviewable, original_preview=original, proposed_preview=_plain_preview(content))
        resp = search_mod.build_search_replace_response(1, use_preserve=use_preserve)
        return attach_edited_context(resp, anchor), session


