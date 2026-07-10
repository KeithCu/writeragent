# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""PPT-Master smol sub-agent executed inside the user venv worker."""

from __future__ import annotations

import logging
import re
from typing import Any, Iterable, cast

from plugin.contrib.smolagents.agents import ToolCallingAgent
from plugin.contrib.smolagents.memory import ActionStep, FinalAnswerStep, ToolCall
from plugin.contrib.smolagents.tools import Tool
from plugin.ppt_master.venv.ipc import emit_worker_event, rpc_tool
from plugin.ppt_master.venv.model import HostRpcModel
from plugin.ppt_master.venv.path_ops import resolve_project_file, resolve_under_root, run_script
from plugin.ppt_master.venv.skill_context import load_skill_context, resolve_data_root_from_env

log = logging.getLogger(__name__)

_SKILL_CACHE: dict[str, str] = {}
_EXPORTED_FLAG: dict[str, bool] = {}


def _tool_schemas(tools: list[Tool]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for t in tools:
        props: dict[str, Any] = {}
        required: list[str] = []
        for name, spec in t.inputs.items():
            props[name] = {"type": spec.get("type", "string"), "description": spec.get("description", "")}
            if not spec.get("nullable", True):
                required.append(name)
        out.append(
            {
                "type": "function",
                "function": {
                    "name": t.name,
                    "description": t.description,
                    "parameters": {"type": "object", "properties": props, "required": required},
                },
            }
        )
    return out


class _RpcHostTool(Tool):
    """Base for tools that call WriterAgent on the LO host."""

    skip_forward_signature_validation = True

    def __init__(self, host_tool_name: str, *, description: str, inputs: dict[str, dict[str, Any]]) -> None:
        self.name = host_tool_name
        self.description = description
        self.inputs = inputs
        self.output_type = "object"
        super().__init__()

    def forward(self, **kwargs: Any) -> Any:
        return rpc_tool(self.name, **kwargs)


class RunPptMasterScript(Tool):
    name = "run_ppt_master_script"
    description = "Run an upstream ppt-master Python script under scripts/ (e.g. scripts/project_manager.py)."
    inputs = {
        "script_relative": {"type": "string", "description": "Path under scripts/, e.g. project_manager.py or source_to_md/pdf_to_md.py"},
        "args": {"type": "array", "description": "CLI arguments after the script path.", "nullable": True},
    }
    output_type = "object"
    skip_forward_signature_validation = True

    def forward(self, script_relative: str, args: list | None = None) -> Any:
        root = resolve_data_root_from_env()
        argv = [str(a) for a in (args or [])]
        return run_script(root, script_relative, argv)


class ReadPptMasterWorkflowFile(Tool):
    name = "read_ppt_master_workflow_file"
    description = "Read a file under the ppt-master data root (SKILL.md, references/, workflows/)."
    inputs = {
        "relative_path": {"type": "string", "description": "e.g. references/executor-base.md"},
    }
    output_type = "object"
    skip_forward_signature_validation = True

    def forward(self, relative_path: str) -> Any:
        root = resolve_data_root_from_env()
        path = resolve_under_root(root, relative_path)
        if path is None or not path.is_file():
            return {"status": "error", "message": f"Not found: {relative_path}"}
        text = path.read_text(encoding="utf-8", errors="replace")
        if len(text) > 32_000:
            text = text[:32_000] + "\n[truncated]"
        return {"status": "ok", "path": str(path), "content": text}


class ReadProjectFile(Tool):
    name = "read_project_file"
    description = "Read a file inside a ppt-master project directory."
    inputs = {
        "project_path": {"type": "string", "description": "Absolute path to project folder."},
        "relative_path": {"type": "string", "description": "Path relative to project root."},
    }
    output_type = "object"
    skip_forward_signature_validation = True

    def forward(self, project_path: str, relative_path: str) -> Any:
        path = resolve_project_file(project_path, relative_path)
        if path is None or not path.is_file():
            return {"status": "error", "message": "File not found."}
        text = path.read_text(encoding="utf-8", errors="replace")
        if len(text) > 64_000:
            text = text[:64_000] + "\n[truncated]"
        return {"status": "ok", "path": str(path), "content": text}


class WriteProjectFile(Tool):
    name = "write_project_file"
    description = "Write or overwrite a file inside a ppt-master project directory."
    inputs = {
        "project_path": {"type": "string", "description": "Absolute path to project folder."},
        "relative_path": {"type": "string", "description": "Path relative to project root."},
        "content": {"type": "string", "description": "File contents."},
    }
    output_type = "object"
    skip_forward_signature_validation = True

    def forward(self, project_path: str, relative_path: str, content: str) -> Any:
        path = resolve_project_file(project_path, relative_path)
        if path is None:
            return {"status": "error", "message": "Invalid project path or relative path."}
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return {"status": "ok", "path": str(path), "bytes": len(content.encode("utf-8"))}


class ReplyToUserTool(Tool):
    name = "reply_to_user"
    description = "Continue the PPT-Master session with an HTML message to the user."
    inputs = {"message": {"type": "string", "description": "HTML reply."}}
    output_type = "string"
    is_final_answer_tool = True
    skip_forward_signature_validation = True

    def forward(self, message: str) -> str:
        return message


class PptMasterFinishedTool(Tool):
    name = "ppt_master_finished"
    description = "End the PPT-Master session. message must be HTML."
    inputs = {
        "message": {"type": "string", "description": "HTML handoff."},
        "exported": {"type": "boolean", "description": "True if export succeeded this session.", "nullable": True},
    }
    output_type = "object"
    is_final_answer_tool = True
    skip_forward_signature_validation = True

    def forward(self, message: str, exported: bool = False) -> dict[str, Any]:
        return {"status": "finished", "result": message, "exported": bool(exported)}


def _build_tools() -> list[Tool]:
    host_tools = [
        _RpcHostTool(
            "validate_ppt_master_project",
            description="Check ppt-master project folder artifacts.",
            inputs={"project_path": {"type": "string", "description": "Project directory path."}},
        ),
        _RpcHostTool(
            "export_presentation_project",
            description="Import project exports/*.pptx into active Impress/Draw document.",
            inputs={"project_path": {"type": "string", "description": "Project directory path."}},
        ),
        _RpcHostTool(
            "apply_ppt_master_template_fill",
            description="Apply fill_plan.json to active presentation.",
            inputs={"fill_plan_path": {"type": "string", "description": "Path to fill_plan.json."}},
        ),
        _RpcHostTool(
            "apply_ppt_master_native_enhance",
            description="Apply native enhancement from project folder.",
            inputs={"project_path": {"type": "string", "description": "Enhancement project path."}},
        ),
    ]
    return [
        RunPptMasterScript(),
        ReadPptMasterWorkflowFile(),
        ReadProjectFile(),
        WriteProjectFile(),
        *host_tools,
        ReplyToUserTool(),
        PptMasterFinishedTool(),
    ]


def _instructions_for_session(session_id: str, *, topic: str | None, ctx_block: str) -> str:
    from plugin.framework.prompts import get_chat_response_format_instructions

    parts = [
        "PPT-MASTER MODE (venv worker):\n",
        "Follow the loaded SKILL workflow. Use scripts and project files on disk; export via host UNO tools.\n",
        ctx_block,
        get_chat_response_format_instructions(None),
    ]
    if topic and topic.strip():
        parts.append(f"\n[PPT-MASTER TOPIC]\n{topic.strip()}\n")
    _SKILL_CACHE[session_id] = "\n\n".join(parts)
    return _SKILL_CACHE[session_id]


def _parse_finished(observations: str) -> dict[str, Any] | None:
    if "'status': 'finished'" not in observations and '"status": "finished"' not in observations:
        return None
    match = re.search(r"'result': '([^']*)'", observations) or re.search(r'"result": "([^"]*)"', observations)
    handoff = match.group(1) if match else None
    exp_match = re.search(r"'exported': (True|False)", observations) or re.search(
        r'"exported": (true|false)', observations, re.I
    )
    exported = exp_match.group(1).lower() == "true" if exp_match else False
    return {"status": "finished", "result": handoff or "PPT-Master complete.", "exported": exported}


def run_turn(payload: dict[str, Any]) -> dict[str, Any]:
    """Execute one PPT-Master user turn inside the venv worker (called from worker_harness)."""
    query = str(payload.get("query") or "")
    history_text = payload.get("history_text")
    topic = payload.get("topic")
    model = payload.get("model")
    session_id = str(payload.get("session_id") or "ppt_master:default")
    max_steps = int(payload.get("max_steps") or 12)
    max_tokens = int(payload.get("max_tokens") or 16384)

    skill = load_skill_context()
    if not skill.get("ok"):
        return {"status": "error", "message": skill.get("block", "PPT-Master data root not configured.")}

    instructions = _instructions_for_session(session_id, topic=topic, ctx_block=str(skill.get("block", "")))
    tools = _build_tools()
    smol_model = HostRpcModel(model_id=model, max_tokens=max_tokens, status_callback=lambda s: emit_worker_event({"kind": "status", "text": s}))

    agent = ToolCallingAgent(
        tools=tools,
        model=smol_model,
        max_steps=max_steps,
        instructions=instructions,
        final_answer_tool_name="reply_to_user",
    )

    if history_text and len(str(history_text)) > 4000:
        history_text = "..." + str(history_text)[-4000:]
    task = f"### CONVERSATION HISTORY:\n{history_text or 'None'}\n\n### CURRENT QUERY:\n{query}"

    final_ans = None
    run_stream = cast("Iterable", agent.run(task, stream=True))
    for step in run_stream:
        if isinstance(step, ToolCall):
            emit_worker_event({"kind": "tool", "name": step.name, "arguments": str(step.arguments)[:500]})
            if step.name == "export_presentation_project":
                _EXPORTED_FLAG[session_id] = True
        elif isinstance(step, ActionStep):
            parts = [f"Step {step.step_number}:\n"]
            if step.model_output:
                mo = step.model_output
                parts.append(f"{(mo.strip() if isinstance(mo, str) else str(mo).strip())}\n")
            if step.observations:
                obs_str = str(step.observations).strip()
                parts.append(f"Observation: {obs_str}\n")
                finished = _parse_finished(obs_str)
                if finished is not None:
                    if _EXPORTED_FLAG.get(session_id):
                        finished["exported"] = True
                    emit_worker_event({"kind": "thinking", "text": "".join(parts)})
                    return finished
            parts.append("\n")
            emit_worker_event({"kind": "thinking", "text": "".join(parts)})
        elif isinstance(step, FinalAnswerStep):
            final_ans = step.output

    return {"status": "ok", "result": str(final_ans) if final_ans is not None else ""}


def clear_session(session_id: str) -> None:
    _SKILL_CACHE.pop(session_id, None)
    _EXPORTED_FLAG.pop(session_id, None)
