# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""UNO template-fill: apply fill_plan.json to Impress via placeholders."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, cast

from plugin.draw.bridge import DrawBridge
from plugin.draw.placeholders import _find_placeholder


def apply_fill_plan_to_doc(doc: Any, plan: dict[str, Any], *, template_doc: Any | None = None) -> dict[str, Any]:
    """Best-effort fill: duplicate slides and set placeholder/body text from plan."""
    slides = plan.get("slides")
    if not isinstance(slides, list) or not slides:
        return {"status": "error", "message": "fill_plan must contain a non-empty slides list."}

    bridge = DrawBridge(doc)
    filled = 0
    for offset, item in enumerate(slides):
        if not isinstance(item, dict):
            continue
        bridge.create_slide(offset, switch=False)
        item_dict = cast("dict[str, Any]", item)
        replacements = item_dict.get("replacements") or item_dict.get("text") or {}
        page = bridge.get_slide_for_tool(doc, offset)
        if isinstance(replacements, dict):
            for _key, text in replacements.items():
                if not text:
                    continue
                shape, _idx = _find_placeholder(page, _key)
                if shape is not None and hasattr(shape, "setString"):
                    shape.setString(text)
                    filled += 1
        elif isinstance(replacements, str) and replacements.strip():
            # For a raw string, try to write to the body or title
            shape, _idx = _find_placeholder(page, "body")
            if shape is None:
                shape, _idx = _find_placeholder(page, "title")
            if shape is not None and hasattr(shape, "setString"):
                shape.setString(replacements)
                filled += 1
    return {"status": "ok", "slides_created": len(slides), "fills_recorded": filled, "note": "Use export after SVG pipeline for full fidelity; template-fill UNO path is incremental."}


def apply_fill_plan_file(doc: Any, plan_path: Path) -> dict[str, Any]:
    data = json.loads(Path(plan_path).read_text(encoding="utf-8"))
    return apply_fill_plan_to_doc(doc, data)
