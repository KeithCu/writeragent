# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""UNO native-enhance: notes, transitions from ppt-master project metadata."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from plugin.draw.bridge import DrawBridge


def apply_enhancement_project(doc: Any, project_path: Path) -> dict[str, Any]:
    """Apply notes/transitions from project enhancement JSON if present."""
    project_path = Path(project_path).expanduser().resolve()
    plan_path = project_path / "enhancement_plan.json"
    if not plan_path.is_file():
        return {"status": "ok", "message": "No enhancement_plan.json; nothing to apply.", "applied": 0}

    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    bridge = DrawBridge(doc)
    pages = bridge.get_pages()
    applied = 0
    for item in plan.get("slides") or []:
        if not isinstance(item, dict):
            continue
        idx = int(item.get("slide_index", item.get("index", 0)))
        if idx < 0 or idx >= pages.getCount():
            continue
        notes = item.get("notes")
        if notes and doc.supportsService("com.sun.star.presentation.PresentationDocument"):
            try:
                page = pages.getByIndex(idx)
                notes_page = page.getNotesPage()
                for i in range(notes_page.getCount()):
                    shape = notes_page.getByIndex(i)
                    if hasattr(shape, "setString"):
                        shape.setString(str(notes))
                        applied += 1
                        break
            except Exception:
                pass
        trans = item.get("transition")
        if trans and isinstance(trans, dict):
            try:
                page = pages.getByIndex(idx)
                if "type" in trans:
                    page.setPropertyValue("TransitionType", int(trans["type"]))
                applied += 1
            except Exception:
                pass
    return {"status": "ok", "applied": applied}
