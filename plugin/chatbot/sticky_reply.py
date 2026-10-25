# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

"""Shared smol ``reply_to_user`` for sticky sidebar modes (leave flag = old tool name).

Register this tool in the agent tool list with ``final_answer_tool_name="reply_to_user"``
so smolagents does not inject a second FinalAnswerTool. Stay = dict with status ok; leave =
a dict the panel already understands (``switch_mode`` / ``finished``).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from plugin.framework.config_schema import as_bool
from plugin.framework.tool import ToolBase

if TYPE_CHECKING:
    from plugin.framework.tool import ToolContext

STATUS_OK = "ok"
STATUS_ERROR = "error"
STATUS_INCOMPLETE = "incomplete"
STATUS_FINISHED = "finished"
STATUS_SWITCH_MODE = "switch_mode"


@dataclass(frozen=True)
class StickyReplySpec:
    """Per-mode leave flag (old tool name), panel status, and optional extra bools."""

    leave_flag: str
    leave_status: str
    extra_bools: tuple[str | tuple[str, str], ...] = ()
    description: str = (
        "Reply to the user in the sidebar. Set the leave flag true to end this mode "
        "and return to Chat."
    )

    def __post_init__(self) -> None:
        assert self.leave_flag, "leave_flag must be non-empty"
        reserved = {"answer", "message", "result", "status"}
        assert self.leave_flag not in reserved, f"leave_flag {self.leave_flag!r} collision with reserved field"
        extra_names = tuple(e[0] if isinstance(e, (tuple, list)) else e for e in self.extra_bools)
        assert not (set(extra_names) & reserved), f"extra_bools collision with reserved field: {set(extra_names) & reserved}"
        assert self.leave_flag not in set(extra_names), f"extra_bools contains leave_flag {self.leave_flag!r}"


LIBRARIAN_REPLY_SPEC = StickyReplySpec(
    leave_flag="switch_to_document_mode",
    leave_status=STATUS_SWITCH_MODE,
    description=(
        "Reply to the user in the sidebar. Set switch_to_document_mode true when "
        "onboarding is done or they want document work (switches the dropdown to Chat)."
    ),
)

BRAINSTORMING_REPLY_SPEC = StickyReplySpec(
    leave_flag="brainstorming_finished",
    leave_status=STATUS_FINISHED,
    extra_bools=(("spec_saved", "Recorded from a successful save_design_spec this turn."),),
    description=(
        "Reply to the user in the sidebar (HTML). Set brainstorming_finished true "
        "to end brainstorming after the spec is saved and reviewed. spec_saved is "
        "recorded from a successful save_design_spec this turn."
    ),
)

WRITING_PLAN_REPLY_SPEC = StickyReplySpec(
    leave_flag="writing_plan_finished",
    leave_status=STATUS_FINISHED,
    # plan_completed was copied onto the leave payload and never read.
    # on_writing_plan_session_finished takes no extra flag; do not re-add
    # the bool unless that callback grows a consumer (spec_saved is the pattern).
    description=(
        "Reply to the user in the sidebar (HTML). Set writing_plan_finished true "
        "when the writing plan is done."
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
        if status == STATUS_ERROR:
            return output
        if status == STATUS_OK:
            result = output.get("result")
            return {"status": STATUS_OK, "result": "" if result is None else str(result)}
        return {"status": STATUS_INCOMPLETE, "result": str(output.get("result") or output.get("message") or output)}
    if output is None:
        return {"status": STATUS_INCOMPLETE, "result": ""}
    return {"status": STATUS_INCOMPLETE, "result": str(output)}


class StickyReplyToUserTool(ToolBase):
    """Smol final-answer tool named ``reply_to_user`` with one optional leave flag."""

    name: str | None = "reply_to_user"
    tier: str = "specialized_control"
    is_final_answer_tool: bool = True
    is_mutation: bool | None = False
    long_running: bool = False
    requires_document: bool = False
    spec: StickyReplySpec
    description: str
    parameters: dict[str, Any] | None
    spec_saved: bool = False

    def __init__(self, spec: StickyReplySpec) -> None:
        self.spec = spec
        self.description = spec.description
        self.spec_saved = False
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
        self._extra_names: list[str] = []
        for extra in spec.extra_bools:
            if isinstance(extra, (tuple, list)):
                name, desc = extra[0], extra[1]
            else:
                name, desc = extra, f"Optional extra flag for this mode ({extra})."
            self._extra_names.append(name)
            properties[name] = {
                "type": "boolean",
                "description": desc,
            }
        self.parameters = {
            "type": "object",
            "properties": properties,
            "required": ["answer"],
        }

    def is_async(self) -> bool:
        return True

    def record_tool_result(self, tool_name: str, result: Any) -> None:
        """Record tool results executed this turn to observe side effects."""
        if tool_name == "save_design_spec" and isinstance(result, dict) and result.get("status") == "ok":
            self.spec_saved = True

    def execute(self, ctx: ToolContext, **kwargs: Any) -> dict[str, Any]:
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
            for extra in self._extra_names:
                if extra == "spec_saved":
                    payload[extra] = bool(self.spec_saved)
                else:
                    payload[extra] = as_bool(kwargs.get(extra, False))
            return payload
        return {"status": STATUS_OK, "result": answer_text}
