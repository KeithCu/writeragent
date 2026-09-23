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

from ..specialized_base import ToolWriterMailMergeBase
from ..target_resolver import resolve_target_cursor

log = logging.getLogger("writeragent.writer.specialized.mail_merge")

import uno


def _to_file_url(path_or_url: str) -> str:
    """Convert a filesystem path or URL string into a normalized file:/// URL."""
    if not path_or_url:
        return ""
    if path_or_url.startswith("file://"):
        return path_or_url
    try:
        # import uno is unconditional; ty treats `if uno` as always-true once UNO is linked.
        if hasattr(uno, "systemPathToFileUrl"):
            res = uno.systemPathToFileUrl(os.path.abspath(path_or_url))
            if isinstance(res, str):
                return res
    except Exception:
        pass
    return Path(os.path.abspath(path_or_url)).as_uri()


def _get_database_context(ctx: Any) -> Any:
    """Obtain the com.sun.star.sdb.DatabaseContext UNO service."""
    comp_ctx = None
    if hasattr(ctx, "get_ctx"):
        comp_ctx = ctx.get_ctx()
    elif hasattr(ctx, "ctx"):
        comp_ctx = ctx.ctx

    if comp_ctx and hasattr(comp_ctx, "getServiceManager"):
        smgr = comp_ctx.getServiceManager()
        if hasattr(smgr, "createInstanceWithContext"):
            return smgr.createInstanceWithContext("com.sun.star.sdb.DatabaseContext", comp_ctx)

    # Fallback to document service factory if component context is unavailable
    doc = getattr(ctx, "doc", None)
    if doc and hasattr(doc, "createInstance"):
        return doc.createInstance("com.sun.star.sdb.DatabaseContext")

    return None


class ListDataSources(ToolWriterMailMergeBase):
    """List all registered LibreOffice data sources and inspect tables and columns."""

    name: str | None = "mail_merge_list_sources"
    intent: str | None = "examine"
    description: str = (
        "List all registered LibreOffice data sources (e.g. databases, registered spreadsheets, CSVs). "
        "Optionally inspect tables/sheets and columns by setting include_tables=True."
    )
    parameters: dict | None = {
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

        try:
            available_names = list(db_ctx.getElementNames())
        except Exception as exc:
            log.exception("Error listing database sources from DatabaseContext")
            return self._tool_error(f"Error querying registered data sources: {exc}")

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
            if include_tables:
                try:
                    ds = db_ctx.getByName(name)
                    # Establish connection to inspect schema
                    conn = ds.getConnection("", "")
                    tables_container = conn.getTables()
                    table_names = list(tables_container.getElementNames())

                    tables_info = []
                    for t_name in table_names:
                        try:
                            tbl = tables_container.getByName(t_name)
                            cols = list(tbl.getColumns().getElementNames())
                            tables_info.append({"table": t_name, "columns": cols})
                        except Exception:
                            tables_info.append({"table": t_name, "columns": []})

                    if hasattr(conn, "close"):
                        conn.close()

                    source_entry["tables"] = tables_info
                except Exception as exc:
                    source_entry["tables_error"] = str(exc)

            results.append(source_entry)

        return {
            "status": "ok",
            "count": len(results),
            "data_sources": results,
            "registered_names": available_names,
        }


class RegisterDataSource(ToolWriterMailMergeBase):
    """Register or unregister a database, spreadsheet, or CSV file in LibreOffice."""

    name: str | None = "mail_merge_register_source"
    intent: str | None = "edit"
    description: str = (
        "Register or unregister a file (.ods spreadsheet, .csv, or .odb database) as a named "
        "data source in LibreOffice, making it available for mail merge operations and merge fields."
    )
    parameters: dict | None = {
        "type": "object",
        "properties": {
            "name": {
                "type": "string",
                "description": "The unique registration name for the data source (e.g., 'Customers', 'NewsletterList').",
            },
            "file_path": {
                "type": "string",
                "description": "Path to the .ods, .csv, or .odb file to register. Required when action is 'register'.",
            },
            "action": {
                "type": "string",
                "enum": ["register", "unregister"],
                "description": "Action to perform: 'register' to add/update, or 'unregister' to remove.",
                "default": "register",
            },
        },
        "required": ["name"],
    }
    is_mutation: bool | None = True

    def execute(self, ctx: Any, **kwargs: Any) -> dict[str, Any]:
        name = kwargs.get("name")
        file_path = kwargs.get("file_path")
        action = kwargs.get("action", "register")

        if not name:
            return self._tool_error("Data source name is required.")

        db_ctx = _get_database_context(ctx)
        if not db_ctx or not hasattr(db_ctx, "registerObject"):
            return self._tool_error("Failed to access LibreOffice DatabaseContext.")

        if action == "unregister":
            try:
                if db_ctx.hasByName(name):
                    db_ctx.revokeObject(name)
                    return {"status": "ok", "message": f"Data source '{name}' unregistered successfully."}
                return {"status": "ok", "message": f"Data source '{name}' was not registered."}
            except Exception as exc:
                log.exception("Error unregistering data source")
                return self._tool_error(f"Failed to unregister data source '{name}': {exc}")

        # action == "register"
        if not file_path:
            return self._tool_error("file_path is required when action='register'.")

        file_url = _to_file_url(file_path)

        try:
            # If already registered with this name, revoke first to ensure clean update
            if db_ctx.hasByName(name):
                db_ctx.revokeObject(name)

            db_ctx.registerObject(name, file_url)
            return {
                "status": "ok",
                "message": f"Data source '{name}' registered successfully pointing to '{file_path}'.",
                "name": name,
                "file_url": file_url,
            }
        except Exception as exc:
            log.exception("Error registering data source")
            return self._tool_error(f"Failed to register data source '{name}': {exc}")


class InsertField(ToolWriterMailMergeBase):
    """Insert a database mail merge field at the specified document location."""

    name: str | None = "mail_merge_insert_field"
    intent: str | None = "edit"
    description: str = (
        "Insert a database mail merge field (e.g. <FirstName>, <Address>) at the specified position. "
        "Use target='beginning', 'end', or 'selection' to insert at those positions. "
        "Use target='search' with old_content to find and replace text with the merge field."
    )
    parameters: dict | None = {
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
            "command_type": {
                "type": "string",
                "enum": ["table", "query", "command"],
                "description": "Data command type: 'table' (0), 'query' (1), or 'command' (2).",
                "default": "table",
            },
            "target": {
                "type": "string",
                "enum": ["beginning", "end", "selection", "full_document", "search"],
                "description": "Where to insert the field.",
                "default": "selection",
            },
            "old_content": {
                "type": "string",
                "description": "Text to find and replace if target = 'search'.",
            },
        },
        "required": ["column_name"],
    }
    is_mutation: bool | None = True

    def execute(self, ctx: Any, **kwargs: Any) -> dict[str, Any]:
        column_name = kwargs.get("column_name")
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

        if not cursor:
            return self._tool_error("Failed to resolve target cursor location.")

        cmd_type_map = {"table": 0, "query": 1, "command": 2}
        cmd_type_val = cmd_type_map.get(command_type_str.lower(), 0)

        # 1. Create or retrieve Database FieldMaster
        master = None
        master_service_name = "com.sun.star.text.fieldmaster.Database"
        master_id = f"com.sun.star.text.fieldmaster.Database.{data_source_name}.{table_name}.{column_name}"

        if hasattr(doc, "getTextFieldMasters"):
            masters = doc.getTextFieldMasters()
            if hasattr(masters, "hasByName") and masters.hasByName(master_id):
                try:
                    master = masters.getByName(master_id)
                except Exception:
                    master = None

        if master is None:
            try:
                master = doc.createInstance(master_service_name)
                if not master:
                    return self._tool_error("Failed to create database field master service.")
                if hasattr(master, "setPropertyValue"):
                    master.setPropertyValue("DataBaseName", data_source_name)
                    master.setPropertyValue("DataTableName", table_name)
                    master.setPropertyValue("DataColumnName", column_name)
                    master.setPropertyValue("DataCommandType", cmd_type_val)
            except Exception as exc:
                log.exception("Error creating database field master")
                return self._tool_error(f"Error configuring field master: {exc}")

        # 2. Create TextField.Database instance
        try:
            field = doc.createInstance("com.sun.star.text.textfield.Database")
            if not field:
                return self._tool_error("Failed to create database textfield service.")
            if hasattr(field, "attachTextFieldMaster"):
                field.attachTextFieldMaster(master)
        except Exception as exc:
            log.exception("Error creating database textfield")
            return self._tool_error(f"Error instantiating database textfield: {exc}")

        if target == "search" and old_content:
            cursor.setString("")

        # 3. Insert into text cursor
        try:
            text = cursor.getText()
            text.insertTextContent(cursor, field, False)
        except Exception as exc:
            log.exception("Error inserting merge field into document")
            return self._tool_error(f"Failed to insert merge field into document: {exc}")

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
    parameters: dict | None = {"type": "object", "properties": {}, "required": []}

    def execute(self, ctx: Any, **kwargs: Any) -> dict[str, Any]:
        doc = getattr(ctx, "doc", None)
        if not doc or not hasattr(doc, "getTextFields"):
            return self._tool_error("Document does not support text fields.")

        fields_container = doc.getTextFields()
        if not hasattr(fields_container, "createEnumeration"):
            return self._tool_error("Cannot enumerate text fields in document.")

        enum = fields_container.createEnumeration()
        merge_fields = []
        field_idx = 0

        while enum.hasMoreElements():
            field = enum.nextElement()
            field_idx += 1

            # Check if this is a Database text field
            is_db_field = False
            if hasattr(field, "supportsService"):
                try:
                    is_db_field = field.supportsService("com.sun.star.text.textfield.Database")
                except Exception:
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
                except Exception:
                    pass

            if is_db_field:
                presentation = ""
                content = ""
                try:
                    presentation = field.getPresentation(False)
                except Exception:
                    presentation = f"<{col_name}>" if col_name else "DatabaseField"

                try:
                    content = field.getPresentation(True)
                except Exception:
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
    """Execute mail merge workflow to files, printer, or email using com.sun.star.text.MailMerge."""

    name: str | None = "mail_merge_run"
    intent: str | None = "edit"
    description: str = (
        "Execute a mail merge operation using LibreOffice's native MailMerge engine. "
        "Merges data from a registered data source into the document template and generates "
        "output files (ODT or PDF) or prints/emails."
    )
    parameters: dict | None = {
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
            "command_type": {
                "type": "string",
                "enum": ["table", "query", "command"],
                "description": "Command type: 'table' (0), 'query' (1), or 'command' (2).",
                "default": "table",
            },
            "output_type": {
                "type": "string",
                "enum": ["file", "printer", "mail"],
                "description": "Destination for merge results: 'file' (default), 'printer', or 'mail'.",
                "default": "file",
            },
            "output_path": {
                "type": "string",
                "description": "Directory path to save output files when output_type='file'.",
            },
            "save_as_single_file": {
                "type": "boolean",
                "description": "If True, merges all records into a single multi-page document. If False, creates separate files per record.",
                "default": False,
            },
            "file_format": {
                "type": "string",
                "enum": ["odt", "pdf"],
                "description": "Output file format: 'odt' or 'pdf'. Default 'odt'.",
                "default": "odt",
            },
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
        data_source_name = kwargs.get("data_source_name")
        table_name = kwargs.get("table_name")
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

        comp_ctx = None
        if hasattr(ctx, "get_ctx"):
            comp_ctx = ctx.get_ctx()
        elif hasattr(ctx, "ctx"):
            comp_ctx = ctx.ctx

        doc = getattr(ctx, "doc", None)

        # Resolve document URL
        doc_url = ""
        if doc_url_arg:
            doc_url = _to_file_url(doc_url_arg)
        elif doc and hasattr(doc, "getURL") and doc.getURL():
            doc_url = doc.getURL()
        else:
            return self._tool_error(
                "Document must be saved or a valid document_url provided before running mail merge."
            )

        # Instantiate com.sun.star.text.MailMerge
        mail_merge = None
        if comp_ctx and hasattr(comp_ctx, "getServiceManager"):
            smgr = comp_ctx.getServiceManager()
            if hasattr(smgr, "createInstanceWithContext"):
                mail_merge = smgr.createInstanceWithContext("com.sun.star.text.MailMerge", comp_ctx)

        if not mail_merge and doc and hasattr(doc, "createInstance"):
            mail_merge = doc.createInstance("com.sun.star.text.MailMerge")

        if not mail_merge:
            return self._tool_error("Failed to instantiate com.sun.star.text.MailMerge service.")

        # Map types
        cmd_map = {"table": 0, "query": 1, "command": 2}
        cmd_type = cmd_map.get(command_type_str.lower(), 0)

        out_map = {"printer": 1, "file": 2, "mail": 3}
        out_type = out_map.get(output_type_str.lower(), 2)

        # Configure MailMerge properties
        try:
            mail_merge.DocumentURL = doc_url
            mail_merge.DataSourceName = data_source_name
            mail_merge.CommandType = cmd_type
            mail_merge.Command = table_name
            mail_merge.OutputType = out_type

            if out_type == 2:  # File
                if not output_path:
                    output_path = os.getcwd()
                output_url = _to_file_url(output_path)
                if not output_url.endswith("/"):
                    output_url += "/"
                mail_merge.OutputURL = output_url
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
        except Exception as exc:
            log.exception("Error executing mail merge")
            return self._tool_error(f"Mail merge execution failed: {exc}")

        return {
            "status": "ok",
            "message": (
                f"Mail merge completed successfully using data source '{data_source_name}' ({table_name}). "
                f"Output destination: {output_path or output_type_str} ({file_format.upper()})."
            ),
            "output_type": output_type_str,
            "output_path": output_path,
            "save_as_single_file": save_as_single_file,
            "file_format": file_format,
        }
