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

from typing import TYPE_CHECKING, Any

from plugin.framework.config_schema import as_bool
from plugin.framework.tool import ToolBase

if TYPE_CHECKING:
    from plugin.chatbot.chat_sidebar_mode import SidebarMode
    from plugin.framework.tool import ToolContext

STATUS_OK = "ok"
STATUS_ERROR = "error"
STATUS_INCOMPLETE = "incomplete"
STATUS_FINISHED = "finished"
STATUS_SWITCH_MODE = "switch_mode"


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
    mode: SidebarMode
    description: str
    parameters: dict[str, Any] | None
    spec_saved: bool

    def __init__(self, mode: SidebarMode) -> None:
        self.mode = mode
        self.description = mode.reply_description
        self.spec_saved = False
        properties: dict[str, Any] = {
            "answer": {
                "type": "string",
                "description": "Message shown in the chat sidebar (HTML where the mode requires it).",
            },
        }
        if mode.leave_flag:
            properties[mode.leave_flag] = {
                "type": "boolean",
                "description": (
                    f"Set true to end this mode ({mode.leave_flag}) and return to Chat. "
                    "Omit or false to keep talking."
                ),
            }
        if mode.tracks_spec_saved:
            properties["spec_saved"] = {
                "type": "boolean",
                "description": "Recorded from a successful save_design_spec this turn.",
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
        answer_text = "" if answer is None else str(answer)
        if self.mode.leave_flag and as_bool(kwargs.get(self.mode.leave_flag, False)):
            payload: dict[str, Any] = {
                "status": self.mode.leave_status,
                "result": answer_text,
                "message": answer_text,
            }
            if self.mode.tracks_spec_saved:
                payload["spec_saved"] = bool(self.spec_saved)
            return payload
        return {"status": STATUS_OK, "result": answer_text}
