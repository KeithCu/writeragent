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
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, TypedDict

import uno

from plugin.doc.document_helpers import DocumentType, get_document_path, get_document_type, resolve_document_by_url
from plugin.embeddings.embeddings_fs import ALL_INDEXABLE_EXTENSIONS

if TYPE_CHECKING:
    from plugin.framework.tool import ToolBase

log = logging.getLogger(__name__)

NEARBY_FILE_EXTENSIONS = ALL_INDEXABLE_EXTENSIONS

NEARBY_IMAGE_EXTENSIONS = frozenset(
    {
        ".png",
        ".jpg",
        ".jpeg",
        ".gif",
        ".webp",
        ".bmp",
        ".svg",
    }
)

FileKind = Literal["documents", "images"]
DocTypeGuess = Literal["writer", "calc", "draw", "image", "unknown"]

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
    ".docx": "writer",
    ".doc": "writer",
    ".rtf": "writer",
    ".txt": "writer",
    ".ods": "calc",
    ".ots": "calc",
    ".fods": "calc",
    ".xlsx": "calc",
    ".xls": "calc",
    ".csv": "calc",
    ".odp": "draw",
    ".otp": "draw",
    ".fodp": "draw",
    ".odg": "draw",
    ".pptx": "draw",
    ".ppt": "draw",
    ".png": "image",
    ".jpg": "image",
    ".jpeg": "image",
    ".gif": "image",
    ".webp": "image",
    ".bmp": "image",
    ".svg": "image",
}


def _extensions_for_file_kind(file_kind: FileKind) -> frozenset[str]:
    if file_kind == "images":
        return NEARBY_IMAGE_EXTENSIONS
    return NEARBY_FILE_EXTENSIONS


def guess_doc_type_from_path(path: str) -> DocTypeGuess:
    """Map a filesystem path extension to writer/calc/draw/image."""
    ext = os.path.splitext(path)[1].lower()
    return _EXTENSION_DOC_TYPE.get(ext, "unknown")


def get_document_research_workflow_hint(ctx=None) -> str:
    """Outer document_research sub-agent workflow text."""
    from plugin.framework.constants import folder_search_enabled

    common = (
        "\n\nDocument research workflow:\n"
        "To do the task (summarize, extract, analyze, answer from document content), use delegate_read_document. "
        "That opens the file and runs one or more specialized read tasks with full read tools on that document — this is the main path.\n"
        "To find the proper filename when the user gives a partial or inexact name, use list_nearby_files first. "
        "Use file_kind=images on list_nearby_files for photos/images. "
        "Pass filter with a substring from their description (e.g. filter='budget' for \"the budget spreadsheet\"), "
        "then delegate_read_document on the matched file name. One delegate_read_document per office file.\n"
    )
    grep_hint = (
        "When you are unsure which file — the task names a keyword but no filename — "
        "use grep_nearby_files to see which nearby files match. "
        "It returns snippet only, not enough for any real work; use it only for file name discovery, then delegate_read_document for the real task on that document.\n"
        "Do not use grep_nearby_files when list_nearby_files can resolve the filename (including partial matches, or when you already know "
        "the target file). If you know which file(s) to read — go straight to delegate_read_document instead.\n"
    )
    index_hint = (
        "For cross-file discovery when the filename is unknown, use search_nearby_files(query, k) on the active folder index; "
        "it combines keyword ranking (BM25/NEAR) and semantic embeddings into one fused result list. "
        "Returns ranked doc_url, score, snippet, and optional para_index (weak hint). "
        "Open the top one or few hits with delegate_read_document and tell the inner read agent to search for the snippet "
        "or topic with search_in_document — do not rely on para_index or character offsets as exact LO coordinates.\n"
        "If search_nearby_files returns status indexing, retry after the background index finishes.\n"
    )
    if folder_search_enabled():
        return common + index_hint
    return common + grep_hint


def filter_document_research_discovery_tools(tools: list[ToolBase], ctx) -> list[ToolBase]:
    """Hide cross-file search tools that do not apply to the current folder_search_mode; list/delegate always kept."""
    from plugin.framework.constants import folder_search_enabled

    hidden: set[str] = {"search_embeddings"}
    if folder_search_enabled():
        hidden.add("grep_nearby_files")
    else:
        hidden.add("search_nearby_files")
    return [t for t in tools if t.name not in hidden]


def _normalize_path(path: str) -> str:
    return os.path.normpath(os.path.abspath(path))


def _is_absolute_or_posix_absolute(path: str) -> bool:
    return os.path.isabs(path) or path.startswith("/")


def _path_to_file_url(path: str) -> str:
    """Build a LO-compatible file URL (file:/// on Unix).

    urljoin('file:', ...) wrongly yields file:/home/... (two slashes);
    loadComponentFromURL and open_document_for_read require file:///home/...
    """
    norm = _normalize_path(path)
    return Path(norm).as_uri()


def _normalize_file_url(url: str) -> str:
    """Repair file:/path URLs from the old urljoin-based _path_to_file_url."""
    raw = str(url).strip()
    if raw.startswith("file:///"):
        return raw
    if raw.startswith("file:/") and not raw.startswith("file://"):
        return "file://" + raw[len("file:") :]
    return raw


def _system_path_from_url(url: str) -> str | None:
    if not url or not str(url).startswith("file:"):
        return None
    url = _normalize_file_url(str(url))
    if not url.startswith("file://"):
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


def _path_settings_from_ctx(ctx: Any) -> Any | None:
    """Return LO PathSettings singleton, or None when UNO is unavailable."""
    if ctx is None:
        return None
    try:
        if hasattr(ctx, "getValueByName"):
            settings = ctx.getValueByName("/singletons/com.sun.star.util.thePathSettings")
            if settings is not None:
                return settings
    except Exception:
        log.debug("_path_settings_from_ctx singleton lookup failed", exc_info=True)
    try:
        smgr = ctx.ServiceManager
        return smgr.createInstanceWithContext("com.sun.star.util.PathSettings", ctx)
    except Exception:
        log.debug("_path_settings_from_ctx createInstance failed", exc_info=True)
    return None


def _substitute_lo_path_variables(ctx: Any, raw: str) -> str:
    """Expand LO path variables such as $(home) in a PathSettings value."""
    text = str(raw).strip()
    if not text:
        return ""
    try:
        if hasattr(ctx, "getValueByName"):
            subst = ctx.getValueByName("/singletons/com.sun.star.util.thePathSubstitution")
            if subst is not None and hasattr(subst, "substituteVariables"):
                return str(subst.substituteVariables(text, True)).strip()
    except Exception:
        log.debug("_substitute_lo_path_variables failed", exc_info=True)
    return text


def _resolve_lo_directory_path(ctx: Any, raw: str) -> str | None:
    """Normalize a PathSettings directory value to an existing absolute path."""
    text = _substitute_lo_path_variables(ctx, raw)
    if not text:
        return None
    if text.startswith("file://"):
        resolved = _system_path_from_url(text)
    else:
        resolved = _normalize_path(text)
    if resolved and os.path.isdir(resolved):
        return resolved
    return None


def get_work_directory(ctx: Any) -> str | None:
    """Return LibreOffice My Documents folder (Work path setting), or None."""
    settings = _path_settings_from_ctx(ctx)
    if settings is None:
        return None
    work_raw: Any = None
    try:
        work_raw = settings.getPropertyValue("Work")
    except Exception:
        log.debug("get_work_directory getPropertyValue(Work) failed", exc_info=True)
    if work_raw is None:
        work_raw = getattr(settings, "Work", None)
    if work_raw is None:
        return None
    return _resolve_lo_directory_path(ctx, str(work_raw))


def resolve_listing_directory(ctx: Any, active_model: Any) -> str | None:
    """Directory to scan: active doc parent, else LO Work path, else None (open-docs fallback)."""
    parent = get_document_directory(active_model)
    if parent:
        return parent
    return get_work_directory(ctx)


def _collect_open_file_urls(
    ctx: Any,
    *,
    exclude_path: str | None,
    extensions: frozenset[str],
) -> dict[str, str]:
    """Map normalized path -> file URL for open LO components matching *extensions*."""
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
            if ext not in extensions:
                continue
            out[_normalize_path(path)] = str(url)
    except Exception:
        log.exception("_collect_open_file_urls failed")
    return out


def _scan_directory(
    directory: str,
    *,
    extensions: frozenset[str],
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
        if ext not in extensions:
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
    file_kind: FileKind = "documents",
    max_entries: int = _DEFAULT_MAX_ENTRIES,
) -> dict[str, Any]:
    """List nearby files for the outer document_research agent.

    *file_kind* ``documents`` (default): LibreOffice office formats only.
    *file_kind* ``images``: image files only (listable; not readable via delegate_read_document).

    Returns a dict with ``files``, ``truncated``, and optional ``listing_root``.
    """
    extensions = _extensions_for_file_kind(file_kind)
    active_path = get_document_path(active_model)
    exclude_path = _normalize_path(active_path) if active_path else None
    open_paths = _collect_open_file_urls(ctx, exclude_path=exclude_path, extensions=extensions)

    listing_root = resolve_listing_directory(ctx, active_model)
    if listing_root:
        try:
            files, truncated = _scan_directory(
                listing_root,
                extensions=extensions,
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
    file_kind: FileKind = "documents",
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

    listing = list_nearby_files(
        ctx, active_model, filter=filter or raw, file_kind=file_kind, max_entries=_DEFAULT_MAX_ENTRIES
    )
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
    if raw.startswith("file:"):
        url = _normalize_file_url(raw)
        path = _system_path_from_url(url)
    elif _is_absolute_or_posix_absolute(raw) and os.path.isfile(raw):
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
        from plugin.writer.format import create_property_value
        desktop = get_desktop(ctx)
        load_props = (
            create_property_value("Hidden", True),
            create_property_value("ReadOnly", True),
        )
        model = desktop.loadComponentFromURL(url, "_default", 0, load_props)
        if model is None:
            return None, None, f"Failed to open {path}", False
        doc_type = _document_type_to_string(get_document_type(model))
        if doc_type == "unknown":
            return None, None, f"Unsupported document type for {path}", False
        from plugin.framework.thread_guard import guard_uno

        return guard_uno(model), doc_type, None, True
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


def _is_same_document(model: Any, active_model: Any) -> bool:
    """Proxy-safe document identity for the is_active flag.

    ``model == active_model`` is ALWAYS False on guard-enabled builds: both sides are fresh
    _UnoThreadGuardProxy instances (no __eq__), so the old identity compare reported
    is_active=false even for the genuinely active document. Compare the stable runtime uid
    instead, falling back to the URL for models without one."""
    if model is None or active_model is None:
        return False
    from plugin.doc.document_helpers import get_runtime_uid

    try:
        ua, ub = get_runtime_uid(model), get_runtime_uid(active_model)
        if ua and ub:
            return ua == ub
    except Exception:
        pass
    try:
        url = str(getattr(model, "URL", "") or "")
        return bool(url) and url == str(getattr(active_model, "URL", "") or "")
    except Exception:
        return False


def _is_modified(model: Any) -> bool:
    """doc.isModified() (XModifiable), best-effort. Lets an agent SEE unsaved changes so it can
    tell the user to save. The agent never saves itself (user owns persistence)."""
    try:
        return bool(model.isModified())
    except Exception:
        return False


def get_open_documents(uno_ctx: Any, active_model: Any = None) -> list[dict[str, Any]]:
    """Retrieve all open documents from the desktop context with metadata."""
    from plugin.framework.thread_guard import assert_main_thread
    from plugin.framework.uno_context import get_desktop
    from plugin.doc.document_helpers import get_document_type, DocumentType, get_runtime_uid
    import os

    assert_main_thread("document_research.get_open_documents")
    desktop = get_desktop(uno_ctx)
    comps = desktop.getComponents()
    if not comps:
        return []
    enum = comps.createEnumeration()
    docs = []
    while enum and enum.hasMoreElements():
        elem = enum.nextElement()
        model = elem
        if hasattr(elem, "getController") and elem.getController():
            model = elem.getController().getModel()
        if model is None or not hasattr(model, "getURL"):
            continue
        # Same filter as MCP's _real_active_document: the Start Center is a live component but
        # not an OfficeDocument, so skip it — listing it would confuse multi-document targeting.
        try:
            if not model.supportsService("com.sun.star.document.OfficeDocument"):
                continue
        except Exception:
            pass  # can't introspect -> keep it (don't drop a real document)
        url = model.getURL()
        if not url:
            # An untitled doc has no URL, so its uid is the ONLY handle a caller can target it by.
            # Never drop it on a type-lookup failure -- list it with doc_type "unknown" instead.
            try:
                doc_type_enum = get_document_type(model)
                doc_type = "writer"
                if doc_type_enum == DocumentType.CALC:
                    doc_type = "calc"
                elif doc_type_enum in (DocumentType.DRAW, DocumentType.IMPRESS):
                    doc_type = "draw"
            except Exception:
                doc_type = "unknown"
                log.debug("get_open_documents: doc type lookup failed for an untitled doc", exc_info=True)
            docs.append({
                "name": "Untitled",
                "url": "",
                "uid": get_runtime_uid(model),
                "path": "",
                "doc_type": doc_type,
                "is_active": _is_same_document(model, active_model),
                "modified": _is_modified(model)
            })
            continue
        
        path = _system_path_from_url(str(url)) or ""
        doc_type_guess = guess_doc_type_from_path(path) if path else "unknown"
        if doc_type_guess == "unknown":
            try:
                doc_type_enum = get_document_type(model)
                if doc_type_enum == DocumentType.WRITER:
                    doc_type_guess = "writer"
                elif doc_type_enum == DocumentType.CALC:
                    doc_type_guess = "calc"
                elif doc_type_enum in (DocumentType.DRAW, DocumentType.IMPRESS):
                    doc_type_guess = "draw"
            except Exception:
                pass
        
        name = os.path.basename(path) if path else "Untitled"
        docs.append({
            "name": name,
            "url": str(url),
            "uid": get_runtime_uid(model),
            "path": path,
            "doc_type": doc_type_guess,
            "is_active": _is_same_document(model, active_model),
            "modified": _is_modified(model)
        })
    return docs

