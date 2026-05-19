# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
"""transform_document_structure tool — Collabora-compatible slide transform DSL."""

from plugin.draw.transform_engine import SlideCommandEngine
from plugin.draw.transform_schema import COLLABORA_TRANSFORM_DSL_URL, TRANSFORM_PARAM_DESCRIPTION, parse_transform_argument
from plugin.framework.tool import ToolBase, ToolBaseDummy


class TransformDocumentStructure(ToolBase, ToolBaseDummy):  # type: ignore[misc]
    """Apply a JSON command sequence to transform Draw/Impress document structure.

    Disabled for default LLM tool lists (ToolBaseDummy). Re-enable by using ToolBase only.
    Tests and manual calls: ``TransformDocumentStructure().execute(ctx, transform=...)``.
    """

    name = "transform_document_structure"
    intent = "edit"
    tier = "core"
    is_mutation = True
    uno_services = ["com.sun.star.drawing.DrawingDocument", "com.sun.star.presentation.PresentationDocument"]
    description = (
        "Transform the currently-open document's structure using a JSON command sequence. "
        "Supports Impress slide operations (navigation, layouts, text, formatting) and document-level UNO commands. "
        "Canonical DSL: %s\n\n%s"
    ) % (COLLABORA_TRANSFORM_DSL_URL, TRANSFORM_PARAM_DESCRIPTION)
    parameters = {
        "type": "object",
        "properties": {
            "transform": {"type": "string", "description": "JSON transformation commands (Collabora SlideCommands schema)."},
            "summary": {
                "type": "string",
                "description": "Optional markdown summary of changes (reserved for future approval UI; ignored in V1).",
            },
        },
        "required": ["transform"],
    }

    def execute(self, ctx, **kwargs):
        raw = kwargs.get("transform")
        transform_obj, err = parse_transform_argument(raw)
        if err:
            return self._tool_error(err)
        assert transform_obj is not None
        engine = SlideCommandEngine(ctx)
        result = engine.apply(transform_obj)
        if result.get("status") == "error":
            return self._tool_error(result.get("message", "Transform failed"), applied=result.get("applied"), warnings=result.get("warnings"))
        return result
