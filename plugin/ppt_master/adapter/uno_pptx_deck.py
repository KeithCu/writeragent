# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""UNO backend: ppt-master project → Impress via PPTX build + LO PPTX import."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from plugin.contrib.ppt_master.upstream import collect_svg_files
from plugin.ppt_master.adapter.uno_pptx_import import import_pptx_to_doc
from plugin.ppt_master.paths import data_root_status
from plugin.ppt_master.pptx_build import ensure_project_pptx


def export_project_to_doc(doc: Any, project_path: Path, ctx: Any | None = None) -> dict[str, Any]:
    """Export a ppt-master project into *doc* via PPTX → LO ODP import."""
    project_path = Path(project_path).expanduser().resolve()
    svg_files = collect_svg_files(project_path)
    if not svg_files:
        return {"status": "error", "message": "No SVG slides found under svg_final/ or svg_output/."}
    if ctx is None:
        return {"status": "error", "message": "UNO context required for PPTX import."}

    status = data_root_status(ctx)
    if not status.get("ok"):
        return {
            "status": "error",
            "message": "PPT-Master data package not configured (Settings → Python → PPT-Master data path).",
            "code": "PPT_MASTER_DATA_MISSING",
        }
    data_root = Path(status["data_root"])

    pptx_path, build_err = ensure_project_pptx(ctx, project_path, data_root)
    if pptx_path is None:
        return {
            "status": "error",
            "message": build_err or "PPTX not available.",
            "code": "PPTX_BUILD_FAILED",
        }

    mirror_odp = project_path / "exports" / f"{pptx_path.stem}.odp"
    return import_pptx_to_doc(
        ctx,
        doc,
        pptx_path,
        clear_existing=True,
        save_mirror_odp=mirror_odp,
    )
