# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import MagicMock, patch

from plugin.ppt_master import client


def test_export_project_to_doc_missing_directory_does_not_collect_svgs(tmp_path: Path) -> None:
    missing = tmp_path / "nope"
    with patch.object(client, "collect_svg_files") as collect:
        out = client.export_project_to_doc(MagicMock(), missing, ctx=MagicMock())
    collect.assert_not_called()
    assert "Project path not found" in out.get("message", "")


def test_export_project_to_doc_no_svgs_does_not_set_data_root_env(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.delenv("PPT_MASTER_DATA_ROOT", raising=False)
    project = tmp_path / "proj"
    project.mkdir()
    (project / "svg_final").mkdir()
    out = client.export_project_to_doc(MagicMock(), project, ctx=MagicMock())
    assert "No SVG slides" in out.get("message", "")
    assert os.environ.get("PPT_MASTER_DATA_ROOT") is None


def test_apply_template_fill_forwards_to_adapter(tmp_path: Path) -> None:
    doc = MagicMock()
    plan = tmp_path / "fill_plan.json"
    plan.write_text("{}", encoding="utf-8")
    with patch.object(client, "apply_fill_plan_file", return_value={"status": "ok"}) as fill:
        out = client.apply_template_fill(doc, plan)
    fill.assert_called_once_with(doc, plan)
    assert out == {"status": "ok"}


def test_apply_native_enhance_forwards_to_adapter(tmp_path: Path) -> None:
    doc = MagicMock()
    project = tmp_path / "proj"
    project.mkdir()
    with patch.object(client, "apply_enhancement_project", return_value={"status": "ok"}) as enhance:
        out = client.apply_native_enhance(doc, project)
    enhance.assert_called_once_with(doc, project)
    assert out == {"status": "ok"}


def test_export_project_to_doc_forwards_cancel_and_returns_error_on_stop(tmp_path: Path) -> None:
    project = tmp_path / "proj"
    project.mkdir()
    (project / "svg_final").mkdir()
    (project / "svg_final" / "slide_001.svg").write_text("<svg></svg>", encoding="utf-8")
    cancel = MagicMock()
    ctx = MagicMock()
    with patch("plugin.ppt_master.client.data_root_status", return_value={"ok": True, "data_root": str(tmp_path)}), \
         patch("plugin.ppt_master.client.ensure_project_pptx", return_value=(None, "PPTX build stopped.")) as mock_ensure, \
         patch("plugin.ppt_master.client.main.call") as mock_main_call:
        res = client.export_project_to_doc(MagicMock(), project, ctx=ctx, cancel=cancel)
    mock_ensure.assert_called_once_with(ctx, project, tmp_path, cancel=cancel)
    mock_main_call.assert_not_called()
    assert res.get("status") == "error"
    assert res.get("code") == "PPTX_BUILD_FAILED"
    assert "PPTX build stopped." in res.get("message", "")

