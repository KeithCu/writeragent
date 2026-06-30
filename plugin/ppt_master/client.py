# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Host entry points for ppt-master → Impress export."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from plugin.ppt_master.adapter.uno_enhance import apply_enhancement_project
from plugin.ppt_master.adapter.uno_svg_deck import export_project_to_doc
from plugin.ppt_master.adapter.uno_template_fill import apply_fill_plan_file
from plugin.ppt_master.paths import apply_data_root_env


def export_project_to_impress(ctx: Any, doc: Any, project_path: str | Path) -> dict[str, Any]:
    """Apply a ppt-master project SVG deck to the open Impress/Draw document."""
    apply_data_root_env(ctx)
    path = Path(project_path).expanduser().resolve()
    if not path.is_dir():
        return {"status": "error", "message": f"Project path not found: {path}"}
    return export_project_to_doc(doc, path)


def apply_template_fill(ctx: Any, doc: Any, plan_path: str | Path) -> dict[str, Any]:
    apply_data_root_env(ctx)
    return apply_fill_plan_file(doc, Path(plan_path))


def apply_native_enhance(ctx: Any, doc: Any, project_path: str | Path) -> dict[str, Any]:
    apply_data_root_env(ctx)
    return apply_enhancement_project(doc, Path(project_path))


def validate_project_structure(project_path: str | Path) -> dict[str, Any]:
    path = Path(project_path).expanduser().resolve()
    if not path.is_dir():
        return {"status": "error", "message": "Project directory does not exist."}
    has_svg = (path / "svg_final").is_dir() or (path / "svg_output").is_dir()
    has_spec = (path / "design_spec.md").is_file() or (path / "spec_lock.md").is_file()
    return {
        "status": "ok",
        "path": str(path),
        "has_svg": has_svg,
        "has_design_spec": has_spec,
        "has_sources": (path / "sources").is_dir(),
    }
