# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
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
"""Writer Mail Merge — specialized mail_merge domain.

Provides tools for data source registration and discovery, inserting database
merge fields, listing document merge fields, and executing mail merge operations
using LibreOffice's com.sun.star.text.MailMerge and DatabaseContext services.
"""

import logging
import os
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

from ..specialized_base import ToolWriterMailMergeBase, enum_param, target_params
from ..target_resolver import resolve_target_cursor
from plugin.doc.undo import commit_or_rollback_mutation
from plugin.framework.errors import is_real_disposal, reraise_if_disposed
from plugin.framework.uno_context import get_service_manager, resolve_uno_context
from plugin.framework.url_utils import normalize_file_url

import uno

log = logging.getLogger("writeragent.writer")


def _to_file_url(path_or_url: str) -> str:
    """Convert a filesystem path or URL string into a normalized file:/// URL."""
    if not path_or_url:
        return ""
    if path_or_url.startswith("file:"):
        return normalize_file_url(path_or_url)
    try:
        # import uno is unconditional; ty treats `if uno` as always-true once UNO is linked.
        if hasattr(uno, "systemPathToFileUrl"):
            res = uno.systemPathToFileUrl(os.path.abspath(path_or_url))
            if isinstance(res, str):
                return res
    except Exception:
        log.debug("systemPathToFileUrl failed", exc_info=True)
    return Path(os.path.abspath(path_or_url)).as_uri()


def _to_system_path(path_or_url: str) -> str:
    """Convert a filesystem path or ``file:`` URL to a system path."""
    if not path_or_url:
        return ""
    if not path_or_url.startswith("file:"):
        return path_or_url
    url = normalize_file_url(path_or_url)
    try:
        if hasattr(uno, "fileUrlToSystemPath"):
            return str(uno.fileUrlToSystemPath(url))
    except Exception:
        log.debug("fileUrlToSystemPath failed", exc_info=True)
    return unquote(urlparse(url).path)


_COMMAND_TYPES = {"table": 0, "query": 1, "command": 2}  # default 0
_OUTPUT_TYPES = {"printer": 1, "file": 2}      # default 2


def _create_global_service(ctx: Any, service_name: str) -> Any:
    comp_ctx = resolve_uno_context(ctx)
    smgr = get_service_manager(comp_ctx)
    if smgr:
        return smgr.createInstanceWithContext(service_name, comp_ctx)
    return None


def _get_database_context(ctx: Any) -> Any:
    """Obtain ``com.sun.star.sdb.DatabaseContext`` from the global service manager.

    A document ``createInstance("com.sun.star.sdb.DatabaseContext")``
    cannot create this service. It is the global single-instance
    ``com.sun.star.comp.dba.ODatabaseContext`` (``services.rdb``).
    LibreOffice 26's document factory raises
    ``ServiceNotRegisteredException: unknown service``. Only
    ``getServiceManager().createInstanceWithContext`` creates it.
    """
    return _create_global_service(ctx, "com.sun.star.sdb.DatabaseContext")


class ListDataSources(ToolWriterMailMergeBase):
    """List all registered LibreOffice data sources and inspect tables and columns."""

    name: str | None = "mail_merge_list_sources"
    intent: str | None = "examine"
    description: str = (
        "List all registered LibreOffice data sources (e.g. databases, registered spreadsheets, CSVs). "
        "Optionally inspect tables/sheets and columns by setting include_tables=True."
    )
    parameters: dict[str, Any] | None = {
        "type": "object",
        "properties": {
            "include_tables": {
                "type": "boolean",
                "description": "Whether to connect to each data source and list its tables and column names.",
                "default": False,
            },
            "data_source_name": {
                "type": "string",
                "description": "Optional name to inspect a specific data source instead of all registered ones.",
            },
        },
        "required": [],
    }

    def execute(self, ctx: Any, **kwargs: Any) -> dict[str, Any]:
        include_tables = kwargs.get("include_tables", False)
        target_ds_name = kwargs.get("data_source_name")

        db_ctx = _get_database_context(ctx)
        if not db_ctx or not hasattr(db_ctx, "getElementNames"):
            return self._tool_error("Failed to access LibreOffice DatabaseContext.")

        available_names = list(db_ctx.getElementNames())

        if target_ds_name:
            if target_ds_name not in available_names:
                return self._tool_error(
                    f"Data source '{target_ds_name}' is not registered in LibreOffice. "
                    f"Available sources: {available_names}"
                )
            selected_names = [target_ds_name]
        else:
            selected_names = available_names

        results = []
        for name in selected_names:
            source_entry: dict[str, Any] = {"name": name}
            try:
                location = db_ctx.getDatabaseLocation(name)
            except Exception as exc:
                reraise_if_disposed(exc)
                location = ""
            source_entry["location"] = str(location or "")
            if include_tables:
                try:
                    ds = db_ctx.getByName(name)
                    # Establish connection to inspect schema
                    conn = ds.getConnection("", "")
                    try:
                        tables_container = conn.getTables()
                        table_names = list(tables_container.getElementNames())

                        tables_info = []
                        for t_name in table_names:
                            try:
                                tbl = tables_container.getByName(t_name)
                                cols = list(tbl.getColumns().getElementNames())
                                tables_info.append({"table": t_name, "columns": cols})
                            except Exception as tbl_exc:
                                reraise_if_disposed(tbl_exc)
                                tables_info.append({"table": t_name, "columns": []})

                        source_entry["tables"] = tables_info
                    finally:
                        # Fix: Ensure database connection is closed even if inspection fails.
                        try:
                            if hasattr(conn, "close"):
                                conn.close()
                        except Exception as exc:
                            reraise_if_disposed(exc)
                            log.debug("mail merge: closing data-source connection failed", exc_info=True)
                except Exception as exc:
                    reraise_if_disposed(exc)
                    source_entry["tables_error"] = str(exc)

            results.append(source_entry)

        return {
            "status": "ok",
            "count": len(results),
            "data_sources": results,
            "registered_names": available_names,
        }


class RegisterDataSource(ToolWriterMailMergeBase):
    """Register or unregister an .odb database file in LibreOffice."""

    name: str | None = "mail_merge_register_source"
    intent: str | None = "edit"
    description: str = (
        "Register or unregister an .odb database file as a named data source in LibreOffice, "
        "making it available for mail merge operations and merge fields."
    )
    parameters: dict[str, Any] | None = {
        "type": "object",
        "properties": {
            "name": {
                "type": "string",
                "description": "The unique registration name for the data source (e.g., 'Customers', 'NewsletterList').",
            },
            "file_path": {
                "type": "string",
                "description": "Path to the .odb file to register. Required when action is 'register'.",
            },
            "action": enum_param(
                ["register", "unregister"],
                "Action to perform: 'register' to add/update, or 'unregister' to remove.",
                default="register",
            ),
        },
        "required": ["name"],
    }
    is_mutation: bool | None = True

    def execute(self, ctx: Any, **kwargs: Any) -> dict[str, Any]:
        name = kwargs["name"]
        file_path = kwargs.get("file_path")
        action = kwargs.get("action", "register")

        if not name:
            return self._tool_error("Data source name is required.")

        # XDatabaseRegistrations methods registerDatabaseLocation / changeDatabaseLocation
        # manage named database locations by URL.
        db_ctx = _get_database_context(ctx)
        if (
            not db_ctx
            or not hasattr(db_ctx, "registerDatabaseLocation")
            or not hasattr(db_ctx, "changeDatabaseLocation")
        ):
            return self._tool_error("Failed to access LibreOffice DatabaseContext.")

        if action == "unregister":
            if db_ctx.hasByName(name):
                # Fix: Use revokeDatabaseLocation instead of XNamingService.revokeObject.
                try:
                    db_ctx.revokeDatabaseLocation(name)
                except Exception as exc:
                    reraise_if_disposed(exc)
                    return self._tool_error(
                        f"Failed to unregister data source '{name}': {exc}",
                        code="UNREGISTER_FAILED",
                        name=name,
                    )
                return {"status": "ok", "message": f"Data source '{name}' unregistered successfully."}
            return {"status": "ok", "message": f"Data source '{name}' was not registered."}

        # action == "register"
        if not file_path:
            return self._tool_error("file_path is required when action='register'.")

        path = _to_system_path(file_path)
        if not os.path.isfile(path) or not os.access(path, os.R_OK):
            return self._tool_error(
                f"Data source file not found or not readable: '{path}'.",
                code="FILE_NOT_FOUND",
                path=path,
            )

        # DatabaseContext.getByName loads registered locations as office database documents (.odb).
        # Raw .ods/.csv files cannot be registered directly without an .odb wrapper.
        if Path(path).suffix.lower() != ".odb":
            return self._tool_error(
                "Only .odb database files can be registered. Register a spreadsheet or CSV through an .odb wrapper first.",
                code="INVALID_FILE_TYPE",
            )

        file_url = _to_file_url(path)

        # Fix: XNamingService.registerObject expected an XInterface database document and failed
        # on URL strings; revoking before re-registering also lost previous registrations on failure.
        # Use changeDatabaseLocation if already registered, otherwise registerDatabaseLocation.
        try:
            if db_ctx.hasByName(name):
                db_ctx.changeDatabaseLocation(name, file_url)
            else:
                db_ctx.registerDatabaseLocation(name, file_url)
        except Exception as exc:
            reraise_if_disposed(exc)
            return self._tool_error(
                f"Failed to register data source '{name}': {exc}",
                code="REGISTRATION_FAILED",
                name=name,
                file_url=file_url,
            )

        return {
            "status": "ok",
            "message": f"Data source '{name}' registered successfully pointing to '{file_path}'.",
            "name": name,
            "file_url": file_url,
        }


class InsertField(ToolWriterMailMergeBase):
    """Insert a database mail merge field at the specified document location."""

    name: str | None = "mail_merge_insert_field"
    intent: str | None = "edit"
    description: str = (
        "Insert a database mail merge field (e.g. <FirstName>, <Address>) at the specified position. "
        "Use target='beginning', 'end', or 'selection' to insert at those positions. "
        "Use target='search' with old_content to find and replace text with the merge field."
    )
    parameters: dict[str, Any] | None = {
        "type": "object",
        "properties": {
            "column_name": {
                "type": "string",
                "description": "Name of the column/field to merge (e.g., 'FirstName', 'Company', 'Address').",
            },
            "data_source_name": {
                "type": "string",
                "description": "Name of the registered data source. Defaults to '' or active data source.",
                "default": "",
            },
            "table_name": {
                "type": "string",
                "description": "Name of the table or sheet (e.g., 'Sheet1', 'Contacts').",
                "default": "",
            },
            "command_type": enum_param(
                ["table", "query", "command"],
                "Data command type: 'table' (0), 'query' (1), or 'command' (2).",
                default="table",
            ),
            **target_params("the field", default="selection"),
        },
        "required": ["column_name"],
    }
    is_mutation: bool | None = True

    def execute(self, ctx: Any, **kwargs: Any) -> dict[str, Any]:
        column_name = kwargs["column_name"]
        data_source_name = kwargs.get("data_source_name", "")
        table_name = kwargs.get("table_name", "")
        command_type_str = kwargs.get("command_type", "table")
        target = kwargs.get("target", "selection")
        old_content = kwargs.get("old_content")

        if not column_name:
            return self._tool_error("column_name is required.")

        doc = getattr(ctx, "doc", None)
        if not doc or not hasattr(doc, "createInstance"):
            return self._tool_error("Document does not support creating UNO instances.")

        try:
            cursor = resolve_target_cursor(ctx, target, old_content)
        except ValueError as ve:
            return self._tool_error(str(ve))

        cmd_type_val = _COMMAND_TYPES.get(command_type_str.lower(), 0)

        # 1. Create or retrieve Database FieldMaster
        master = None
        master_service_name = "com.sun.star.text.fieldmaster.Database"
        master_id = f"com.sun.star.text.fieldmaster.Database.{data_source_name}.{table_name}.{column_name}"

        masters = doc.getTextFieldMasters()
        if hasattr(masters, "hasByName") and masters.hasByName(master_id):
            try:
                master = masters.getByName(master_id)
            except Exception as exc:
                reraise_if_disposed(exc)
                master = None

        if master is None:
            master = doc.createInstance(master_service_name)
            if not master:
                return self._tool_error("Failed to create database field master service.")
            if hasattr(master, "setPropertyValue"):
                master.setPropertyValue("DataBaseName", data_source_name)
                master.setPropertyValue("DataTableName", table_name)
                master.setPropertyValue("DataColumnName", column_name)
                master.setPropertyValue("DataCommandType", cmd_type_val)

        # 2. Create TextField.Database instance
        field = doc.createInstance("com.sun.star.text.textfield.Database")
        if not field:
            return self._tool_error("Failed to create database textfield service.")
        if hasattr(field, "attachTextFieldMaster"):
            field.attachTextFieldMaster(master)

        if target == "search" and old_content:
            from ..edit_review import next_agent_edit_undo_title

            undo_title = next_agent_edit_undo_title()

            def mutate() -> None:
                cursor.setString("")
                cursor.getText().insertTextContent(cursor, field, False)

            # Fix: Previously deleted matched text before inserting field without undo context;
            # if insert failed the text was permanently lost. Use commit_or_rollback_mutation.
            err = commit_or_rollback_mutation(
                self,
                doc,
                undo_title,
                mutate,
                undo_unavailable_msg="Cannot replace the matched text atomically (no usable undo context).",
                rollback_msg="Failed to insert the merge field; the change was rolled back",
            )
            if err is not None:
                return err
        else:
            try:
                cursor.getText().insertTextContent(cursor, field, False)
            except Exception as exc:
                if is_real_disposal(exc):
                    raise
                return self._tool_error(f"Failed to insert merge field: {exc}")

        return {
            "status": "ok",
            "message": f"Successfully inserted merge field '<{column_name}>'.",
            "field": {
                "column_name": column_name,
                "data_source_name": data_source_name,
                "table_name": table_name,
                "command_type": command_type_str,
            },
        }


class ListFields(ToolWriterMailMergeBase):
    """List all database mail merge fields placed in the active document."""

    name: str | None = "mail_merge_list_fields"
    intent: str | None = "examine"
    description: str = (
        "List all database merge fields currently placed in the active Writer document, "
        "including their column names, data source names, and table names."
    )
    parameters: dict[str, Any] | None = {"type": "object", "properties": {}, "required": []}

    def execute(self, ctx: Any, **kwargs: Any) -> dict[str, Any]:
        doc = getattr(ctx, "doc", None)
        if not doc:
            return self._tool_error("Document does not support text fields.")

        fields_container = doc.getTextFields()
        if not hasattr(fields_container, "createEnumeration"):
            return self._tool_error("Cannot enumerate text fields in document.")

        enum = fields_container.createEnumeration()
        merge_fields = []
        field_idx = 0

        while enum.hasMoreElements() is True:
            field = enum.nextElement()
            field_idx += 1

            # Check if this is a Database text field
            is_db_field = False
            if hasattr(field, "supportsService"):
                try:
                    is_db_field = field.supportsService("com.sun.star.text.textfield.Database")
                except Exception as exc:
                    reraise_if_disposed(exc)
                    is_db_field = False

            db_name = ""
            tbl_name = ""
            col_name = ""

            if hasattr(field, "getPropertySetInfo"):
                try:
                    info = field.getPropertySetInfo()
                    if info.hasPropertyByName("DataColumnName"):
                        is_db_field = True
                        col_name = field.getPropertyValue("DataColumnName") or ""
                    if info.hasPropertyByName("DataBaseName"):
                        db_name = field.getPropertyValue("DataBaseName") or ""
                    if info.hasPropertyByName("DataTableName"):
                        tbl_name = field.getPropertyValue("DataTableName") or ""
                except Exception as exc:
                    reraise_if_disposed(exc)
                    log.debug("mail merge: database field properties unreadable", exc_info=True)

            if is_db_field:
                presentation = ""
                content = ""
                try:
                    presentation = field.getPresentation(False)
                except Exception as exc:
                    reraise_if_disposed(exc)
                    presentation = f"<{col_name}>" if col_name else "DatabaseField"

                try:
                    content = field.getPresentation(True)
                except Exception as exc:
                    reraise_if_disposed(exc)
                    content = ""

                merge_fields.append({
                    "id": field_idx,
                    "column_name": col_name,
                    "data_source_name": db_name,
                    "table_name": tbl_name,
                    "presentation": presentation,
                    "content": content,
                })

        return {
            "status": "ok",
            "count": len(merge_fields),
            "fields": merge_fields,
        }


class RunMerge(ToolWriterMailMergeBase):
    """Execute mail merge workflow to files or printer using com.sun.star.text.MailMerge."""

    name: str | None = "mail_merge_run"
    intent: str | None = "edit"
    description: str = (
        "Execute a mail merge operation using LibreOffice's native MailMerge engine. "
        "Merges data from a registered data source into the document template and generates "
        "output files (ODT or PDF) or prints/emails."
    )
    parameters: dict[str, Any] | None = {
        "type": "object",
        "properties": {
            "data_source_name": {
                "type": "string",
                "description": "Name of the registered data source to merge from.",
            },
            "table_name": {
                "type": "string",
                "description": "Name of the table, query, or sheet in the data source.",
            },
            "command_type": enum_param(
                ["table", "query", "command"],
                "Command type: 'table' (0), 'query' (1), or 'command' (2).",
                default="table",
            ),
            "output_type": enum_param(
                ["file", "printer"],
                "Destination for merge results: 'file' (default) or 'printer'.",
                default="file",
            ),
            "output_path": {
                "type": "string",
                "description": "Directory path to save output files when output_type='file'.",
            },
            "save_as_single_file": {
                "type": "boolean",
                "description": "If True, merges all records into a single multi-page document. If False, creates separate files per record.",
                "default": False,
            },
            "file_format": enum_param(
                ["odt", "pdf"],
                "Output file format: 'odt' or 'pdf'. Default 'odt'.",
                default="odt",
            ),
            "file_name_prefix": {
                "type": "string",
                "description": "Base filename prefix for output files, or column name when file_name_from_column=True.",
                "default": "MergedDocument",
            },
            "file_name_from_column": {
                "type": "boolean",
                "description": "If True, uses values from the column specified in file_name_prefix to name individual files.",
                "default": False,
            },
            "filter": {
                "type": "string",
                "description": "Optional SQL WHERE filter clause to select specific records (e.g. \"City = 'London'\").",
            },
            "document_url": {
                "type": "string",
                "description": "Optional URL/path to template document. Defaults to the active document.",
            },
        },
        "required": ["data_source_name", "table_name"],
    }
    is_mutation: bool | None = True
    long_running: bool = True

    def execute(self, ctx: Any, **kwargs: Any) -> dict[str, Any]:
        data_source_name = kwargs["data_source_name"]
        table_name = kwargs["table_name"]
        command_type_str = kwargs.get("command_type", "table")
        output_type_str = kwargs.get("output_type", "file")
        output_path = kwargs.get("output_path")
        save_as_single_file = kwargs.get("save_as_single_file", False)
        file_format = kwargs.get("file_format", "odt")
        file_name_prefix = kwargs.get("file_name_prefix", "MergedDocument")
        file_name_from_column = kwargs.get("file_name_from_column", False)
        filter_clause = kwargs.get("filter")
        doc_url_arg = kwargs.get("document_url")

        if not data_source_name or not table_name:
            return self._tool_error("data_source_name and table_name are required.")

        doc = getattr(ctx, "doc", None)

        # Resolve document URL
        doc_url = ""
        out_dir = ""
        if doc_url_arg:
            template_path = _to_system_path(doc_url_arg)
            if not os.path.isfile(template_path):
                return self._tool_error(
                    f"Template not found: {doc_url_arg}", code="FILE_NOT_FOUND", path=template_path
                )
            doc_url = _to_file_url(template_path)
        elif doc:
            try:
                modified = doc.isModified()
                url = doc.getURL()
            except Exception as exc:
                reraise_if_disposed(exc)
                return self._tool_error("Cannot read the active document before mail merge.")
            # Fix: MailMerge reads template from disk directly via DocumentURL, ignoring in-memory
            # edits in WriterAgent. Reject if active document has unsaved changes.
            if modified is True:
                return self._tool_error(
                    "The active document has unsaved changes. Save it first, or pass document_url "
                    "for a saved template; mail merge reads the template from disk.",
                    code="DOCUMENT_MODIFIED",
                )
            if url:
                doc_url = url
        if not doc_url:
            return self._tool_error(
                "Document must be saved or a valid document_url provided before running mail merge."
            )

        # MailMerge is SwXMailMerge on the global service manager, not a document-factory
        # service. doc.createInstance raises ServiceNotRegisteredException (same class of
        # bug as ShapeCollection / DatabaseContext).
        mail_merge = _create_global_service(ctx, "com.sun.star.text.MailMerge")
        if not mail_merge:
            return self._tool_error("Failed to instantiate com.sun.star.text.MailMerge service.")

        # Map types
        cmd_type = _COMMAND_TYPES.get(command_type_str.lower(), 0)
        out_type = _OUTPUT_TYPES.get(output_type_str.lower(), 2)

        # Configure MailMerge properties
        mail_merge.DocumentURL = doc_url
        mail_merge.DataSourceName = data_source_name
        mail_merge.CommandType = cmd_type
        mail_merge.Command = table_name
        mail_merge.OutputType = out_type

        output_url = None
        if out_type == 2:  # File
            # Fix: When output_path is omitted, do not fall back to os.getcwd() which can be the
            # LO installation or root folder. Leaving OutputURL unset lets LO save to the template directory.
            if output_path:
                out_dir = _to_system_path(output_path)
                try:
                    if os.path.exists(out_dir) and not os.path.isdir(out_dir):
                        return self._tool_error(
                            f"Output path exists and is not a directory: '{output_path}'",
                            code="INVALID_OUTPUT_PATH",
                            path=out_dir,
                        )
                    os.makedirs(out_dir, exist_ok=True)
                except OSError as exc:
                    return self._tool_error(
                        f"Cannot use output directory '{output_path}': {exc}",
                        code="INVALID_OUTPUT_PATH",
                        path=out_dir,
                    )
                output_url = _to_file_url(out_dir)
                if not output_url.endswith("/"):
                    output_url += "/"
                mail_merge.OutputURL = output_url
            else:
                out_dir = os.path.dirname(_to_system_path(doc_url))
            mail_merge.SaveAsSingleFile = save_as_single_file
            mail_merge.FileNamePrefix = file_name_prefix
            mail_merge.FileNameFromColumn = file_name_from_column

            # Set filter for PDF or ODT
            if file_format.lower() == "pdf":
                mail_merge.SaveFilter = "writer_pdf_export"
            else:
                mail_merge.SaveFilter = "writer8"

        if filter_clause:
            mail_merge.Filter = filter_clause

        # Execute the merge
        mail_merge.execute([])

        # Fix: Stop claiming a file format for printer output.
        dest_msg = (
            f"Output directory: {out_dir or output_url} ({file_format.upper()})."
            if out_type == 2
            else "Output destination: printer."
        )

        return {
            "status": "ok",
            "message": (
                f"Mail merge completed successfully using data source '{data_source_name}' ({table_name}). "
                f"{dest_msg}"
            ),
            "output_type": output_type_str,
            "output_path": output_path,
            "output_url": output_url,
            "output_directory": out_dir if out_type == 2 else None,
            "save_as_single_file": save_as_single_file,
            "file_format": file_format,
        }
