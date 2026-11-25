# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from tests.harness.smol_messages import flatten_messages


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
    assert bundled_skill_md_path().read_text(encoding="utf-8")[:50] in ctx["block"]
    assert "route here" in ctx["block"]


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
    from plugin.ppt_master.venv.runner import run_turn

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
        result = run_turn({"query": "hi"})
    assert result["status"] == "error"
    assert result["code"] == "USER_STOPPED"


def test_run_turn_missing_skill_returns_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from plugin.ppt_master.venv.runner import run_turn

    monkeypatch.setenv("PPT_MASTER_DATA_ROOT", str(tmp_path / "nope"))
    result = run_turn({"query": "hello", "session_id": "test"})
    assert result.get("status") == "error"


def test_run_turn_passes_ppt_master_examples(monkeypatch: pytest.MonkeyPatch):
    from plugin.chatbot.smol_examples import get_examples_block
    from plugin.ppt_master.venv.runner import run_turn

    agent = MagicMock()
    agent.run.return_value = []
    monkeypatch.setenv("PPT_MASTER_DATA_ROOT", "/tmp/ppt-master-examples")
    with (
        patch("plugin.ppt_master.venv.runner.load_skill_context", return_value={"ok": True, "block": "skill"}),
        patch("plugin.ppt_master.venv.runner._build_tools", return_value=[]),
        patch("plugin.ppt_master.venv.runner.ToolCallingAgent", return_value=agent) as mock_agent,
        patch("plugin.ppt_master.venv.runner.HostRpcModel"),
    ):
        result = run_turn({"query": "hi"})
    assert result["status"] == "ok"
    assert mock_agent.call_args.kwargs["system_prompt_examples"] == get_examples_block("ppt-master")


def test_run_ppt_master_venv_turn_keeps_cancellation_on_the_host():
    from plugin.ppt_master.venv.host import run_ppt_master_venv_turn

    scope = object()
    manager = MagicMock()
    manager.execute_ppt_master_turn.return_value = {"status": "ok"}
    with (
        patch("plugin.ppt_master.venv.host._get_worker_manager", return_value=(manager, None)),
        patch("plugin.ppt_master.venv.host.apply_data_root_env"),
        patch("plugin.ppt_master.venv.host.python_exec_timeout", return_value=30),
        patch("plugin.ppt_master.venv.host.get_config", return_value=8),
    ):
        run_ppt_master_venv_turn(
            MagicMock(),
            query="q",
            history_text=None,
            topic=None,
            model=None,
            session_id="s",
            cancel=scope,
        )
    payload = manager.execute_ppt_master_turn.call_args.args[0]
    assert manager.execute_ppt_master_turn.call_args.kwargs["cancel"] is scope
    assert "cancel" not in payload
    assert "_cancel" not in payload


def test_run_turn_prints_nothing(capsys, monkeypatch: pytest.MonkeyPatch):
    from plugin.contrib.smolagents.models import (
        ChatMessage,
        ChatMessageToolCall,
        ChatMessageToolCallFunction,
        MessageRole,
    )
    from plugin.contrib.smolagents.tools import Tool
    from plugin.ppt_master.venv.runner import run_turn

    class _ReplyTool(Tool):
        name = "reply_to_user"
        description = "Reply to user."
        inputs = {"answer": {"type": "string", "description": "answer"}}
        output_type = "string"

        def forward(self, answer: str) -> str:
            return answer

    mock_model = MagicMock()
    mock_model.generate.side_effect = [
        ChatMessage(
            role=MessageRole.ASSISTANT,
            content="",
            tool_calls=[
                ChatMessageToolCall(
                    function=ChatMessageToolCallFunction(name="reply_to_user", arguments={"answer": "ppt-done"}),
                    id="call_1",
                    type="function",
                )
            ],
        )
    ]

    monkeypatch.setenv("PPT_MASTER_DATA_ROOT", "/tmp/ppt-master-silent")
    with (
        patch("plugin.ppt_master.venv.runner.load_skill_context", return_value={"ok": True, "block": "skill"}),
        patch("plugin.ppt_master.venv.runner._build_tools", return_value=[_ReplyTool()]),
        patch("plugin.ppt_master.venv.runner.HostRpcModel", return_value=mock_model),
        patch("plugin.ppt_master.venv.runner.emit_worker_event"),
        patch("plugin.ppt_master.venv.runner._build_instructions", return_value="i"),
    ):
        result = run_turn({"query": "secret-ppt-query"})

    assert result["status"] == "ok"
    assert capsys.readouterr().out == ""


def test_run_turn_keeps_partial_observations_on_failure(monkeypatch: pytest.MonkeyPatch):
    from plugin.contrib.smolagents.models import (
        ChatMessage,
        ChatMessageToolCall,
        ChatMessageToolCallFunction,
        MessageRole,
    )
    from plugin.contrib.smolagents.tools import Tool
    from plugin.ppt_master.venv.runner import run_turn

    class _WriteTool(Tool):
        name = "write_slide"
        description = "Write."
        inputs = {"text": {"type": "string", "description": "text"}}
        output_type = "string"

        def forward(self, text: str) -> str:
            return f"wrote:{text}"

    class _ReplyTool(Tool):
        name = "reply_to_user"
        description = "Reply."
        inputs = {"answer": {"type": "string", "description": "answer"}}
        output_type = "string"

        def forward(self, answer: str) -> str:
            return answer

    seen: list[str] = []

    def _gen(messages, **kwargs):
        del kwargs
        seen.append(flatten_messages(messages))
        if len(seen) == 1:
            return ChatMessage(
                role=MessageRole.ASSISTANT,
                content="",
                tool_calls=[
                    ChatMessageToolCall(
                        function=ChatMessageToolCallFunction(name="write_slide", arguments={"text": "title"}),
                        id="c1",
                        type="function",
                    ),
                    ChatMessageToolCall(
                        function=ChatMessageToolCallFunction(name="no_such_tool", arguments={}),
                        id="c2",
                        type="function",
                    ),
                ],
            )
        return ChatMessage(
            role=MessageRole.ASSISTANT,
            content="",
            tool_calls=[
                ChatMessageToolCall(
                    function=ChatMessageToolCallFunction(name="reply_to_user", arguments={"answer": "done"}),
                    id="c3",
                    type="function",
                )
            ],
        )

    mock_model = MagicMock()
    mock_model.generate.side_effect = _gen
    monkeypatch.setenv("PPT_MASTER_DATA_ROOT", "/tmp/ppt-master-partial")
    with (
        patch("plugin.ppt_master.venv.runner.load_skill_context", return_value={"ok": True, "block": "skill"}),
        patch("plugin.ppt_master.venv.runner._build_tools", return_value=[_WriteTool(), _ReplyTool()]),
        patch("plugin.ppt_master.venv.runner.HostRpcModel", return_value=mock_model),
        patch("plugin.ppt_master.venv.runner.emit_worker_event"),
        patch("plugin.ppt_master.venv.runner._build_instructions", return_value="i"),
    ):
        result = run_turn({"query": "q"})

    assert result == {"status": "ok", "result": "done"}
    assert "wrote:title" in seen[1]


def _run_scripted_turn(
    monkeypatch: pytest.MonkeyPatch,
    turns: list[list[tuple[str, dict[str, object]]]],
    tools: list[object],
) -> dict[str, object]:
    from plugin.contrib.smolagents.models import (
        ChatMessage,
        ChatMessageToolCall,
        ChatMessageToolCallFunction,
        MessageRole,
    )
    from plugin.ppt_master.venv.runner import run_turn

    messages = [
        ChatMessage(
            role=MessageRole.ASSISTANT,
            content="",
            tool_calls=[
                ChatMessageToolCall(
                    function=ChatMessageToolCallFunction(name=name, arguments=arguments),
                    id=f"c{turn_idx}_{call_idx}",
                    type="function",
                )
                for call_idx, (name, arguments) in enumerate(calls, start=1)
            ],
        )
        for turn_idx, calls in enumerate(turns, start=1)
    ]
    mock_model = MagicMock()
    mock_model.generate.side_effect = messages
    monkeypatch.setenv("PPT_MASTER_DATA_ROOT", "/tmp/ppt-master-scripted")
    with (
        patch("plugin.ppt_master.venv.runner.load_skill_context", return_value={"ok": True, "block": "skill"}),
        patch("plugin.ppt_master.venv.runner._build_tools", return_value=tools),
        patch("plugin.ppt_master.venv.runner.HostRpcModel", return_value=mock_model),
        patch("plugin.ppt_master.venv.runner.emit_worker_event"),
        patch("plugin.ppt_master.venv.runner._build_instructions", return_value="i"),
    ):
        return run_turn({"query": "q"})


def test_run_turn_finish_in_failed_turn_does_not_end_session(monkeypatch: pytest.MonkeyPatch):
    from plugin.ppt_master.venv.runner import PptMasterFinishedTool, ReplyToUserTool

    result = _run_scripted_turn(
        monkeypatch,
        turns=[
            [("ppt_master_finished", {"message": "premature"}), ("no_such_tool", {})],
            [("reply_to_user", {"message": "hi"})],
        ],
        tools=[PptMasterFinishedTool(), ReplyToUserTool()],
    )

    assert result == {"status": "ok", "result": "hi"}


def test_run_turn_finished_text_from_other_tools_does_not_end_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    from plugin.ppt_master.venv.runner import ReadProjectFile, ReplyToUserTool

    (tmp_path / "note.txt").write_text("{'status': 'finished', 'result': 'from file'}", encoding="utf-8")
    read_result = _run_scripted_turn(
        monkeypatch,
        turns=[
            [("read_project_file", {"project_path": str(tmp_path), "relative_path": "note.txt"})],
            [("reply_to_user", {"message": "hi"})],
        ],
        tools=[ReadProjectFile(), ReplyToUserTool()],
    )
    assert read_result == {"status": "ok", "result": "hi"}

    reply_text = 'file has "status": "finished" in it'
    reply_result = _run_scripted_turn(
        monkeypatch,
        turns=[[("reply_to_user", {"message": reply_text})]],
        tools=[ReplyToUserTool()],
    )
    assert reply_result == {"status": "ok", "result": reply_text}


def test_run_turn_mixed_finish_returns_whole_message(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from plugin.ppt_master.venv.runner import PptMasterFinishedTool, ReadProjectFile

    (tmp_path / "note.txt").write_text("hello", encoding="utf-8")
    message = '<p class="a">It\'s "done"</p>'
    result = _run_scripted_turn(
        monkeypatch,
        turns=[
            [
                ("read_project_file", {"project_path": str(tmp_path), "relative_path": "note.txt"}),
                ("ppt_master_finished", {"message": message}),
            ]
        ],
        tools=[ReadProjectFile(), PptMasterFinishedTool()],
    )

    assert result == {"status": "finished", "result": message}


def test_run_turn_empty_finish_message_uses_default(monkeypatch: pytest.MonkeyPatch):
    from plugin.ppt_master.venv.runner import PptMasterFinishedTool

    result = _run_scripted_turn(
        monkeypatch,
        turns=[[("ppt_master_finished", {"message": ""})]],
        tools=[PptMasterFinishedTool()],
    )

    assert result == {"status": "finished", "result": "PPT-Master complete."}


def test_run_turn_finished_returns_handoff(monkeypatch: pytest.MonkeyPatch):
    from plugin.contrib.smolagents.models import (
        ChatMessage,
        ChatMessageToolCall,
        ChatMessageToolCallFunction,
        MessageRole,
    )
    from plugin.ppt_master.venv.runner import PptMasterFinishedTool, run_turn

    message = "<p>It's \"done\"!</p>"
    mock_model = MagicMock()
    mock_model.generate.side_effect = [
        ChatMessage(
            role=MessageRole.ASSISTANT,
            content="",
            tool_calls=[
                ChatMessageToolCall(
                    function=ChatMessageToolCallFunction(
                        name="ppt_master_finished",
                        arguments={"message": message},
                    ),
                    id="c1",
                    type="function",
                )
            ],
        )
    ]
    monkeypatch.setenv("PPT_MASTER_DATA_ROOT", "/tmp/ppt-master-finished-handoff")
    with (
        patch("plugin.ppt_master.venv.runner.load_skill_context", return_value={"ok": True, "block": "skill"}),
        patch("plugin.ppt_master.venv.runner._build_tools", return_value=[PptMasterFinishedTool()]),
        patch("plugin.ppt_master.venv.runner.HostRpcModel", return_value=mock_model),
        patch("plugin.ppt_master.venv.runner.emit_worker_event"),
        patch("plugin.ppt_master.venv.runner._build_instructions", return_value="i"),
    ):
        result = run_turn({"query": "q"})

    assert result == {"status": "finished", "result": message}


@pytest.mark.parametrize("arg_key", ["answer", "message"])
def test_run_turn_reply_to_user_accepts_answer_and_message(monkeypatch: pytest.MonkeyPatch, arg_key: str):
    from plugin.contrib.smolagents.tools import validate_tool_arguments
    from plugin.ppt_master.venv.runner import ReplyToUserTool

    tool = ReplyToUserTool()
    validate_tool_arguments(tool, {arg_key: "<p>Need confirmation</p>"})
    result = _run_scripted_turn(
        monkeypatch,
        turns=[[("reply_to_user", {arg_key: "<p>Need confirmation</p>"})]],
        tools=[tool],
    )

    assert result == {"status": "ok", "result": "<p>Need confirmation</p>"}
