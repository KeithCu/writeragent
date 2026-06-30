# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""UNO backend: project SVG folder → SlideBuildPlan → Impress."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from plugin.contrib.ppt_master.svg_convert import collect_svg_files, svg_to_slide_plan
from plugin.contrib.ppt_master.shape_ops import SlideBuildPlan
from plugin.ppt_master.adapter.uno_apply import apply_slide_plans


def _read_notes_for_slide(project_path: Path, slide_num: int) -> str | None:
    notes_dir = project_path / "notes"
    per_slide = notes_dir / f"slide_{slide_num:02d}.md"
    if per_slide.is_file():
        return per_slide.read_text(encoding="utf-8", errors="replace").strip()
    return None


def build_plans_from_project(project_path: Path) -> list[SlideBuildPlan]:
    project_path = Path(project_path).expanduser().resolve()
    svg_files = collect_svg_files(project_path)
    plans: list[SlideBuildPlan] = []
    for i, svg_path in enumerate(svg_files):
        notes = _read_notes_for_slide(project_path, i + 1)
        plans.append(svg_to_slide_plan(svg_path, slide_index=i, notes_text=notes))
    return plans


def export_project_to_doc(doc: Any, project_path: Path) -> dict[str, Any]:
    plans = build_plans_from_project(project_path)
    if not plans:
        return {"status": "error", "message": "No SVG slides found under svg_final/ or svg_output/."}
    return apply_slide_plans(doc, plans)
