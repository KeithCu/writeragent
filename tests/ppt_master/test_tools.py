# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

from unittest.mock import MagicMock, patch

from plugin.ppt_master.tools import (
    ApplyPptMasterNativeEnhance,
    ApplyPptMasterTemplateFill,
    ExportPresentationProject,
)


def test_export_execute_applies_env_once_and_calls_export_project_to_doc() -> None:
    ctx = MagicMock()
    ctx.doc = MagicMock()
    ctx.ctx = MagicMock()
    with patch("plugin.ppt_master.tools.apply_data_root_env") as apply_env, patch(
        "plugin.ppt_master.tools.data_root_status", return_value={"ok": True, "data_root": "/data"}
    ), patch("plugin.ppt_master.tools.export_project_to_doc", return_value={"status": "ok"}) as export_doc:
        out = ExportPresentationProject().execute(ctx, project_path="/proj")
    apply_env.assert_called_once_with(ctx.ctx)
    export_doc.assert_called_once_with(ctx.doc, "/proj", ctx.ctx, cancel=ctx.send_cancellation)
    assert out == {"status": "ok"}


def test_template_fill_execute_calls_two_arg_client() -> None:
    ctx = MagicMock()
    ctx.doc = MagicMock()
    ctx.ctx = MagicMock()
    with patch("plugin.ppt_master.tools.apply_data_root_env"), patch(
        "plugin.ppt_master.tools.apply_template_fill", return_value={"status": "ok"}
    ) as fill:
        out = ApplyPptMasterTemplateFill().execute(ctx, fill_plan_path="/plan.json")
    fill.assert_called_once_with(ctx.doc, "/plan.json")
    assert out == {"status": "ok"}


def test_native_enhance_execute_calls_two_arg_client() -> None:
    ctx = MagicMock()
    ctx.doc = MagicMock()
    ctx.ctx = MagicMock()
    with patch("plugin.ppt_master.tools.apply_data_root_env"), patch(
        "plugin.ppt_master.tools.apply_native_enhance", return_value={"status": "ok"}
    ) as enhance:
        out = ApplyPptMasterNativeEnhance().execute(ctx, project_path="/proj")
    enhance.assert_called_once_with(ctx.doc, "/proj")
    assert out == {"status": "ok"}
