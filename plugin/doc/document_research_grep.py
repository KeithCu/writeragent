# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
"""Cross-file text grep for the document_research outer sub-agent."""

from __future__ import annotations

import logging
import os
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable

from plugin.doc.document_research import (
    FileEntry,
    close_document_research_document,
    file_entry_for_path,
    guess_doc_type_from_path,
    list_nearby_files,
    open_document_for_read,
    _system_path_from_url,
    _USE_DEFAULT,
)
from plugin.calc.spreadsheet_search import search_spreadsheet_cells
from plugin.doc.paragraph_search import compile_search_pattern, search_paragraph_texts
from plugin.doc.text_helpers import get_string_without_tracked_deletions
from plugin.framework.errors import uno_probe, user_stopped_payload

log = logging.getLogger(__name__)

# Listing can already be truncated (list_nearby_files max 100). At most 50
# candidates are opened. Each file returns at most 5 matches, and the scan
# stops at 30 snippets (stopped_early). Draw also sets that hit's partial
# when _DRAW_GREP_SHAPE_CAP (200) is hit with result budget left. A hit's
# partial: true means that file has more matches than were returned. Stop
# returns USER_STOPPED and drops hits.
DEFAULT_GREP_MAX_FILES = 50
DEFAULT_GREP_MAX_RESULTS_PER_FILE = 5
DEFAULT_GREP_MAX_TOTAL_RESULTS = 30
_DRAW_GREP_SHAPE_CAP = 200

# Same 200-character preview budget as Search hit text (spreadsheet_search
# _HIT_TEXT_MAX); not a user setting.
_SNIPPET_MAX = 200


def _snippet(text: str) -> str:
    return text if len(text) <= _SNIPPET_MAX else text[:_SNIPPET_MAX] + "…"


def resolve_grep_candidates(
    ctx: Any,
    active_model: Any,
    *,
    file_subset: str | None = None,
    exclude_path: Any = _USE_DEFAULT,
    open_paths: Any = _USE_DEFAULT,
    listing_root: Any = _USE_DEFAULT,
    stop_checker: Callable[[], bool] | None = None,
) -> tuple[list[FileEntry], bool, str | None]:
    """Return (candidates, truncated_files, error_message).

    *file_subset* is a basename token (e.g. ``budget`` → ``*budget*.od*``),
    an absolute path, or a ``file://`` URL of one file.
    """
    raw = str(file_subset).strip() if file_subset else None

    if raw and raw.startswith("file:"):
        # urlparse + url2pathname drops the host of file://server/share/...;
        # document research already converts with uno.fileUrlToSystemPath.
        local_path = _system_path_from_url(raw)
        if local_path and os.path.isfile(local_path):
            return [file_entry_for_path(local_path)], False, None
        return [], False, f"No file matching {raw!r}"

    if raw and os.path.isabs(raw) and os.path.isfile(raw):
        return [file_entry_for_path(raw)], False, None

    listing = list_nearby_files(
        ctx,
        active_model,
        filter=raw,
        file_kind="documents",
        max_entries=100,
        exclude_path=exclude_path,
        open_paths=open_paths,
        listing_root=listing_root,
        stop_checker=stop_checker,
    )
    if listing.get("status") != "ok":
        if listing.get("code") == "USER_STOPPED":
            raise InterruptedError()
        return [], False, listing.get("message", "Could not list nearby files")

    files: list[FileEntry] = list(listing.get("files") or [])
    listing_truncated = bool(listing.get("truncated"))

    open_files = [f for f in files if f.get("is_open")]
    closed_files = [f for f in files if not f.get("is_open")]
    ordered = open_files + closed_files

    truncated_files = listing_truncated or len(ordered) > DEFAULT_GREP_MAX_FILES
    return ordered[:DEFAULT_GREP_MAX_FILES], truncated_files, None


def _grep_text_in_writer(
    model: Any,
    services: Any,
    pattern: str,
    *,
    regex: bool = False,
    case_sensitive: bool = False,
    max_results: int,
    stop_checker: Callable[[], bool] | None = None,
) -> tuple[list[dict[str, Any]], int, bool]:
    doc_svc = services.document
    para_ranges = doc_svc.get_paragraph_ranges(model)
    para_texts: list[str] = []
    # getString() includes tracked deletions, so a keyword the author
    # removed still matched. Grep is a read of the visible document;
    # this is the same helper other reads use. The search is already on
    # the main thread.
    for para in para_ranges:
        if stop_checker and stop_checker():
            break
        if para.supportsService("com.sun.star.text.Paragraph"):
            para_texts.append(get_string_without_tracked_deletions(para))
        elif para.supportsService("com.sun.star.text.TextTable"):
            cell_texts = [
                uno_probe(lambda: get_string_without_tracked_deletions(para.getCellByName(name)), "")
                for name in (para.getCellNames() or ())
            ]
            para_texts.append("\n".join(c for c in cell_texts if c))
        else:
            para_texts.append("")

    # Bugfix: Stop was ignored for the whole Writer extract and for the
    # paragraph scan. stop_checker was on the signature but never called,
    # and search_paragraph_texts never received it, so a large document
    # finished getString and the match walk before grep_nearby_files could
    # return USER_STOPPED. The outer file loop still decides USER_STOPPED:
    # the check after each file's search covers the last file, so Stop during
    # that search is not status: ok.
    matches, total = search_paragraph_texts(
        pattern,
        para_texts,
        regex=regex,
        case_sensitive=case_sensitive,
        max_results=max_results,
        context_paragraphs=2,  # Always include 2 paragraphs of context for Writer matches
        stop_checker=stop_checker,
    )
    for match in matches:
        match["text"] = _snippet(match["text"])
        for context_entry in match["context"]:
            context_entry["text"] = _snippet(context_entry["text"])
    # Bugfix: match_count was the uncapped total from search_paragraph_texts
    # while matches was sliced to max_results, and partial was forced false.
    # Calc and Draw already set match_count to the returned length. Writer
    # now does too. partial is true when that total is larger than the slice,
    # or when Stop cut extraction or the scan. search_paragraph_texts still
    # returns the uncapped total so this does not guess from len == max_results.
    partial = total > len(matches) or bool(stop_checker and stop_checker())
    return matches, len(matches), partial


def _grep_text_in_calc(
    model: Any,
    pattern: str,
    *,
    regex: bool = False,
    case_sensitive: bool = False,
    max_results: int,
    stop_checker: Callable[[], bool] | None = None,
) -> tuple[list[dict[str, Any]], int, bool]:
    # search_spreadsheet_cells stops at max_results with no truncated flag,
    # and search_in_spreadsheet still needs the list. One extra hit
    # distinguishes a full cap from a short sheet. partial here is truncation only.
    fetched = search_spreadsheet_cells(
        model,
        pattern,
        regex=regex,
        case_sensitive=case_sensitive,
        max_results=max_results + 1,
        all_sheets=True,
        stop_checker=stop_checker,
    )
    partial = len(fetched) > max_results
    matches = fetched[:max_results]
    return matches, len(matches), partial


def _shape_text_matches_pattern(
    text: str,
    pattern: str,
    *,
    regex: bool,
    case_sensitive: bool,
    compiled: Any,
) -> bool:
    if not text:
        return False
    if regex and compiled is not None:
        return compiled.search(text) is not None
    if case_sensitive:
        return pattern in text
    return pattern.lower() in text.lower()


def _grep_text_in_draw(
    model: Any,
    pattern: str,
    *,
    regex: bool = False,
    case_sensitive: bool = False,
    max_results: int,
    shape_cap: int = _DRAW_GREP_SHAPE_CAP,
    stop_checker: Callable[[], bool] | None = None,
) -> tuple[list[dict[str, Any]], int, bool]:
    compiled = compile_search_pattern(pattern, regex=regex, case_sensitive=case_sensitive)

    matches: list[dict[str, Any]] = []
    shapes_visited = 0
    partial = False

    pages = model.getDrawPages()
    page_count = pages.getCount()

    scan_limit = max_results + 1
    for page_idx in range(page_count):
        if stop_checker and stop_checker():
            partial = True
            break
        if len(matches) >= scan_limit or shapes_visited >= shape_cap:
            break
        page = pages.getByIndex(page_idx)
        # match_count must not exceed the matches returned. Searching
        # each page with the full max_results lets a later page add
        # another full batch; slicing the list while counting the
        # unsliced length inflates the count. Pass the leftover budget
        # per page (same as group recursion) and count that slice.
        remaining = scan_limit - len(matches)
        page_matches, shapes_visited, page_partial = _grep_shapes_on_page(
            page,
            page_idx,
            pattern,
            regex=regex,
            case_sensitive=case_sensitive,
            compiled=compiled,
            max_results=remaining,
            shapes_visited=shapes_visited,
            shape_cap=shape_cap,
            stop_checker=stop_checker,
        )
        matches.extend(page_matches)
        partial = partial or page_partial
        if shapes_visited >= shape_cap and len(matches) < max_results:
            partial = True

    # The shape walk has no truncated flag, so one extra hit distinguishes
    # a full cap from a deck that ended on the last returned match.
    returned = matches[:max_results]
    return returned, len(returned), partial or len(matches) > max_results


def _grep_shapes_on_page(
    xshapes: Any,
    page_index: int,
    pattern: str,
    *,
    regex: bool,
    case_sensitive: bool,
    compiled: Any,
    max_results: int,
    shapes_visited: int,
    shape_cap: int,
    path_prefix: str = "",
    stop_checker: Callable[[], bool] | None = None,
) -> tuple[list[dict[str, Any]], int, bool]:
    matches: list[dict[str, Any]] = []
    partial = False

    count = xshapes.getCount()

    for i in range(count):
        if stop_checker and stop_checker():
            partial = True
            break
        if len(matches) >= max_results or shapes_visited >= shape_cap:
            partial = partial or shapes_visited >= shape_cap
            break
        shapes_visited += 1
        shape = xshapes.getByIndex(i)

        shape_path = f"{path_prefix}{i}" if path_prefix else str(i)

        text = str(uno_probe(lambda: shape.getString(), "") or "").strip()
        if text and _shape_text_matches_pattern(text, pattern, regex=regex, case_sensitive=case_sensitive, compiled=compiled):
            snippet = _snippet(text)
            matches.append({"page_index": page_index, "shape_index": shape_path, "text": snippet})

        if "GroupShape" in str(uno_probe(lambda: shape.getShapeType(), "")):
            group_matches, shapes_visited, group_partial = _grep_shapes_on_page(
                shape,
                page_index,
                pattern,
                regex=regex,
                case_sensitive=case_sensitive,
                compiled=compiled,
                max_results=max_results - len(matches),
                shapes_visited=shapes_visited,
                shape_cap=shape_cap,
                path_prefix=f"{shape_path}.",
                stop_checker=stop_checker,
            )
            matches.extend(group_matches)
            if group_partial:
                partial = True

    return matches, shapes_visited, partial


def _search_opened_document(
    model: Any,
    doc_type: str,
    services: Any,
    pattern: str,
    *,
    regex: bool,
    case_sensitive: bool,
    max_results_per_file: int,
    stop_checker: Callable[[], bool] | None = None,
) -> tuple[list[dict[str, Any]], int, bool, str | None]:
    """Return (matches, match_count, partial, error_message)."""
    partial = False
    try:
        if doc_type == "writer":
            matches, count, partial = _grep_text_in_writer(
                model,
                services,
                pattern,
                regex=regex,
                case_sensitive=case_sensitive,
                max_results=max_results_per_file,
                stop_checker=stop_checker,
            )
            return matches, count, partial, None
        if doc_type == "calc":
            matches, count, partial = _grep_text_in_calc(
                model,
                pattern,
                regex=regex,
                case_sensitive=case_sensitive,
                max_results=max_results_per_file,
                stop_checker=stop_checker,
            )
            return matches, count, partial, None
        if doc_type == "draw":
            matches, count, partial = _grep_text_in_draw(
                model,
                pattern,
                regex=regex,
                case_sensitive=case_sensitive,
                max_results=max_results_per_file,
                stop_checker=stop_checker,
            )
            return matches, count, partial, None
        return [], 0, False, f"Unsupported doc_type {doc_type!r} for grep"
    except Exception as e:
        log.exception("grep search failed for doc_type=%s", doc_type)
        return [], 0, False, str(e)


def _search_one_file(
    ctx: Any,
    target: str,
    services: Any,
    pattern: str,
    *,
    regex: bool,
    case_sensitive: bool,
    max_results: int,
    stop_checker: Callable[[], bool] | None = None,
) -> tuple[list[dict[str, Any]], str | None, bool, str | None]:
    """Open, search, and close one file in one main-thread hop.

    Returns (matches, doc_type, partial, error). One hop means a queued but
    cancelled item never opens a document, and a started item always closes it.
    """
    model, doc_type, open_err, opened = open_document_for_read(ctx, target)
    if model is None or doc_type is None:
        return [], None, False, open_err or "Open failed"
    try:
        matches, _count, partial, search_err = _search_opened_document(
            model,
            doc_type,
            services,
            pattern,
            regex=regex,
            case_sensitive=case_sensitive,
            max_results_per_file=max_results,
            stop_checker=stop_checker,
        )
    finally:
        close_document_research_document(model, opened_for_document_research=opened)
    return matches, doc_type, partial, search_err


def grep_nearby_files(
    ctx: Any,
    active_model: Any,
    services: Any,
    pattern: str,
    *,
    file_subset: str | None = None,
    regex: bool = False,
    case_sensitive: bool = False,
    stop_checker: Callable[[], bool] | None = None,
    status_callback: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """Search nearby LibreOffice files for *pattern*; return aggregated hits with snippets."""
    pattern = str(pattern).strip()
    if not pattern:
        return {"status": "error", "message": "pattern is required"}

    try:
        compile_search_pattern(pattern, regex=regex, case_sensitive=case_sensitive)
    except ValueError as exc:
        # Bugfix: an invalid regex was raised inside each opened file, so
        # grep_nearby_files opened every candidate, searched nothing, and
        # appended the same error once per file. The pattern does not
        # depend on the document. Fail once, before listing or opening.
        return {"status": "error", "message": str(exc)}

    subset_norm = str(file_subset).strip() if file_subset else None

    from plugin.framework.queue_executor import execute_on_main_thread
    from plugin.framework.send_cancellation import SendCancelled

    try:
        candidates, truncated_files, list_err = resolve_grep_candidates(
            ctx,
            active_model,
            file_subset=subset_norm,
            stop_checker=stop_checker,
        )
    except InterruptedError:
        return user_stopped_payload()

    if list_err:
        return {"status": "error", "message": list_err}

    hits: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    total_snippets = 0
    stopped_early = False
    user_stopped = False
    files_scanned = 0  # Files we attempted to open; a failed open still counts.

    for idx, entry in enumerate(candidates):
        if stop_checker and stop_checker():
            user_stopped = True
            break
        if total_snippets >= DEFAULT_GREP_MAX_TOTAL_RESULTS:
            stopped_early = True
            break

        path = entry["path"]
        name = entry["name"]
        url = entry.get("url") or ""
        doc_type_guess = entry.get("doc_type_guess") or guess_doc_type_from_path(path)
        if doc_type_guess == "unknown" or doc_type_guess == "image":
            continue

        if status_callback:
            status_callback(f"Grep: {name} ({idx + 1}/{len(candidates)})...")

        target = url if url.startswith("file://") else path
        per_file_limit = min(DEFAULT_GREP_MAX_RESULTS_PER_FILE, DEFAULT_GREP_MAX_TOTAL_RESULTS - total_snippets)

        files_scanned += 1
        try:
            matches, doc_type, partial, file_err = execute_on_main_thread(
                _search_one_file,
                ctx,
                target,
                services,
                pattern,
                regex=regex,
                case_sensitive=case_sensitive,
                max_results=per_file_limit,
                stop_checker=stop_checker,
            )
        except SendCancelled:
            return user_stopped_payload()
        except (TimeoutError, RuntimeError) as exc:
            # A file that cannot be marshalled must not abort the whole scan.
            errors.append({"path": path, "message": str(exc)})
            continue

        if stop_checker and stop_checker():
            user_stopped = True
            break

        if file_err or doc_type is None:
            errors.append({"path": path, "message": file_err or "Open failed"})
            continue

        if not matches:
            continue

        hit_entry: dict[str, Any] = {
            "path": path,
            "name": name,
            "doc_type": doc_type,
            "match_count": len(matches),
            "matches": matches,
        }
        if partial:
            hit_entry["partial"] = True
        hits.append(hit_entry)
        total_snippets += len(matches)

        if total_snippets >= DEFAULT_GREP_MAX_TOTAL_RESULTS:
            stopped_early = True
            break

    if user_stopped:
        return user_stopped_payload()

    result: dict[str, Any] = {
        "status": "ok",
        "pattern": pattern,
        "file_subset": subset_norm,
        "files_scanned": files_scanned,
        "files_with_hits": len(hits),
        "truncated_files": truncated_files,
        "stopped_early": stopped_early,
        "hits": hits,
    }
    if errors:
        result["errors"] = errors
    return result
