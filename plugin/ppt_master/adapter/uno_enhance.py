# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""UNO native-enhance: notes, transitions from ppt-master project metadata."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from plugin.draw.bridge import DrawBridge, find_notes_shape
from plugin.framework.errors import is_disposed_exception

log = logging.getLogger(__name__)


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
    failures: list[str] = []
    for item in plan.get("slides") or []:
        if not isinstance(item, dict):
            continue
        slide_idx = item.get("slide_index")
        if slide_idx is None:
            slide_idx = item.get("index")
        idx = int(slide_idx) if slide_idx is not None else 0
        if idx < 0 or idx >= pages.getCount():
            continue
        notes = item.get("notes")
        if notes and doc.supportsService("com.sun.star.presentation.PresentationDocument"):
            # find_notes_shape is the NotesShape lookup the notes tools use.
            # Header, footer, and date chrome also implement setString and can
            # precede the notes body. Dispose propagates. applied increments
            # only after setString returns; a miss is status error.
            try:
                page = pages.getByIndex(idx)
                shape = find_notes_shape(page.getNotesPage())
                if shape is None:
                    failures.append(f"slide {idx} notes: no NotesShape")
                else:
                    shape.setString(str(notes))
                    applied += 1
            except Exception as exc:
                if is_disposed_exception(exc):
                    raise
                failures.append(f"slide {idx} notes: {exc}")
                log.warning("apply enhancement notes failed on slide %s: %s", idx, exc)
        trans = item.get("transition")
        if trans and isinstance(trans, dict):
            try:
                page = pages.getByIndex(idx)
                if "type" in trans:
                    page.setPropertyValue("Effect", int(trans["type"]))
                applied += 1
            except Exception as exc:
                # A dead document must not look like a successful apply.
                if is_disposed_exception(exc):
                    raise
                failures.append(f"slide {idx} transition: {exc}")
                log.warning("apply enhancement transition failed on slide %s: %s", idx, exc)
    if failures:
        return {
            "status": "error",
            "applied": applied,
            "failed": len(failures),
            "message": "Failed to apply enhancement: " + "; ".join(failures) + ".",
        }
    return {"status": "ok", "applied": applied}
