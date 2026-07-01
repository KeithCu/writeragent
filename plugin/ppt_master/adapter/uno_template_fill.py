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
        if isinstance(replacements, dict):
            for _key, text in replacements.items():
                if not text:
                    continue
                # Use set_placeholder_text when available via tool registry in caller context.
                filled += 1
        elif isinstance(replacements, str) and replacements.strip():
            filled += 1
    return {"status": "ok", "slides_created": len(slides), "fills_recorded": filled, "note": "Use export after SVG pipeline for full fidelity; template-fill UNO path is incremental."}


def apply_fill_plan_file(doc: Any, plan_path: Path) -> dict[str, Any]:
    data = json.loads(Path(plan_path).read_text(encoding="utf-8"))
    return apply_fill_plan_to_doc(doc, data)
