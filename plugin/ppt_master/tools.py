# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""ppt-master specialized tools (sidebar PPT-Master mode only)."""

from __future__ import annotations

from typing import Any

from plugin.draw.base import ToolDrawPptMasterBase
from plugin.ppt_master.client import (
    apply_native_enhance,
    apply_template_fill,
    export_project_to_impress,
    validate_project_structure,
)
from plugin.ppt_master.paths import PPT_MASTER_INSTALL_CMD, apply_data_root_env, data_root_status


class ExportPresentationProject(ToolDrawPptMasterBase):
    name = "export_presentation_project"
    description = (
        "Export a ppt-master project folder into the active Impress/Draw document "
        "by building or loading exports/*.pptx and importing via LibreOffice's native PPTX filter."
    )
    is_mutation = True
    long_running = True
    parameters = {
        "type": "object",
        "properties": {
            "project_path": {"type": "string", "description": "Path to ppt-master project directory."},
        },
        "required": ["project_path"],
    }

    def execute(self, ctx, **kwargs: Any) -> dict[str, Any]:
        apply_data_root_env(ctx.ctx)
        st = data_root_status(ctx.ctx)
        if not st.get("ok"):
            return self._tool_error(
                f"PPT-Master data package not found. Install with: {PPT_MASTER_INSTALL_CMD}",
                code="PPT_MASTER_DATA_MISSING",
            )
        path = kwargs.get("project_path")
        if not path:
            return self._tool_error("project_path is required.", code="MISSING_PATH")
        return export_project_to_impress(ctx.ctx, ctx.doc, path)


class ValidatePptMasterProject(ToolDrawPptMasterBase):
    name = "validate_ppt_master_project"
    description = "Check that a ppt-master project folder has expected artifacts (SVG slides, design spec)."
    is_mutation = False
    parameters = {
        "type": "object",
        "properties": {
            "project_path": {"type": "string", "description": "Path to ppt-master project directory."},
        },
        "required": ["project_path"],
    }

    def execute(self, ctx, **kwargs: Any) -> dict[str, Any]:
        apply_data_root_env(ctx.ctx)
        path = kwargs.get("project_path")
        if not path:
            return self._tool_error("project_path is required.", code="MISSING_PATH")
        return validate_project_structure(path)


class ApplyPptMasterTemplateFill(ToolDrawPptMasterBase):
    name = "apply_ppt_master_template_fill"
    description = "Apply a ppt-master fill_plan.json to the active presentation (template-fill route)."
    is_mutation = True
    parameters = {
        "type": "object",
        "properties": {
            "fill_plan_path": {"type": "string", "description": "Path to fill_plan.json."},
        },
        "required": ["fill_plan_path"],
    }

    def execute(self, ctx, **kwargs: Any) -> dict[str, Any]:
        apply_data_root_env(ctx.ctx)
        plan_path = kwargs.get("fill_plan_path")
        if not plan_path:
            return self._tool_error("fill_plan_path is required.", code="MISSING_PATH")
        return apply_template_fill(ctx.ctx, ctx.doc, plan_path)


class ApplyPptMasterNativeEnhance(ToolDrawPptMasterBase):
    name = "apply_ppt_master_native_enhance"
    description = "Apply ppt-master native enhancement (notes, transitions) from a project folder."
    is_mutation = True
    parameters = {
        "type": "object",
        "properties": {
            "project_path": {"type": "string", "description": "Path to ppt-master enhancement project."},
        },
        "required": ["project_path"],
    }

    def execute(self, ctx, **kwargs: Any) -> dict[str, Any]:
        apply_data_root_env(ctx.ctx)
        path = kwargs.get("project_path")
        if not path:
            return self._tool_error("project_path is required.", code="MISSING_PATH")
        return apply_native_enhance(ctx.ctx, ctx.doc, path)
