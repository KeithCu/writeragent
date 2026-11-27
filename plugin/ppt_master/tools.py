# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""ppt-master specialized tools (sidebar PPT-Master mode only)."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

from plugin.draw.base import ToolDrawPptMasterBase
from plugin.ppt_master.client import (
    apply_native_enhance,
    apply_template_fill,
    export_project_to_doc,
    validate_project_structure,
)
from plugin.ppt_master.paths import PPT_MASTER_INSTALL_CMD, apply_data_root_env, data_root_status

if TYPE_CHECKING:
    from plugin.framework.tool import ToolContext


class ExportPresentationProject(ToolDrawPptMasterBase):
    name: str | None = "export_presentation_project"
    description: str = (
        "Export a ppt-master project folder into the active Impress/Draw document "
        "by building or loading exports/*.pptx and importing via LibreOffice's native PPTX filter."
    )
    is_mutation: bool | None = True
    long_running: bool = True
    timeout: float | None = 600.0
    parameters: dict[str, Any] | None = {
        "type": "object",
        "properties": {
            "project_path": {"type": "string", "description": "Path to ppt-master project directory."},
        },
        "required": ["project_path"],
    }

    def is_async(self) -> bool:
        return True

    def execute(self, ctx: ToolContext, **kwargs: Any) -> dict[str, Any]:
        apply_data_root_env()
        st = data_root_status()
        if not st.get("ok"):
            return self._tool_error(
                f"PPT-Master data package not found. Install with: {PPT_MASTER_INSTALL_CMD}",
                code="PPT_MASTER_DATA_MISSING",
            )
        path, err = self._required_path(kwargs, "project_path")
        if err is not None:
            return err
        assert path is not None
        return export_project_to_doc(ctx.doc, path, ctx.ctx, data_root=Path(st["data_root"]), cancel=ctx.send_cancellation)


class ValidatePptMasterProject(ToolDrawPptMasterBase):
    name: str | None = "validate_ppt_master_project"
    description: str = "Check that a ppt-master project folder has expected artifacts (SVG slides, design spec)."
    is_mutation: bool | None = False
    parameters: dict[str, Any] | None = {
        "type": "object",
        "properties": {
            "project_path": {"type": "string", "description": "Path to ppt-master project directory."},
        },
        "required": ["project_path"],
    }

    def execute(self, ctx: ToolContext, **kwargs: Any) -> dict[str, Any]:
        apply_data_root_env()
        path, err = self._required_path(kwargs, "project_path")
        if err is not None:
            return err
        assert path is not None
        return validate_project_structure(path)


class ApplyPptMasterTemplateFill(ToolDrawPptMasterBase):
    name: str | None = "apply_ppt_master_template_fill"
    description: str = "Apply a ppt-master fill_plan.json to the active presentation (template-fill route)."
    is_mutation: bool | None = True
    parameters: dict[str, Any] | None = {
        "type": "object",
        "properties": {
            "fill_plan_path": {"type": "string", "description": "Path to fill_plan.json."},
        },
        "required": ["fill_plan_path"],
    }

    def execute(self, ctx: ToolContext, **kwargs: Any) -> dict[str, Any]:
        apply_data_root_env()
        plan_path, err = self._required_path(kwargs, "fill_plan_path")
        if err is not None:
            return err
        assert plan_path is not None
        return apply_template_fill(ctx.doc, plan_path)


class ApplyPptMasterNativeEnhance(ToolDrawPptMasterBase):
    name: str | None = "apply_ppt_master_native_enhance"
    description: str = "Apply ppt-master native enhancement (notes, transitions) from a project folder."
    is_mutation: bool | None = True
    parameters: dict[str, Any] | None = {
        "type": "object",
        "properties": {
            "project_path": {"type": "string", "description": "Path to ppt-master enhancement project."},
        },
        "required": ["project_path"],
    }

    def execute(self, ctx: ToolContext, **kwargs: Any) -> dict[str, Any]:
        apply_data_root_env()
        path, err = self._required_path(kwargs, "project_path")
        if err is not None:
            return err
        assert path is not None
        return apply_native_enhance(ctx.doc, path)
