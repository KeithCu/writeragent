# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
"""MCP-tier document file operations: open, save, and export."""

from __future__ import annotations

import logging
import os
from typing import Any, NamedTuple

from plugin.doc.document_research import document_entry, resolve_file_target
from plugin.framework.doc_type import doc_type_label_for_enum, get_document_type
from plugin.framework.errors import is_disposal
from plugin.framework.tool import ToolBase, ToolContext
from plugin.framework.uno_context import create_property_value, get_desktop, resolve_document_by_url

log = logging.getLogger("writeragent.document_file_tools")

# Export filter per distinct document type and lowercase extension. Names are LibreOffice
# FilterFactory names for that module; a filter from another module crashes soffice, so the
# table is keyed by the document's own type and no caller can pass a filter name.
_FILTERS: dict[str, dict[str, str]] = {
    "writer": {
        "odt": "writer8",
        "ott": "writer8_template",
        "docx": "MS Word 2007 XML",
        "doc": "MS Word 97",
        "rtf": "Rich Text Format",
        "txt": "Text",
        "html": "HTML (StarWriter)",
        "pdf": "writer_pdf_Export",
    },
    "calc": {
        "ods": "calc8",
        "ots": "calc8_template",
        "xlsx": "Calc MS Excel 2007 XML",
        "xls": "MS Excel 97",
        "csv": "Text - txt - csv (StarCalc)",
        "html": "HTML (StarCalc)",
        "pdf": "calc_pdf_Export",
    },
    "impress": {
        "odp": "impress8",
        "otp": "impress8_template",
        "pptx": "Impress MS PowerPoint 2007 XML",
        "ppt": "MS PowerPoint 97",
        "pdf": "impress_pdf_Export",
    },
    "draw": {
        "odg": "draw8",
        "otg": "draw8_template",
        "pdf": "draw_pdf_Export",
        "png": "draw_png_Export",
        "svg": "draw_svg_Export",
    },
}

# Filters without the IMPORT flag: storeAsURL rejects them (ERRCODE_IO_INVALIDPARAMETER).
_EXPORT_ONLY: frozenset[str] = frozenset({"pdf", "png", "svg"})


def _format_list(doc_label: str, *, save: bool) -> str:
    return ", ".join(f".{e}" for e in _FILTERS[doc_label] if not (save and e in _EXPORT_ONLY))


class _FilterError(NamedTuple):
    code: str
    message: str


def export_filter(doc_label: str, path: str, *, save: bool) -> str | _FilterError:
    """Return a LibreOffice filter name or _FilterError(code, message) for *doc_label* and *path*."""
    table = _FILTERS.get(doc_label)
    if table is None:
        return _FilterError(
            "UNSUPPORTED_DOCUMENT",
            "Unsupported document type (only Writer, Calc, Impress, and Draw are supported).",
        )
    ext = os.path.splitext(path)[1].lower().lstrip(".")
    if save and ext in _EXPORT_ONLY and ext in table:
        return _FilterError("EXPORT_ONLY_FORMAT", f"'.{ext}' is an export-only format; use document_export instead.")
    filter_name = table.get(ext)
    if filter_name is None:
        allowed = _format_list(doc_label, save=save)
        return _FilterError("UNKNOWN_FORMAT", f"Unsupported extension '.{ext}' for {doc_label}. Allowed: {allowed}.")
    return filter_name


def _doc_label(doc: Any) -> str:
    return doc_type_label_for_enum(get_document_type(doc))


def _resolve_save_target(
    doc: Any,
    raw_path: Any,
    *,
    save: bool,
) -> tuple[str, str, str] | _FilterError:
    """Validate target path and document type; return (path, url, filter_name) or _FilterError."""
    target = resolve_file_target(raw_path)
    if target is None:
        return _FilterError("INVALID_PATH", "Path must be an absolute file path or file:// URL.")
    path, url = target
    res = export_filter(_doc_label(doc), path, save=save)
    if isinstance(res, _FilterError):
        return res
    parent = os.path.dirname(path)
    if not os.path.isdir(parent):
        return _FilterError("DIRECTORY_NOT_FOUND", f"Target folder does not exist: {parent}")
    return path, url, res


class DocumentOpen(ToolBase):
    """Open a document file or instantiate an untitled copy from a template."""

    name: str | None = "document_open"
    description: str = (
        "Open a document file in LibreOffice from an absolute path or file:// URL. "
        "If that file is already open, returns that open document without reloading it. "
        "With as_template=true, creates a new untitled document from a copy of the file and leaves the file untouched "
        "(template files such as .ott/.ots/.otp always open this way). "
        "Returns the document's url and uid; pass one of them as document_url on later tool calls to keep working on it."
    )
    tier: str = "mcp"
    is_mutation: bool | None = False
    requires_document: bool = False
    parameters: dict[str, Any] | None = {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "Absolute file path or file:// URL to open.",
            },
            "as_template": {
                "type": "boolean",
                "default": False,
                "description": "Open as an untitled copy without modifying the source file.",
            },
        },
        "required": ["path"],
    }

    def execute(self, ctx: ToolContext, **kwargs: Any) -> dict[str, Any]:
        target = resolve_file_target(kwargs.get("path", ""))
        if target is None:
            return self._tool_error("Path must be an absolute file path or file:// URL.", code="INVALID_PATH")
        path, url = target
        if not os.path.isfile(path):
            return self._tool_error(f"File not found: {path}", code="FILE_NOT_FOUND")

        as_template = bool(kwargs.get("as_template", False))
        if not as_template:
            existing, _t = resolve_document_by_url(ctx.ctx, url)
            if existing is not None:
                return {"status": "ok", "already_open": True, "document": document_entry(existing)}

        try:
            desktop = get_desktop(ctx.ctx)
            props = (create_property_value("AsTemplate", True),) if as_template else ()
            model = desktop.loadComponentFromURL(url, "_default", 0, props)
        except Exception as exc:
            if is_disposal(exc):
                raise
            log.exception("document_open failed for %s", path)
            return self._tool_error(f"Failed to open {path}: {exc}", code="OPEN_FAILED")

        if model is None:
            return self._tool_error(f"LibreOffice returned no document for {path}.", code="OPEN_FAILED")
        return {"status": "ok", "already_open": False, "document": document_entry(model)}


class DocumentSave(ToolBase):
    """Save the target document in place or to a new path/format."""

    name: str | None = "document_save"
    description: str = (
        "Save the target document. Without path: save in place in its current format "
        "(an untitled or read-only document needs a path). "
        "With path (absolute or file:// URL): save it there and switch the document to that file; "
        "the extension picks the format: "
        f"Writer {_format_list('writer', save=True)}; "
        f"Calc {_format_list('calc', save=True)}; "
        f"Impress {_format_list('impress', save=True)}; "
        f"Draw {_format_list('draw', save=True)}. "
        "An existing file is overwritten. PDF and images are export-only: use document_export."
    )
    tier: str = "mcp"
    is_mutation: bool | None = True
    parameters: dict[str, Any] | None = {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "Optional target absolute file path or file:// URL. Omit to save in place.",
            },
        },
        "required": [],
    }

    def execute(self, ctx: ToolContext, **kwargs: Any) -> dict[str, Any]:
        doc = ctx.doc
        raw_path = kwargs.get("path")
        if not str(raw_path or "").strip():
            if not doc.hasLocation():
                return self._tool_error(
                    "Untitled document has no file location; pass path to save it.",
                    code="NO_LOCATION",
                )
            if doc.isReadonly():
                return self._tool_error(
                    "Document is read-only; pass a new path to save a copy.",
                    code="READ_ONLY",
                )
            try:
                doc.store()
            except Exception as exc:
                if is_disposal(exc):
                    raise
                log.exception("document_save in-place failed")
                return self._tool_error(f"Failed to save document: {exc}", code="SAVE_FAILED")
            return {"status": "ok", "document": document_entry(doc)}

        resolved = _resolve_save_target(doc, raw_path, save=True)
        if isinstance(resolved, _FilterError):
            return self._tool_error(resolved.message, code=resolved.code)
        path, url, filter_name = resolved

        try:
            doc.storeAsURL(url, (create_property_value("FilterName", filter_name),))
        except Exception as exc:
            if is_disposal(exc):
                raise
            log.exception("document_save failed for %s", path)
            return self._tool_error(f"Failed to save to {path}: {exc}", code="SAVE_FAILED")
        return {"status": "ok", "document": document_entry(doc)}


class DocumentExport(ToolBase):
    """Export a copy of the target document without changing its current file or format."""

    name: str | None = "document_export"
    description: str = (
        "Write a copy of the target document to path (absolute or file:// URL) in the format the extension picks: "
        f"Writer {_format_list('writer', save=False)}; "
        f"Calc {_format_list('calc', save=False)}; "
        f"Impress {_format_list('impress', save=False)}; "
        f"Draw {_format_list('draw', save=False)}. "
        "The open document keeps its current file and format. An existing file is overwritten."
    )
    tier: str = "mcp"
    is_mutation: bool | None = False
    parameters: dict[str, Any] | None = {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "Target absolute file path or file:// URL.",
            },
        },
        "required": ["path"],
    }

    def requires_document_lock(self, arguments: Any = None) -> bool:
        return True

    def execute(self, ctx: ToolContext, **kwargs: Any) -> dict[str, Any]:
        doc = ctx.doc
        resolved = _resolve_save_target(doc, kwargs.get("path", ""), save=False)
        if isinstance(resolved, _FilterError):
            return self._tool_error(resolved.message, code=resolved.code)
        path, url, filter_name = resolved

        try:
            doc.storeToURL(url, (create_property_value("FilterName", filter_name),))
        except Exception as exc:
            if is_disposal(exc):
                raise
            log.exception("document_export failed for %s", path)
            return self._tool_error(f"Failed to export to {path}: {exc}", code="EXPORT_FAILED")
        return {"status": "ok", "path": path, "url": url, "document": document_entry(doc)}
