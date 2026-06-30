# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Trusted venv stub: ppt-master project folder → serializable SlideBuildPlan list."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from plugin.contrib.ppt_master.shape_ops import slide_plan_to_dict
from plugin.ppt_master.adapter.uno_svg_deck import build_plans_from_project


def build_slide_plans(project_path: str | Path, backend: str = "uno") -> dict[str, Any]:
    """Build slide plans in the worker; host applies via UNO on the main thread."""
    path = Path(project_path).expanduser().resolve()
    if not path.is_dir():
        return {"status": "error", "message": f"Project path not found: {path}"}
    plans = build_plans_from_project(path)
    if not plans:
        return {"status": "error", "message": "No SVG slides found under svg_final/ or svg_output/."}
    return {
        "status": "ok",
        "backend": backend,
        "project_path": str(path),
        "plans": [slide_plan_to_dict(p) for p in plans],
    }
