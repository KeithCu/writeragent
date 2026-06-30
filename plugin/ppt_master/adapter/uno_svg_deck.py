# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""UNO backend: project SVG folder → Impress via LO draw_svg_import."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from plugin.contrib.ppt_master.upstream import collect_notes_upstream, collect_svg_files
from plugin.ppt_master.adapter.uno_svg_import import import_svg_files_to_doc
from plugin.ppt_master.paths import data_root_status


def _read_notes_for_slide(project_path: Path, slide_num: int) -> str | None:
    notes_dir = project_path / "notes"
    per_slide = notes_dir / f"slide_{slide_num:02d}.md"
    if per_slide.is_file():
        return per_slide.read_text(encoding="utf-8", errors="replace").strip()
    return None


def _notes_for_slides(project_path: Path, svg_files: list[Path], data_root: Path | None) -> dict[int, str]:
    """Map slide index → notes text (upstream filename match, then slide_NN.md)."""
    notes_by_index: dict[int, str] = {}
    upstream_notes: dict[str, str] | None = None
    if data_root is not None:
        upstream_notes = collect_notes_upstream(project_path, data_root)
    for i, svg_path in enumerate(svg_files):
        text: str | None = None
        if upstream_notes:
            text = upstream_notes.get(svg_path.stem) or upstream_notes.get(svg_path.name)
            if text is None:
                for key, val in upstream_notes.items():
                    if key.replace(".md", "") == svg_path.stem or key.endswith(svg_path.stem):
                        text = val
                        break
        if not text:
            text = _read_notes_for_slide(project_path, i + 1)
        if text:
            notes_by_index[i] = text
    return notes_by_index


def export_project_to_doc(doc: Any, project_path: Path, ctx: Any | None = None) -> dict[str, Any]:
    project_path = Path(project_path).expanduser().resolve()
    svg_files = collect_svg_files(project_path)
    if not svg_files:
        return {"status": "error", "message": "No SVG slides found under svg_final/ or svg_output/."}
    if ctx is None:
        return {"status": "error", "message": "UNO context required for SVG import."}

    status = data_root_status(ctx)
    data_root = Path(status["data_root"]) if status.get("ok") else None
    notes_by_index = _notes_for_slides(project_path, svg_files, data_root)
    return import_svg_files_to_doc(ctx, doc, svg_files, project_dir=project_path, notes_by_index=notes_by_index)
