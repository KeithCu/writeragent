# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2024 John Balis
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

"""Writer document indexes (TOC, bibliography) — specialized indexes domain."""

from plugin.modules.writer.base import ToolWriterIndexBase
from plugin.modules.writer.target_resolver import resolve_target_cursor


class IndexesUpdateAll(ToolWriterIndexBase):
    name = "indexes_update_all"
    intent = "navigate"
    description = "Refresh/update all document indexes (table of contents, bibliography, etc.)."
    parameters = {"type": "object", "properties": {}, "required": []}
    is_mutation = True

    def execute(self, ctx, **kwargs):
        doc = ctx.doc
        if not hasattr(doc, "getDocumentIndexes"):
            return self._tool_error("Document does not support indexes")
        indexes = doc.getDocumentIndexes()
        count = indexes.getCount()
        refreshed = []
        for i in range(count):
            idx = indexes.getByIndex(i)
            idx.update()
            name = idx.getName() if hasattr(idx, "getName") else "index_%d" % i
            refreshed.append(name)
        return {"status": "ok", "refreshed": refreshed, "count": count}


class RefreshIndexesAlias(ToolWriterIndexBase):
    """Same behavior as indexes_update_all (legacy name)."""

    name = "refresh_indexes"
    intent = "navigate"
    description = "Refresh all document indexes (TOC, bibliography, etc.). Alias of indexes_update_all."
    parameters = {"type": "object", "properties": {}, "required": []}
    is_mutation = True

    def execute(self, ctx, **kwargs):
        doc = ctx.doc
        if not hasattr(doc, "getDocumentIndexes"):
            return self._tool_error("Document does not support indexes")
        indexes = doc.getDocumentIndexes()
        count = indexes.getCount()
        refreshed = []
        for i in range(count):
            idx = indexes.getByIndex(i)
            idx.update()
            name = idx.getName() if hasattr(idx, "getName") else "index_%d" % i
            refreshed.append(name)
        return {"status": "ok", "refreshed": refreshed, "count": count}


class IndexesList(ToolWriterIndexBase):
    name = "indexes_list"
    intent = "navigate"
    description = "List all document indexes (table of contents, alphabetical index, bibliography, etc.)."
    parameters = {"type": "object", "properties": {}, "required": []}
    is_mutation = False

    def execute(self, ctx, **kwargs):
        doc = ctx.doc
        if not hasattr(doc, "getDocumentIndexes"):
            return self._tool_error("Document does not support indexes")
        indexes = doc.getDocumentIndexes()
        count = indexes.getCount()
        result = []
        for i in range(count):
            idx = indexes.getByIndex(i)
            name = idx.getName() if hasattr(idx, "getName") else f"index_{i}"
            title = idx.Title if hasattr(idx, "Title") else ""
            # service name is useful for knowing the type
            type_name = "unknown"
            if hasattr(idx, "getImplementationName"):
                type_name = idx.getImplementationName()
                if type_name == "SwXDocumentIndex":
                    type_name = "alphabetical"
                elif type_name == "SwXContentIndex":
                    type_name = "toc"
                elif type_name == "SwXUserIndex":
                    type_name = "user"
            result.append({"index": i, "name": name, "title": title, "type": type_name})
        return {"status": "ok", "indexes": result, "count": count}


class IndexesCreate(ToolWriterIndexBase):
    name = "indexes_create"
    intent = "edit"
    description = (
        "Create a new document index (e.g. toc, alphabetical, user, illustration, table, object, bibliography). "
        "Use target='beginning', 'end', or 'selection' to insert at those positions. "
        "Use target='search' with old_content to find and replace text. "
        "Note for future AI maintainers: Other specialized tools may also need this logic."
    )
    parameters = {
        "type": "object",
        "properties": {
            "index_kind": {
                "type": "string",
                "enum": ["toc", "alphabetical", "user", "illustration", "table", "object", "bibliography"],
                "description": "The type of index to create.",
            },
            "title": {
                "type": "string",
                "description": "The title for the index (e.g., 'Table of Contents').",
            },
            "create_from_outline": {
                "type": "boolean",
                "description": "Whether to create the index from the document outline (mainly for toc). Default true.",
            },
            "target": {
                "type": "string",
                "enum": ["beginning", "end", "selection", "full_document", "search"],
                "description": "Where to insert the index.",
            },
            "old_content": {
                "type": "string",
                "description": "Text to find and replace if target = 'search'.",
            },
        },
        "required": ["index_kind"],
    }
    is_mutation = True

    def execute(self, ctx, **kwargs):
        doc = ctx.doc
        index_kind = kwargs.get("index_kind", "toc")
        title = kwargs.get("title")
        create_from_outline = kwargs.get("create_from_outline", True)
        target = kwargs.get("target", "selection")
        old_content = kwargs.get("old_content")

        try:
            # Map simplified names to UNO services
            service_map = {
                "toc": "com.sun.star.text.ContentIndex",
                "alphabetical": "com.sun.star.text.DocumentIndex",
                "user": "com.sun.star.text.UserIndex",
                "illustration": "com.sun.star.text.IllustrationsIndex",
                "table": "com.sun.star.text.TableIndex",
                "object": "com.sun.star.text.ObjectIndex",
                "bibliography": "com.sun.star.text.Bibliography",
            }
            service_name = service_map.get(index_kind, "com.sun.star.text.ContentIndex")

            index = doc.createInstance(service_name)
            if title is not None and hasattr(index, "Title"):
                index.Title = title

            if index_kind == "toc" and hasattr(index, "CreateFromOutline"):
                index.CreateFromOutline = create_from_outline

            try:
                cursor = resolve_target_cursor(ctx, target, old_content)
            except ValueError as ve:
                return self._tool_error(str(ve))

            if not cursor:
                return self._tool_error("Failed to resolve target location.")

            if target == "search" and old_content:
                cursor.setString("")

            text = cursor.getText()
            text.insertTextContent(cursor, index, False)
            index.update()

            return {"status": "ok", "message": f"Created '{index_kind}' index successfully", "title": title}
        except Exception as e:
            return self._tool_error(f"Failed to create index: {str(e)}")


class IndexesAddMark(ToolWriterIndexBase):
    name = "indexes_add_mark"
    intent = "edit"
    description = (
        "Add an index mark (e.g. alphabetical index entry). "
        "Use target='beginning', 'end', or 'selection' to insert at those positions. "
        "Use target='search' with old_content to find and replace text. "
        "Can specify primary/secondary keys for alphabetical indexes."
    )
    parameters = {
        "type": "object",
        "properties": {
            "mark_text": {
                "type": "string",
                "description": "Visible text or entry key for the mark.",
            },
            "index_kind": {
                "type": "string",
                "enum": ["alphabetical", "user"],
                "description": "The type of index mark to create (default 'alphabetical').",
            },
            "primary_key": {
                "type": "string",
                "description": "The primary key for an alphabetical index entry (optional).",
            },
            "secondary_key": {
                "type": "string",
                "description": "The secondary key for an alphabetical index entry (optional).",
            },
            "target": {
                "type": "string",
                "enum": ["beginning", "end", "selection", "full_document", "search"],
                "description": "Where to insert the index mark.",
            },
            "old_content": {
                "type": "string",
                "description": "Text to find and replace if target = 'search'.",
            },
        },
        "required": ["mark_text"],
    }
    is_mutation = True

    def execute(self, ctx, **kwargs):
        doc = ctx.doc
        mark_text = kwargs.get("mark_text")
        index_kind = kwargs.get("index_kind", "alphabetical")
        primary_key = kwargs.get("primary_key")
        secondary_key = kwargs.get("secondary_key")
        target = kwargs.get("target", "selection")
        old_content = kwargs.get("old_content")

        try:
            cursor = resolve_target_cursor(ctx, target, old_content)
        except ValueError as ve:
            return self._tool_error(str(ve))

        if not cursor:
            return self._tool_error("Failed to resolve target location.")

        try:
            service_name = "com.sun.star.text.DocumentIndexMark"
            if index_kind == "user":
                service_name = "com.sun.star.text.UserIndexMark"

            mark = doc.createInstance(service_name)

            if hasattr(mark, "MarkEntry"):
                mark.MarkEntry = mark_text
            elif hasattr(mark, "PrimaryKey") and hasattr(mark, "SecondaryKey"):
                pass  # DocumentIndexMark handles these via properties

            if index_kind == "alphabetical":
                if hasattr(mark, "PrimaryKey") and primary_key is not None:
                    mark.PrimaryKey = primary_key
                if hasattr(mark, "SecondaryKey") and secondary_key is not None:
                    mark.SecondaryKey = secondary_key
                # SwXDocumentIndexMark uses these
                try:
                    mark.setPropertyValue("PrimaryKey", primary_key or "")
                    mark.setPropertyValue("SecondaryKey", secondary_key or "")
                except Exception:
                    pass

            text = cursor.getText()
            text.insertTextContent(cursor, mark, False)

            return {"status": "ok", "message": f"Added '{index_kind}' index mark for '{mark_text}'"}
        except Exception as e:
            return self._tool_error(f"Failed to add index mark: {str(e)}")
