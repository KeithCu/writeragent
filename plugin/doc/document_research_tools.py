# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
"""Document research outer-agent tools: list nearby files in the same folder."""

from __future__ import annotations

import logging
from typing import Any, ClassVar, Literal

from plugin.framework.tool import ToolBase, ToolContext
from plugin.doc.document_research import list_nearby_files

log = logging.getLogger(__name__)


class ListNearbyFiles(ToolBase):
    """List office files in the active document's directory (or LO Work folder if untitled)."""

    name = "list_nearby_files"
    description = (
        "List files in the same folder as the active document (newest first). "
        "Default file_kind documents: LibreOffice formats (.odt, .ods, .odp, .odg, flat XML, templates). "
        "file_kind images: .png, .jpg, .jpeg, .gif, .webp, .bmp, .svg only (discovery; not readable via delegate_read_document). "
        "Excludes the active file. Optional filter is a case-insensitive substring on the basename."
    )
    tier = "specialized"
    specialized_domain: ClassVar[str | None] = "document_research"
    specialized_cross_cutting: ClassVar[bool] = True
    is_mutation = False
    parameters = {
        "type": "object",
        "properties": {
            "filter": {"type": "string", "description": "Optional basename substring (e.g. 'budget')."},
            "file_kind": {
                "type": "string",
                "enum": ["documents", "images"],
                "description": "documents (default): office files. images: photos/diagrams in the folder.",
            },
        },
        "required": [],
    }

    def is_async(self) -> bool:
        return True

    def execute(self, ctx: ToolContext, **kwargs: Any) -> dict[str, Any]:
        from plugin.framework.queue_executor import execute_on_main_thread

        filt = kwargs.get("filter")
        file_kind_raw = kwargs.get("file_kind")
        file_kind: Literal["documents", "images"] = "images" if file_kind_raw == "images" else "documents"

        def _run() -> dict[str, Any]:
            return list_nearby_files(ctx.ctx, ctx.doc, filter=filt, file_kind=file_kind)

        return execute_on_main_thread(_run)
