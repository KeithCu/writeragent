# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

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
    from plugin.contrib.ppt_master.skill_paths import bundled_skill_md_path
    from plugin.ppt_master.venv.skill_context import load_skill_context

    monkeypatch.setenv("PPT_MASTER_DATA_ROOT", str(tmp_path))
    (tmp_path / "scripts").mkdir()
    (tmp_path / "workflows").mkdir()
    (tmp_path / "workflows" / "routing.md").write_text("route here\n", encoding="utf-8")

    ctx = load_skill_context()
    assert ctx["ok"] is True
    assert "WriterAgent fork" in ctx["block"]
    assert bundled_skill_md_path().read_text(encoding="utf-8")[:50] in ctx["block"] or "PPT Master" in ctx["block"]
    assert "WRITERAGENT LO BRIDGE" in ctx["block"]


def test_resolve_writeragent_skill_md_prefers_bundled_fork():
    from plugin.contrib.ppt_master.skill_paths import bundled_skill_md_path, resolve_writeragent_skill_md

    assert resolve_writeragent_skill_md().resolve() == bundled_skill_md_path().resolve()


def test_resolve_writeragent_skill_md_fallback(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from plugin.contrib.ppt_master import skill_paths

    fallback = tmp_path / "SKILL.md"
    fallback.write_text("# fallback\n", encoding="utf-8")
    monkeypatch.setattr(skill_paths, "_BUNDLED_SKILL", tmp_path / "missing.md")
    monkeypatch.setenv("PPT_MASTER_DATA_ROOT", str(tmp_path))

    assert skill_paths.resolve_writeragent_skill_md().resolve() == fallback.resolve()


def test_parse_finished_keeps_apostrophe():
    from plugin.ppt_master.venv.runner import _parse_finished

    observations = str({"status": "finished", "result": "it's \"quoted\"", "exported": True})
    parsed = _parse_finished(observations)
    assert parsed is not None
    assert parsed["result"] == "it's \"quoted\""
    assert parsed["exported"] is True


def test_rpc_llm_raises_user_stopped(monkeypatch: pytest.MonkeyPatch):
    from plugin.ppt_master.venv.ipc import UserStopped, rpc_llm

    monkeypatch.setattr("plugin.ppt_master.venv.ipc._write_frame", lambda payload: None)
    monkeypatch.setattr(
        "plugin.ppt_master.venv.ipc._read_host_response",
        lambda context: {"status": "error", "code": "USER_STOPPED", "message": "Stopped by user."},
    )
    with pytest.raises(UserStopped, match="Stopped by user."):
        rpc_llm(messages=[{"role": "user", "content": "x"}])


def test_rpc_llm_without_stop_code_is_runtime_error(monkeypatch: pytest.MonkeyPatch):
    """A stop-shaped message without code is a generic error and does not end the turn."""
    from plugin.ppt_master.venv.ipc import rpc_llm

    monkeypatch.setattr("plugin.ppt_master.venv.ipc._write_frame", lambda payload: None)
    monkeypatch.setattr(
        "plugin.ppt_master.venv.ipc._read_host_response",
        lambda context: {"status": "error", "message": "LLM request stopped by user."},
    )
    with pytest.raises(RuntimeError, match="LLM request stopped by user."):
        rpc_llm(messages=[{"role": "user", "content": "x"}])


def test_rpc_tool_raises_user_stopped(monkeypatch: pytest.MonkeyPatch):
    from plugin.ppt_master.venv.ipc import UserStopped, rpc_tool

    monkeypatch.setattr("plugin.ppt_master.venv.ipc._write_frame", lambda payload: None)
    monkeypatch.setattr(
        "plugin.ppt_master.venv.ipc._read_host_response",
        lambda context: {"status": "error", "code": "USER_STOPPED", "message": "Stopped by user."},
    )
    with pytest.raises(UserStopped):
        rpc_tool("export_presentation_project", project_path="/p")


def test_run_turn_returns_user_stopped_when_host_stops(monkeypatch: pytest.MonkeyPatch):
    from plugin.contrib.smolagents.memory import ActionStep
    from plugin.contrib.smolagents.monitoring import Timing
    from plugin.contrib.smolagents.utils import AgentError
    from plugin.ppt_master.venv.ipc import UserStopped
    from plugin.ppt_master.venv.runner import clear_session, run_turn

    stopped = UserStopped("Stopped by user.")
    wrapped = AgentError("tool failed", MagicMock())
    wrapped.__cause__ = stopped
    step = ActionStep(step_number=1, timing=Timing(start_time=0), error=wrapped)
    agent = MagicMock()
    agent.run.return_value = [step]
    monkeypatch.setenv("PPT_MASTER_DATA_ROOT", "/tmp/ppt-master-stop")
    with (
        patch("plugin.ppt_master.venv.runner.load_skill_context", return_value={"ok": True, "block": "skill"}),
        patch("plugin.ppt_master.venv.runner._build_tools", return_value=[]),
        patch("plugin.ppt_master.venv.runner.ToolCallingAgent", return_value=agent),
        patch("plugin.ppt_master.venv.runner.HostRpcModel"),
        patch("plugin.ppt_master.venv.runner.emit_worker_event"),
    ):
        result = run_turn({"query": "hi", "session_id": "stop-test"})
    clear_session("stop-test")
    assert result["status"] == "error"
    assert result["code"] == "USER_STOPPED"


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
        assert mock_run.call_args.kwargs["cancellation_scope"] is ctx.send_cancellation
        assert out["status"] == "ok"
        assert "done" in out["result"]


def test_run_turn_passes_ppt_master_examples(monkeypatch: pytest.MonkeyPatch):
    from plugin.chatbot.smol_examples import get_examples_block
    from plugin.ppt_master.venv.runner import clear_session, run_turn

    agent = MagicMock()
    agent.run.return_value = []
    monkeypatch.setenv("PPT_MASTER_DATA_ROOT", "/tmp/ppt-master-examples")
    with (
        patch("plugin.ppt_master.venv.runner.load_skill_context", return_value={"ok": True, "block": "skill"}),
        patch("plugin.ppt_master.venv.runner._build_tools", return_value=[]),
        patch("plugin.ppt_master.venv.runner.ToolCallingAgent", return_value=agent) as mock_agent,
        patch("plugin.ppt_master.venv.runner.HostRpcModel"),
    ):
        result = run_turn({"query": "hi", "session_id": "examples-test"})
    clear_session("examples-test")
    assert result["status"] == "ok"
    assert mock_agent.call_args.kwargs["system_prompt_examples"] == get_examples_block("ppt-master")


def test_run_ppt_master_venv_turn_keeps_cancellation_on_the_host():
    from plugin.ppt_master.venv.host import run_ppt_master_venv_turn

    scope = object()
    manager = MagicMock()
    manager.execute_ppt_master_turn.return_value = {"status": "ok"}
    with (
        patch("plugin.ppt_master.venv.host._worker_manager_for_ctx", return_value=(manager, None)),
        patch("plugin.ppt_master.venv.host.apply_data_root_env"),
        patch("plugin.ppt_master.venv.host.configured_python_exec_timeout", return_value=30),
        patch("plugin.ppt_master.venv.host.resolve_python_exec_timeout", return_value=30),
        patch("plugin.ppt_master.venv.host.get_config_int", return_value=8),
    ):
        run_ppt_master_venv_turn(
            MagicMock(),
            query="q",
            history_text=None,
            topic=None,
            model=None,
            session_id="s",
            cancellation_scope=scope,
        )
    payload = manager.execute_ppt_master_turn.call_args.args[0]
    assert manager.execute_ppt_master_turn.call_args.kwargs["cancellation_scope"] is scope
    assert "cancellation_scope" not in payload
    assert "_cancellation_scope" not in payload
