# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import MagicMock, patch

from plugin.ppt_master.tools import ExportPresentationProject


def test_export_execute_applies_env_once_and_calls_export_project_to_doc(tmp_path: Path, monkeypatch) -> None:
    data_root = tmp_path / "ppt-master"
    data_root.mkdir()
    (data_root / "SKILL.md").write_text("# skill", encoding="utf-8")
    (data_root / "templates").mkdir()
    (data_root / "scripts").mkdir()

    monkeypatch.delenv("PPT_MASTER_DATA_ROOT", raising=False)
    import plugin.framework.config as config_mod

    monkeypatch.setattr(config_mod, "get_config", lambda key: str(data_root) if key == "scripting.ppt_master_data_path" else "")

    ctx = MagicMock()
    ctx.doc = MagicMock()
    ctx.ctx = MagicMock()
    with patch("plugin.ppt_master.tools.export_project_to_doc", return_value={"status": "ok"}) as export_doc:
        out = ExportPresentationProject().execute(ctx, project_path="/proj")
    assert os.environ.get("PPT_MASTER_DATA_ROOT") == str(data_root)
    export_doc.assert_called_once_with(ctx.doc, "/proj", ctx.ctx, cancel=ctx.send_cancellation)
    assert out == {"status": "ok"}
