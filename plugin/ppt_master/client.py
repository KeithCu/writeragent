# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Host entry points for ppt-master → Impress export."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from plugin.contrib.ppt_master.upstream import collect_svg_files
from plugin.framework.errors import ToolExecutionError, format_error_payload
from plugin.framework.queue_executor import main
from plugin.ppt_master.adapter.uno_enhance import apply_enhancement_project
from plugin.ppt_master.adapter.uno_pptx_import import import_pptx_to_doc
from plugin.ppt_master.adapter.uno_template_fill import apply_fill_plan_file
from plugin.ppt_master.paths import apply_data_root_env, data_root_status
from plugin.ppt_master.pptx_build import ensure_project_pptx, find_project_pptx


def export_project_to_doc(doc: Any, project_path: Path, ctx: Any | None = None) -> dict[str, Any]:
    """Export a ppt-master project into *doc* via PPTX → LO ODP import.

    Applies the ppt-master data root from *ctx* when present (idempotent; safe if the caller already did).
    """
    project_path = Path(project_path).expanduser().resolve()
    svg_files = collect_svg_files(project_path)
    if not svg_files:
        return format_error_payload(ToolExecutionError("No SVG slides found under svg_final/ or svg_output/."))
    if ctx is None:
        return format_error_payload(ToolExecutionError("UNO context required for PPTX import."))
    apply_data_root_env(ctx)

    status = data_root_status(ctx)
    if not status.get("ok"):
        return format_error_payload(
            ToolExecutionError(
                "PPT-Master data package not configured (Settings → Python → PPT-Master data path).",
                code="PPT_MASTER_DATA_MISSING",
            )
        )
    data_root = Path(status["data_root"])

    pptx_path, build_err = ensure_project_pptx(ctx, project_path, data_root)
    if pptx_path is None:
        return format_error_payload(
            ToolExecutionError(
                build_err or "PPTX not available.",
                code="PPTX_BUILD_FAILED",
            )
        )

    mirror_odp = project_path / "exports" / f"{pptx_path.stem}.odp"

    return main.call(
        lambda: import_pptx_to_doc(
            ctx,
            doc,
            pptx_path,
            clear_existing=True,
            save_mirror_odp=mirror_odp,
        )
    )


def export_project_to_impress(ctx: Any, doc: Any, project_path: str | Path) -> dict[str, Any]:
    """Apply a ppt-master project to the open Impress/Draw document via PPTX → ODP import.

    Caller must pass UNO *ctx*; ``export_project_to_doc`` applies the data root from it (``tools.py`` also
    calls ``apply_data_root_env`` once per execute before dispatch).
    """
    path = Path(project_path).expanduser().resolve()
    if not path.is_dir():
        return format_error_payload(ToolExecutionError(f"Project path not found: {path}"))
    return export_project_to_doc(doc, path, ctx=ctx)


def apply_template_fill(ctx: Any, doc: Any, plan_path: str | Path) -> dict[str, Any]:
    """Apply a fill plan JSON file to *doc*. *ctx* matches the ppt-master tool entry signature (unused here)."""
    del ctx
    return apply_fill_plan_file(doc, Path(plan_path))


def apply_native_enhance(ctx: Any, doc: Any, project_path: str | Path) -> dict[str, Any]:
    """Apply enhancement metadata from a project directory. *ctx* matches the ppt-master tool entry signature (unused here)."""
    del ctx
    return apply_enhancement_project(doc, Path(project_path))


def validate_project_structure(project_path: str | Path) -> dict[str, Any]:
    path = Path(project_path).expanduser().resolve()
    if not path.is_dir():
        return format_error_payload(ToolExecutionError("Project directory does not exist."))
    has_svg = (path / "svg_final").is_dir() or (path / "svg_output").is_dir()
    has_spec = (path / "design_spec.md").is_file() or (path / "spec_lock.md").is_file()
    pptx = find_project_pptx(path)
    return {
        "status": "ok",
        "path": str(path),
        "has_svg": has_svg,
        "has_exports_pptx": pptx is not None,
        "exports_pptx": str(pptx) if pptx is not None else None,
        "has_design_spec": has_spec,
        "has_sources": (path / "sources").is_dir(),
    }
