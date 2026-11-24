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
import shutil
import tempfile
from typing import Any, NamedTuple

from plugin.calc.bridge import CalcBridge, filter_agent_sheet_names
from plugin.doc.document_research import HTML_WRITER_FILTER, document_entry, resolve_file_target
from plugin.framework.doc_type import DOCUMENT_SERVICES, doc_type_label_for_enum, get_document_type
from plugin.framework.errors import is_disposal
from plugin.framework.tool import ToolBase, ToolContext
from plugin.framework.uno_context import create_property_value, get_desktop, resolve_document_by_url
from plugin.framework.url_utils import path_to_file_url

log = logging.getLogger("writeragent.document_file_tools")

_CSV_FILTER = "Text - txt - csv (StarCalc)"

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
        "csv": _CSV_FILTER,
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

# Calc filters that write a single sheet. For CSV, a visible window also blocks in an
# active-sheet confirmation dialog (ScDocShell::ConvertTo in sc/source/ui/docshell/docsh.cxx).
_SINGLE_SHEET_FILTERS: frozenset[str] = frozenset({_CSV_FILTER, "DIF", "SYLK", "dBase"})

# LibreOffice's own CSV defaults (ScImportOptions in sc/source/ui/inc/imoptdlg.hxx)
# plus token 12, the 1-based sheet number.
_CSV_SHEET_OPTIONS = "44,34,UTF-8,1,,0,false,true,false,false,false,{sheet}"

_PLAIN_TEXT_LOSS = "Plain text keeps only the text: formatting, images, headers and footers are not in the file."

# What a save in this filter leaves out of the file. document_save keeps the document on
# that format, so every later in-place save leaves out the same content.
_LOSSY_FILTERS: dict[str, str] = {
    "Text": _PLAIN_TEXT_LOSS,
    "Text (encoded)": _PLAIN_TEXT_LOSS,
    "HTML (StarWriter)": "HTML keeps text and basic formatting; page layout and other document features may be lost.",
    "HTML (StarCalc)": "HTML keeps displayed values and basic formatting; formulas are not in the file.",
    _CSV_FILTER: "CSV keeps the cell values of one sheet; formulas and formatting are not in the file.",
}

_HTML_WEB_FILTER = "HTML"


def _current_filter(doc: Any) -> str:
    return next((p.Value for p in doc.getArgs() if p.Name == "FilterName"), "")


def _save_result(doc: Any) -> dict[str, Any]:
    """Result of a successful save, with a warning when the document's format drops content."""
    result: dict[str, Any] = {"status": "ok", "document": document_entry(doc)}
    loss = _LOSSY_FILTERS.get(_current_filter(doc))
    if loss:
        result["warning"] = f"{loss} Later saves without path use this format too; save to an ODF path to keep everything."
    return result


def _detect_load_filter(uno_ctx: Any, url: str) -> tuple[str, str]:
    """Return (filter name, document service) LibreOffice would load *url* with, or ("", "").

    ReadOnly keeps detection from opening the file for writing. The streams in the
    returned descriptor are released when this function returns, before the load.
    """
    smgr = uno_ctx.getServiceManager()
    detection = smgr.createInstanceWithContext("com.sun.star.document.TypeDetection", uno_ctx)
    _type_name, descriptor = detection.queryTypeByDescriptor(
        (create_property_value("URL", url), create_property_value("ReadOnly", True)), True
    )
    filter_name = next((p.Value for p in descriptor if p.Name == "FilterName"), "")
    if not filter_name:
        return "", ""
    factory = smgr.createInstanceWithContext("com.sun.star.document.FilterFactory", uno_ctx)
    service = next(p.Value for p in factory.getByName(filter_name) if p.Name == "DocumentService")
    return filter_name, service


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


def _sheet_required(doc: Any, filter_name: str) -> _FilterError | None:
    """Error when *filter_name* keeps one sheet and the workbook has more, else None."""
    if filter_name not in _SINGLE_SHEET_FILTERS:
        return None
    sheets = doc.getSheets()
    # LibreOffice's GetTableCount check includes hidden and leading-_ sheets, so count raw sheets here.
    count = sheets.getCount()
    if count <= 1:
        return None
    names = ", ".join(filter_agent_sheet_names(sheets.getElementNames()))
    return _FilterError(
        "SHEET_REQUIRED",
        f"This workbook has {count} sheets and this format keeps only one. "
        f"To write one sheet, call document_export with a .csv path and sheet set to one of: {names}. "
        "To keep every sheet, save to .ods or .xlsx.",
    )


def _resolve_save_target(
    doc: Any,
    raw_path: Any,
    *,
    save: bool,
    sheet: str | None = None,
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
    if sheet is not None:
        if res != _CSV_FILTER:
            return _FilterError(
                "SHEET_NOT_SUPPORTED",
                "sheet applies only to .csv exports of a Calc document; omit it for this format.",
            )
    else:
        err = _sheet_required(doc, res)
        if err is not None:
            return err
    return path, url, res


def _export_sheet_csv(doc: Any, sheet_number: int, path: str) -> None:
    """Write one sheet (1-based number) to *path* as CSV without LibreOffice's active-sheet dialog.

    With a sheet number the filter writes "<name>-<sheet>.csv" next to the given URL and
    nothing at the URL itself, so it exports into a temp folder and the file is copied.
    """
    with tempfile.TemporaryDirectory(prefix="wa_csv_") as tmp:
        doc.storeToURL(
            path_to_file_url(os.path.join(tmp, "sheet.csv")),
            (
                create_property_value("FilterName", _CSV_FILTER),
                create_property_value("FilterOptions", _CSV_SHEET_OPTIONS.format(sheet=sheet_number)),
            ),
        )
        written = next((n for n in os.listdir(tmp) if n.startswith("sheet-")), None)
        if written is None:
            # LibreOffice reports success even when it cannot create a file named after the sheet.
            raise OSError("LibreOffice wrote no CSV file for the sheet")
        shutil.copyfile(os.path.join(tmp, written), path)


class DocumentOpen(ToolBase):
    """Open a document file or instantiate an untitled copy from a template."""

    name: str | None = "document_open"
    description: str = (
        "Open a document file in LibreOffice from an absolute path or file:// URL. "
        "If that file is already open, returns that open document without reloading it. "
        "With as_template=true, creates a new untitled document from a copy of the file and leaves the file untouched "
        "(template files such as .ott/.ots/.otp always open this way). "
        "HTML opens as a Writer document; master documents, formulas and databases are not supported. "
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
            existing, existing_type = resolve_document_by_url(ctx.ctx, url)
            if existing is not None:
                if existing_type == "unknown":
                    return self._tool_error(
                        f"{path} is already open as a document type these tools cannot edit; close that window first.",
                        code="UNSUPPORTED_DOCUMENT",
                    )
                return {"status": "ok", "already_open": True, "document": document_entry(existing)}

        try:
            props: list[Any] = [create_property_value("AsTemplate", True)] if as_template else []
            # Detect the target service before loading: _default can recycle the Start
            # Center or an empty untitled window (LoadEnv::impl_searchRecycleTarget in
            # framework/source/loadenv/loadenv.cxx), so loading then closing would close
            # the user's window.
            filter_name, service = _detect_load_filter(ctx.ctx, url)
            if filter_name == _HTML_WEB_FILTER:
                # HTML defaults to Writer/Web (HTML.xcu), which get_document_type reports
                # as UNKNOWN so save-as and export fail; use HTML__StarWriter_.xcu instead.
                props.append(create_property_value("FilterName", HTML_WRITER_FILTER))
            elif service and service not in DOCUMENT_SERVICES:
                return self._tool_error(
                    f"{path} is not a supported document type (only Writer, Calc, Impress, and Draw are supported).",
                    code="UNSUPPORTED_DOCUMENT",
                )
            desktop = get_desktop(ctx.ctx)
            model = desktop.loadComponentFromURL(url, "_default", 0, tuple(props))
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
        ".txt, .csv and .html keep only part of the content and later saves stay in that format; use document_export for a copy. "
        "A workbook with several sheets cannot be saved as .csv: use document_export with sheet. "
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
            err = _sheet_required(doc, _current_filter(doc))
            if err is not None:
                return self._tool_error(err.message, code=err.code)
            try:
                doc.store()
            except Exception as exc:
                if is_disposal(exc):
                    raise
                log.exception("document_save in-place failed")
                return self._tool_error(f"Failed to save document: {exc}", code="SAVE_FAILED")
            return _save_result(doc)

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
        return _save_result(doc)


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
            "sheet": {
                "type": "string",
                "description": "Calc .csv only: name of the sheet to export. Required when the workbook has more than one sheet.",
            },
        },
        "required": ["path"],
    }

    def requires_document_lock(self, arguments: Any = None) -> bool:
        return True

    def execute(self, ctx: ToolContext, **kwargs: Any) -> dict[str, Any]:
        doc = ctx.doc
        raw_sheet = kwargs.get("sheet")
        sheet = str(raw_sheet) if str(raw_sheet or "").strip() else None
        resolved = _resolve_save_target(doc, kwargs.get("path", ""), save=False, sheet=sheet)
        if isinstance(resolved, _FilterError):
            return self._tool_error(resolved.message, code=resolved.code)
        path, url, filter_name = resolved

        sheet_number: int | None = None
        if sheet is not None:
            # Copying over an open document's file makes its next store() fail with ERRCODE_ABORT
            # or prompt in a visible window via SfxMedium::CheckFileDate (sfx2/source/doc/docfile.cxx).
            if resolve_document_by_url(ctx.ctx, url)[0] is not None:
                return self._tool_error(
                    f"{path} is open in LibreOffice; export the sheet to a different path.",
                    code="PATH_IS_OPEN",
                )
            # Keep outside the try block so UnoObjectError (with available and similar sheet names)
            # reaches execute_safe instead of becoming EXPORT_FAILED.
            sheet_number = CalcBridge(doc).get_sheet(sheet).getRangeAddress().Sheet + 1

        try:
            if sheet_number is not None:
                _export_sheet_csv(doc, sheet_number, path)
            else:
                doc.storeToURL(url, (create_property_value("FilterName", filter_name),))
        except Exception as exc:
            if is_disposal(exc):
                raise
            log.exception("document_export failed for %s", path)
            return self._tool_error(f"Failed to export to {path}: {exc}", code="EXPORT_FAILED")
        return {"status": "ok", "path": path, "url": url, "document": document_entry(doc)}
