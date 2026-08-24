# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

"""Shared smol ``reply_to_user`` for sticky sidebar modes (leave flag = old tool name).

Register this tool in the agent tool list with ``final_answer_tool_name="reply_to_user"``
so smolagents does not inject a second FinalAnswerTool. Stay = string answer; leave =
a dict the panel already understands (``switch_mode`` / ``finished``).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from plugin.framework.config_schema import as_bool
from plugin.framework.tool import ToolBase


@dataclass(frozen=True)
class StickyReplySpec:
    """Per-mode leave flag (old tool name), panel status, and optional extra bools."""

    leave_flag: str
    leave_status: str
    extra_bools: tuple[str, ...] = ()
    description: str = (
        "Reply to the user in the sidebar. Set the leave flag true to end this mode "
        "and return to Chat."
    )


LIBRARIAN_REPLY_SPEC = StickyReplySpec(
    leave_flag="switch_to_document_mode",
    leave_status="switch_mode",
    description=(
        "Reply to the user in the sidebar. Set switch_to_document_mode true when "
        "onboarding is done or they want document work (switches the dropdown to Chat)."
    ),
)

BRAINSTORMING_REPLY_SPEC = StickyReplySpec(
    leave_flag="brainstorming_finished",
    leave_status="finished",
    extra_bools=("spec_saved",),
    description=(
        "Reply to the user in the sidebar (HTML). Set brainstorming_finished true "
        "to end brainstorming after the spec is saved and reviewed. spec_saved true "
        "if save_design_spec ran this session."
    ),
)

WRITING_PLAN_REPLY_SPEC = StickyReplySpec(
    leave_flag="writing_plan_finished",
    leave_status="finished",
    extra_bools=("plan_completed",),
    description=(
        "Reply to the user in the sidebar (HTML). Set writing_plan_finished true "
        "when the writing plan is done. plan_completed true if all sections were written."
    ),
)


def interpret_sticky_final_answer(output: Any, *, leave_status: str) -> dict[str, Any]:
    """Map smol FinalAnswerStep.output to the panel payload (no observation scraping)."""
    if isinstance(output, dict):
        status = output.get("status")
        if status == leave_status:
            result = output.get("result")
            if result is None:
                result = output.get("message") or ""
            payload = dict(output)
            payload["result"] = str(result)
            return payload
        if status == "error":
            return output
        if status == "ok":
            result = output.get("result")
            return {"status": "ok", "result": "" if result is None else str(result)}
        return {"status": "ok", "result": str(output)}
    if output is None:
        return {"status": "ok", "result": ""}
    return {"status": "ok", "result": str(output)}


class StickyReplyToUserTool(ToolBase):
    """Smol final-answer tool named ``reply_to_user`` with one optional leave flag."""

    name = "reply_to_user"
    tier = "specialized_control"
    is_final_answer_tool = True
    is_mutation = False
    long_running = False
    requires_document = False

    def __init__(self, spec: StickyReplySpec) -> None:
        self.spec = spec
        self.description = spec.description
        properties: dict[str, Any] = {
            "answer": {
                "type": "string",
                "description": "Message shown in the chat sidebar (HTML where the mode requires it).",
            },
            spec.leave_flag: {
                "type": "boolean",
                "description": (
                    f"Set true to end this mode ({spec.leave_flag}) and return to Chat. "
                    "Omit or false to keep talking."
                ),
            },
        }
        for extra in spec.extra_bools:
            properties[extra] = {
                "type": "boolean",
                "description": f"Optional extra flag for this mode ({extra}).",
            }
        self.parameters = {
            "type": "object",
            "properties": properties,
            "required": ["answer"],
        }

    def is_async(self) -> bool:
        return False

    def execute(self, ctx, **kwargs: Any) -> Any:
        del ctx
        answer = kwargs.get("answer")
        if answer is None:
            answer = kwargs.get("message") or ""
        answer_text = str(answer)
        if as_bool(kwargs.get(self.spec.leave_flag, False)):
            payload: dict[str, Any] = {
                "status": self.spec.leave_status,
                "result": answer_text,
                "message": answer_text,
            }
            for extra in self.spec.extra_bools:
                payload[extra] = as_bool(kwargs.get(extra, False))
            return payload
        return answer_text
