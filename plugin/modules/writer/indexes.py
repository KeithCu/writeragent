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


class IndexesUpdateAll(ToolWriterIndexBase):
    name = "indexes_update_all"
    intent = "navigate"
    description = (
        "Refresh/update all document indexes (table of contents, bibliography, etc.)."
    )
    parameters = {"type": "object", "properties": {}, "required": []}
    uno_services = ["com.sun.star.text.TextDocument"]
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
    uno_services = ["com.sun.star.text.TextDocument"]
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


class IndexesCreate(ToolWriterIndexBase):
    name = "indexes_create"
    intent = "edit"
    description = (
        "Create a new document index (e.g. TOC). Full UNO setup is model-specific; "
        "prefer inserting the index via LibreOffice (Insert > Table of Contents) "
        "then call indexes_update_all."
    )
    parameters = {
        "type": "object",
        "properties": {
            "index_kind": {
                "type": "string",
                "description": "Intended index type label (e.g. toc, bibliography).",
            },
        },
        "required": [],
    }
    uno_services = ["com.sun.star.text.TextDocument"]
    is_mutation = True

    def execute(self, ctx, **kwargs):
        return self._tool_error(
            "indexes_create is not automated in WriterAgent yet. "
            "Insert the index in LibreOffice, then use indexes_update_all."
        )


class IndexesAddMark(ToolWriterIndexBase):
    name = "indexes_add_mark"
    intent = "edit"
    description = (
        "Add an index mark (e.g. TOC entry) at the current selection. "
        "Reserved for future UNO wiring."
    )
    parameters = {
        "type": "object",
        "properties": {
            "mark_text": {
                "type": "string",
                "description": "Visible text or entry key for the mark.",
            },
        },
        "required": [],
    }
    uno_services = ["com.sun.star.text.TextDocument"]
    is_mutation = True

    def execute(self, ctx, **kwargs):
        return self._tool_error(
            "indexes_add_mark is not implemented yet. "
            "Use Writer menus to insert index marks."
        )
