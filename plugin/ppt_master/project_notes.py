# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Map ppt-master project speaker notes to slide indices."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

from plugin.contrib.ppt_master.upstream import collect_notes_upstream


def _read_notes_for_slide(project_path: Path, slide_num: int) -> str | None:
    notes_dir = project_path / "notes"
    per_slide = notes_dir / f"slide_{slide_num:02d}.md"
    if per_slide.is_file():
        return per_slide.read_text(encoding="utf-8", errors="replace").strip()
    return None


def notes_for_slides(project_path: Path, svg_files: list[Path], data_root: Path | None) -> dict[int, str]:
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
