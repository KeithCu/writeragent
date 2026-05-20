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
    guess_doc_type_from_path,
    list_nearby_files,
    open_document_for_read,
)
from plugin.calc.spreadsheet_search import search_spreadsheet_cells
from plugin.writer.paragraph_search import search_paragraph_texts

log = logging.getLogger(__name__)

DEFAULT_GREP_MAX_FILES = 10
DEFAULT_GREP_MAX_RESULTS_PER_FILE = 5
DEFAULT_GREP_MAX_TOTAL_RESULTS = 30
_DRAW_GREP_SHAPE_CAP = 200


def resolve_grep_candidates(
    ctx: Any,
    active_model: Any,
    *,
    file_subset: str | None = None,
    max_files: int = DEFAULT_GREP_MAX_FILES,
) -> tuple[list[FileEntry], bool, str | None]:
    """Return (candidates, truncated_files, error_message).

    *file_subset* is a basename token (e.g. ``budget`` → ``*budget*.od*``) or an absolute path to one file.
    """
    raw = str(file_subset).strip() if file_subset else None

    if raw and os.path.isabs(raw) and os.path.isfile(raw):
        norm = os.path.normpath(os.path.abspath(raw))
        entry = FileEntry(
            path=norm,
            name=os.path.basename(norm),
            url="",
            modified=0.0,
            size_bytes=0,
            doc_type_guess=guess_doc_type_from_path(norm),
            is_open=False,
        )
        try:
            st = os.stat(norm)
            entry["modified"] = st.st_mtime
            entry["size_bytes"] = st.st_size
        except OSError:
            pass
        return [entry], False, None

    listing = list_nearby_files(ctx, active_model, filter=raw, file_kind="documents", max_entries=100)
    if listing.get("status") != "ok":
        return [], False, listing.get("message", "Could not list nearby files")

    files: list[FileEntry] = list(listing.get("files") or [])
    listing_truncated = bool(listing.get("truncated"))

    open_files = [f for f in files if f.get("is_open")]
    closed_files = [f for f in files if not f.get("is_open")]
    ordered = open_files + closed_files

    truncated_files = listing_truncated or len(ordered) > max_files
    return ordered[:max_files], truncated_files, None


def _grep_text_in_writer(
    model: Any,
    services: Any,
    pattern: str,
    *,
    regex: bool = False,
    case_sensitive: bool = False,
    max_results: int,
    context_paragraphs: int,
) -> tuple[list[dict[str, Any]], int]:
    doc_svc = services.document
    para_ranges = doc_svc.get_paragraph_ranges(model)
    para_texts: list[str] = []
    for para in para_ranges:
        try:
            if para.supportsService("com.sun.star.text.Paragraph"):
                para_texts.append(para.getString())
            else:
                para_texts.append("")
        except Exception:
            para_texts.append("")

    try:
        return search_paragraph_texts(
            pattern,
            para_texts,
            regex=regex,
            case_sensitive=case_sensitive,
            max_results=max_results,
            context_paragraphs=context_paragraphs,
        )
    except ValueError as e:
        raise ValueError(str(e)) from e


def _grep_text_in_calc(
    model: Any,
    pattern: str,
    *,
    regex: bool = False,
    case_sensitive: bool = False,
    max_results: int,
) -> tuple[list[dict[str, Any]], int]:
    matches = search_spreadsheet_cells(
        model,
        pattern,
        regex=regex,
        case_sensitive=case_sensitive,
        max_results=max_results,
        all_sheets=True,
    )
    return matches, len(matches)


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
) -> tuple[list[dict[str, Any]], int, bool]:
    import re as re_mod

    compiled = None
    if regex:
        flags = 0 if case_sensitive else re_mod.IGNORECASE
        try:
            compiled = re_mod.compile(pattern, flags)
        except re_mod.error as e:
            raise ValueError(f"Invalid regex: {e}") from e

    matches: list[dict[str, Any]] = []
    shapes_visited = 0
    partial = False

    try:
        pages = model.getDrawPages()
        page_count = pages.getCount()
    except Exception:
        return matches, 0, partial

    for page_idx in range(page_count):
        if len(matches) >= max_results or shapes_visited >= shape_cap:
            break
        try:
            page = pages.getByIndex(page_idx)
        except Exception:
            continue
        page_matches, shapes_visited, partial = _grep_shapes_on_page(
            page,
            page_idx,
            pattern,
            regex=regex,
            case_sensitive=case_sensitive,
            compiled=compiled,
            max_results=max_results,
            shapes_visited=shapes_visited,
            shape_cap=shape_cap,
        )
        matches.extend(page_matches)
        if shapes_visited >= shape_cap and len(matches) < max_results:
            partial = True

    return matches[:max_results], len(matches), partial


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
) -> tuple[list[dict[str, Any]], int, bool]:
    matches: list[dict[str, Any]] = []
    partial = False

    try:
        count = xshapes.getCount()
    except Exception:
        return matches, shapes_visited, partial

    for i in range(count):
        if len(matches) >= max_results or shapes_visited >= shape_cap:
            partial = shapes_visited >= shape_cap
            break
        shapes_visited += 1
        try:
            shape = xshapes.getByIndex(i)
        except Exception:
            continue

        shape_path = f"{path_prefix}{i}" if path_prefix else str(i)

        if hasattr(shape, "getString"):
            try:
                text = str(shape.getString() or "").strip()
            except Exception:
                text = ""
            if text and _shape_text_matches_pattern(text, pattern, regex=regex, case_sensitive=case_sensitive, compiled=compiled):
                snippet = text if len(text) <= 200 else text[:200] + "…"
                matches.append({"page_index": page_index, "shape_index": shape_path, "text": snippet})

        try:
            shape_type = shape.getShapeType()
        except Exception:
            shape_type = ""
        if "GroupShape" in str(shape_type):
            try:
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
                )
                matches.extend(group_matches)
                if group_partial:
                    partial = True
            except Exception:
                log.debug("grep group shape failed", exc_info=True)

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
    context_paragraphs: int,
) -> tuple[list[dict[str, Any]], int, bool, str | None]:
    """Return (matches, match_count, partial, error_message)."""
    partial = False
    try:
        if doc_type == "writer":
            matches, count = _grep_text_in_writer(
                model,
                services,
                pattern,
                regex=regex,
                case_sensitive=case_sensitive,
                max_results=max_results_per_file,
                context_paragraphs=context_paragraphs,
            )
            return matches, count, False, None
        if doc_type == "calc":
            matches, count = _grep_text_in_calc(
                model,
                pattern,
                regex=regex,
                case_sensitive=case_sensitive,
                max_results=max_results_per_file,
            )
            return matches, count, False, None
        if doc_type == "draw":
            matches, count, partial = _grep_text_in_draw(
                model,
                pattern,
                regex=regex,
                case_sensitive=case_sensitive,
                max_results=max_results_per_file,
            )
            return matches, count, partial, None
        return [], 0, False, f"Unsupported doc_type {doc_type!r} for grep"
    except ValueError as e:
        return [], 0, False, str(e)
    except Exception as e:
        log.exception("grep search failed for doc_type=%s", doc_type)
        return [], 0, False, str(e)


def _process_events_if_available(ctx: Any) -> None:
    try:
        from plugin.framework.uno_context import get_toolkit

        toolkit = get_toolkit(ctx)
        if toolkit is not None and hasattr(toolkit, "processEventsToIdle"):
            toolkit.processEventsToIdle()
    except Exception:
        log.debug("processEventsToIdle during grep failed", exc_info=True)


def grep_nearby_files(
    ctx: Any,
    active_model: Any,
    services: Any,
    pattern: str,
    *,
    file_subset: str | None = None,
    regex: bool = False,
    case_sensitive: bool = False,
    max_files: int = DEFAULT_GREP_MAX_FILES,
    max_results_per_file: int = DEFAULT_GREP_MAX_RESULTS_PER_FILE,
    max_total_results: int = DEFAULT_GREP_MAX_TOTAL_RESULTS,
    context_paragraphs: int = 1,
    stop_checker: Callable[[], bool] | None = None,
    status_callback: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """Search nearby LibreOffice files for *pattern*; return aggregated hits with snippets."""
    pattern = str(pattern).strip()
    if not pattern:
        return {"status": "error", "message": "pattern is required"}

    max_files = max(1, int(max_files))
    max_results_per_file = max(1, int(max_results_per_file))
    max_total_results = max(1, int(max_total_results))
    context_paragraphs = max(0, int(context_paragraphs))

    subset_norm = str(file_subset).strip() if file_subset else None

    candidates, truncated_files, list_err = resolve_grep_candidates(
        ctx,
        active_model,
        file_subset=subset_norm,
        max_files=max_files,
    )
    if list_err:
        return {"status": "error", "message": list_err}

    hits: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    total_snippets = 0
    stopped_early = False
    files_scanned = 0

    for idx, entry in enumerate(candidates):
        if stop_checker and stop_checker():
            stopped_early = True
            break
        if total_snippets >= max_total_results:
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
        model, doc_type, open_err, opened_for_document_research = open_document_for_read(ctx, target)
        files_scanned += 1

        if model is None or doc_type is None:
            errors.append({"path": path, "message": open_err or "Open failed"})
            _process_events_if_available(ctx)
            continue

        per_file_limit = min(max_results_per_file, max_total_results - total_snippets)
        try:
            matches, match_count, partial, search_err = _search_opened_document(
                model,
                doc_type,
                services,
                pattern,
                regex=regex,
                case_sensitive=case_sensitive,
                max_results_per_file=per_file_limit,
                context_paragraphs=context_paragraphs,
            )
        finally:
            close_document_research_document(model, opened_for_document_research=opened_for_document_research)

        _process_events_if_available(ctx)

        if search_err:
            errors.append({"path": path, "message": search_err})
            continue

        if not matches:
            continue

        hit_entry: dict[str, Any] = {
            "path": path,
            "name": name,
            "doc_type": doc_type,
            "match_count": match_count,
            "matches": matches,
        }
        if partial:
            hit_entry["partial"] = True
        hits.append(hit_entry)
        total_snippets += len(matches)

        if total_snippets >= max_total_results:
            stopped_early = True
            break

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
