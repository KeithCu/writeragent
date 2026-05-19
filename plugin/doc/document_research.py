# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
"""Same-folder file discovery and hidden read-only document open for document_research delegation."""

from __future__ import annotations

import logging
import os
import urllib.parse
import urllib.request
from typing import Any, Literal, TypedDict, cast

import uno

from plugin.doc.document_helpers import DocumentType, get_document_path, get_document_type, resolve_document_by_url

log = logging.getLogger(__name__)

NEARBY_FILE_EXTENSIONS = frozenset(
    {
        ".odt",
        ".ott",
        ".ods",
        ".ots",
        ".odp",
        ".otp",
        ".odg",
        ".fodt",
        ".fods",
        ".fodp",
    }
)

DocTypeGuess = Literal["writer", "calc", "draw", "unknown"]

_DEFAULT_MAX_ENTRIES = 100


class FileEntry(TypedDict):
    path: str
    name: str
    url: str
    modified: float
    size_bytes: int
    doc_type_guess: DocTypeGuess
    is_open: bool


_EXTENSION_DOC_TYPE: dict[str, DocTypeGuess] = {
    ".odt": "writer",
    ".ott": "writer",
    ".fodt": "writer",
    ".ods": "calc",
    ".ots": "calc",
    ".fods": "calc",
    ".odp": "draw",
    ".otp": "draw",
    ".fodp": "draw",
    ".odg": "draw",
}


def guess_doc_type_from_path(path: str) -> DocTypeGuess:
    """Map a filesystem path extension to writer/calc/draw."""
    ext = os.path.splitext(path)[1].lower()
    return _EXTENSION_DOC_TYPE.get(ext, "unknown")


def _normalize_path(path: str) -> str:
    return os.path.normpath(os.path.abspath(path))


def _path_to_file_url(path: str) -> str:
    return urllib.parse.urljoin("file:", urllib.request.pathname2url(_normalize_path(path)))


def _system_path_from_url(url: str) -> str | None:
    if not url or not str(url).startswith("file://"):
        return None
    try:
        return _normalize_path(str(uno.fileUrlToSystemPath(url)))
    except Exception:
        log.debug("fileUrlToSystemPath failed for %s", url, exc_info=True)
        return None


def _should_skip_filename(name: str) -> bool:
    if name.startswith("~$"):
        return True
    lower = name.lower()
    return lower.endswith(".tmp") or lower.endswith(".bak")


def get_document_directory(model: Any) -> str | None:
    """Return the parent directory of the active document path, or None."""
    path = get_document_path(model)
    if not path:
        return None
    parent = os.path.dirname(_normalize_path(path))
    return parent if os.path.isdir(parent) else None


def get_work_directory(ctx: Any) -> str | None:
    """Return LibreOffice profile Work folder as an absolute path, or None."""
    if ctx is None:
        return None
    try:
        smgr = ctx.ServiceManager
        path_settings = smgr.createInstanceWithContext("com.sun.star.util.thePathSettings", ctx)
        if path_settings is None:
            return None
        work = path_settings.getPropertyValue("Work")
        if work is None:
            return None
        work_str = str(work).strip()
        if not work_str:
            return None
        if work_str.startswith("file://"):
            resolved = _system_path_from_url(work_str)
        else:
            resolved = _normalize_path(work_str)
        if resolved and os.path.isdir(resolved):
            return resolved
    except Exception:
        log.debug("get_work_directory failed", exc_info=True)
    return None


def resolve_listing_directory(ctx: Any, active_model: Any) -> str | None:
    """Directory to scan: active doc parent, else LO Work path, else None (open-docs fallback)."""
    parent = get_document_directory(active_model)
    if parent:
        return parent
    return get_work_directory(ctx)


def _collect_open_file_urls(ctx: Any, *, exclude_path: str | None) -> dict[str, str]:
    """Map normalized path -> file URL for open LO components."""
    from plugin.framework.uno_context import get_desktop

    out: dict[str, str] = {}
    exclude_norm = _normalize_path(exclude_path) if exclude_path else None
    try:
        desktop = get_desktop(ctx)
        comps = desktop.getComponents()
        if not comps:
            return out
        enum = comps.createEnumeration()
        while enum and enum.hasMoreElements():
            elem = enum.nextElement()
            model = elem
            if hasattr(elem, "getController") and elem.getController():
                model = elem.getController().getModel()
            if model is None or not hasattr(model, "getURL"):
                continue
            url = model.getURL()
            if not url or not str(url).startswith("file://"):
                continue
            path = _system_path_from_url(str(url))
            if not path:
                continue
            if exclude_norm and _normalize_path(path) == exclude_norm:
                continue
            ext = os.path.splitext(path)[1].lower()
            if ext not in NEARBY_FILE_EXTENSIONS:
                continue
            out[_normalize_path(path)] = str(url)
    except Exception:
        log.exception("_collect_open_file_urls failed")
    return out


def _scan_directory(
    directory: str,
    *,
    filter_substring: str | None,
    exclude_path: str | None,
    open_paths: dict[str, str],
    max_entries: int,
) -> tuple[list[FileEntry], bool]:
    entries: list[FileEntry] = []
    truncated = False
    exclude_norm = _normalize_path(exclude_path) if exclude_path else None
    filter_lower = filter_substring.lower() if filter_substring else None

    try:
        names = os.listdir(directory)
    except OSError as e:
        raise OSError(f"Cannot list directory {directory!r}: {e}") from e

    candidates: list[tuple[str, tuple[str, os.stat_result]]] = []
    for name in names:
        if _should_skip_filename(name):
            continue
        ext = os.path.splitext(name)[1].lower()
        if ext not in NEARBY_FILE_EXTENSIONS:
            continue
        if filter_lower and filter_lower not in name.lower():
            continue
        full = os.path.join(directory, name)
        try:
            if not os.path.isfile(full):
                continue
        except OSError:
            continue
        norm = _normalize_path(full)
        if exclude_norm and norm == exclude_norm:
            continue
        try:
            st = os.stat(full)
        except OSError:
            continue
        candidates.append((name, (full, st)))

    # Sort by mtime newest first
    candidates.sort(key=lambda item: item[1][1].st_mtime, reverse=True)

    for name, (full, st) in candidates:
        if len(entries) >= max_entries:
            truncated = True
            break
        norm = _normalize_path(full)
        url = open_paths.get(norm) or _path_to_file_url(full)
        entries.append(
            FileEntry(
                path=norm,
                name=name,
                url=url,
                modified=st.st_mtime,
                size_bytes=st.st_size,
                doc_type_guess=guess_doc_type_from_path(full),
                is_open=norm in open_paths,
            )
        )
    return entries, truncated


def _entries_from_open_only(
    open_paths: dict[str, str],
    *,
    filter_substring: str | None,
    max_entries: int,
) -> tuple[list[FileEntry], bool]:
    entries: list[FileEntry] = []
    truncated = False
    filter_lower = filter_substring.lower() if filter_substring else None
    items: list[tuple[str, str, float, int]] = []
    for norm, url in open_paths.items():
        name = os.path.basename(norm)
        if _should_skip_filename(name):
            continue
        if filter_lower and filter_lower not in name.lower():
            continue
        try:
            st = os.stat(norm)
        except OSError:
            st_mtime, st_size = 0.0, 0
        else:
            st_mtime, st_size = st.st_mtime, st.st_size
        items.append((norm, url, st_mtime, st_size))
    items.sort(key=lambda x: x[2], reverse=True)
    for norm, url, mtime, size in items:
        if len(entries) >= max_entries:
            truncated = True
            break
        entries.append(
            FileEntry(
                path=norm,
                name=os.path.basename(norm),
                url=url,
                modified=mtime,
                size_bytes=size,
                doc_type_guess=guess_doc_type_from_path(norm),
                is_open=True,
            )
        )
    return entries, truncated


def list_nearby_files(
    ctx: Any,
    active_model: Any,
    *,
    filter: str | None = None,
    max_entries: int = _DEFAULT_MAX_ENTRIES,
) -> dict[str, Any]:
    """List nearby office files for the outer document_research agent.

    Returns a dict with ``files``, ``truncated``, and optional ``listing_root``.
    """
    active_path = get_document_path(active_model)
    exclude_path = _normalize_path(active_path) if active_path else None
    open_paths = _collect_open_file_urls(ctx, exclude_path=exclude_path)

    listing_root = resolve_listing_directory(ctx, active_model)
    if listing_root:
        try:
            files, truncated = _scan_directory(
                listing_root,
                filter_substring=filter,
                exclude_path=exclude_path,
                open_paths=open_paths,
                max_entries=max_entries,
            )
            return {"status": "ok", "files": files, "truncated": truncated, "listing_root": listing_root}
        except OSError as e:
            return {"status": "error", "message": str(e), "details": {"path": listing_root}}

    # No directory: list other open documents only
    files, truncated = _entries_from_open_only(open_paths, filter_substring=filter, max_entries=max_entries)
    if not files:
        return {
            "status": "error",
            "message": "No nearby files found. Save the document or open sibling files in LibreOffice.",
        }
    return {"status": "ok", "files": files, "truncated": truncated, "listing_root": None}


def resolve_path_or_name(
    ctx: Any,
    active_model: Any,
    path_or_name: str,
    *,
    filter: str | None = None,
) -> tuple[str | None, str | None]:
    """Resolve a path or basename to an absolute path and file URL.

    Returns (path, url) or (None, error_message).
    """
    raw = str(path_or_name).strip()
    if not raw:
        return None, "path_or_name is required"

    if os.path.isabs(raw) and os.path.isfile(raw):
        norm = _normalize_path(raw)
        return norm, _path_to_file_url(norm)

    listing = list_nearby_files(ctx, active_model, filter=filter or raw, max_entries=_DEFAULT_MAX_ENTRIES)
    if listing.get("status") != "ok":
        return None, listing.get("message", "Could not resolve file")

    files: list[FileEntry] = listing.get("files") or []
    if not files:
        return None, f"No file matching {raw!r}"

    raw_lower = raw.lower()
    # Exact basename match first
    for entry in files:
        if entry["name"].lower() == raw_lower or entry["path"].lower() == raw_lower:
            return entry["path"], entry["url"]

    # Substring on basename (newest-first list already)
    matches = [e for e in files if raw_lower in e["name"].lower()]
    if len(matches) == 1:
        return matches[0]["path"], matches[0]["url"]
    if len(matches) > 1:
        names = ", ".join(e["name"] for e in matches[:5])
        return None, f"Multiple files match {raw!r}: {names}"

    if len(files) == 1:
        return files[0]["path"], files[0]["url"]

    return None, f"No file matching {raw!r}"


def _create_property_value(name: str, value: Any) -> Any:
    p = cast("Any", uno.createUnoStruct("com.sun.star.beans.PropertyValue"))
    p.Name = name
    p.Value = value
    return p


def _document_type_to_string(doc_type: DocumentType) -> str:
    if doc_type == DocumentType.CALC:
        return "calc"
    if doc_type in (DocumentType.DRAW, DocumentType.IMPRESS):
        return "draw"
    if doc_type == DocumentType.WRITER:
        return "writer"
    return "unknown"


def open_document_for_read(ctx: Any, path_or_url: str) -> tuple[Any | None, str | None, str | None, bool]:
    """Open or reuse a document hidden+read-only.

    Returns (model, doc_type, error_message, opened_for_document_research). The last flag is True only
    when this call loaded a new hidden document; callers must pass it to
    :func:`close_document_research_document` after the read finishes. Reused desktop documents are not closed.
    """
    from plugin.framework.uno_context import get_desktop

    path: str | None = None
    url: str | None = None
    raw = str(path_or_url).strip()
    if raw.startswith("file://"):
        url = raw
        path = _system_path_from_url(url)
    elif os.path.isabs(raw) and os.path.isfile(raw):
        path = _normalize_path(raw)
        url = _path_to_file_url(path)
    else:
        return None, None, f"Invalid path or URL: {raw!r}", False

    if not path or not url:
        return None, None, "Could not resolve path", False

    existing, existing_type = resolve_document_by_url(ctx, url)
    if existing is not None:
        return existing, existing_type or _document_type_to_string(get_document_type(existing)), None, False

    try:
        desktop = get_desktop(ctx)
        load_props = (
            _create_property_value("Hidden", True),
            _create_property_value("ReadOnly", True),
        )
        model = desktop.loadComponentFromURL(url, "_default", 0, load_props)
        if model is None:
            return None, None, f"Failed to open {path}", False
        doc_type = _document_type_to_string(get_document_type(model))
        if doc_type == "unknown":
            return None, None, f"Unsupported document type for {path}", False
        return model, doc_type, None, True
    except Exception as e:
        log.exception("open_document_for_read failed for %s", path)
        return None, None, f"Failed to open {path}: {e}", False


def close_document_research_document(model: Any, *, opened_for_document_research: bool) -> None:
    """Close a sibling document opened by :func:`open_document_for_read` for document_research read.

    Bugfix: without this, repeated delegate_read_document calls leave hidden LO components open.
    Only closes when *opened_for_document_research* is True (not when reusing a user-visible open doc).
    """
    if not opened_for_document_research or model is None:
        return
    try:
        close_fn = getattr(model, "close", None)
        if callable(close_fn):
            close_fn(True)
    except Exception:
        log.exception("Failed to close document_research read document")
