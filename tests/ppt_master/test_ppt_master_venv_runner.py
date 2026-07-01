# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


def test_resolve_under_root_rejects_traversal(tmp_path: Path):
    from plugin.ppt_master.venv.path_ops import resolve_under_root

    root = tmp_path / "data"
    root.mkdir()
    (root / "scripts").mkdir()
    (root / "scripts" / "ok.py").write_text("print('ok')", encoding="utf-8")

    ok = resolve_under_root(root, "scripts/ok.py")
    assert ok is not None
    assert ok.name == "ok.py"

    assert resolve_under_root(root, "../etc/passwd") is None
    assert resolve_under_root(root, "scripts/../../outside") is None


def test_run_script_requires_scripts_prefix(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from plugin.ppt_master.venv.path_ops import run_script

    scripts = tmp_path / "scripts"
    scripts.mkdir()
    script = scripts / "demo.py"
    script.write_text("print('hello')", encoding="utf-8")
    monkeypatch.setenv("PPT_MASTER_DATA_ROOT", str(tmp_path))

    out = run_script(tmp_path, "demo.py", [])
    assert out["status"] == "ok"
    assert "hello" in out.get("stdout", "")

    bad = run_script(tmp_path, "../outside.py", [])
    assert bad["status"] == "error"


def test_load_skill_context_missing_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from plugin.ppt_master.venv.skill_context import load_skill_context

    monkeypatch.setenv("PPT_MASTER_DATA_ROOT", str(tmp_path / "missing"))
    ctx = load_skill_context()
    assert ctx["ok"] is False
    assert "data root" in ctx["block"].lower() or "missing" in ctx["block"].lower()


def test_load_skill_context_includes_skill_md(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from plugin.ppt_master.venv.skill_context import load_skill_context

    monkeypatch.setenv("PPT_MASTER_DATA_ROOT", str(tmp_path))
    (tmp_path / "SKILL.md").write_text("# PPT-Master skill\n", encoding="utf-8")
    (tmp_path / "workflows").mkdir()
    (tmp_path / "workflows" / "routing.md").write_text("route here\n", encoding="utf-8")

    ctx = load_skill_context()
    assert ctx["ok"] is True
    assert "PPT-Master skill" in ctx["block"]
    assert "WRITERAGENT LO BRIDGE" in ctx["block"]


def test_run_turn_missing_skill_returns_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from plugin.ppt_master.venv.runner import run_turn

    monkeypatch.setenv("PPT_MASTER_DATA_ROOT", str(tmp_path / "nope"))
    result = run_turn({"query": "hello", "session_id": "test"})
    assert result.get("status") == "error"


def test_ppt_master_session_delegates_to_venv():
    from plugin.chatbot.ppt_master import PptMasterSessionTool
    from plugin.framework.tool import ToolContext

    tool = PptMasterSessionTool()
    ctx = MagicMock(spec=ToolContext)
    ctx.ctx = MagicMock()
    ctx.doc = "anthropic/claude-sonnet-4"
    ctx.status_callback = None
    ctx.append_thinking_callback = None
    ctx.stop_checker = None

    fake_doc = MagicMock()
    fake_doc.getURL.return_value = "file:///deck.odp"

    with (
        patch("plugin.framework.uno_context.get_active_document", return_value=fake_doc),
        patch("plugin.framework.uno_context.get_ctx", return_value=MagicMock()),
        patch(
            "plugin.ppt_master.venv.host.run_ppt_master_venv_turn",
            return_value={"status": "ok", "result": "<p>done</p>"},
        ) as mock_run,
    ):
        out = tool.execute(ctx, query="Build a deck")
        mock_run.assert_called_once()
        assert out["status"] == "ok"
        assert "done" in out["result"]
