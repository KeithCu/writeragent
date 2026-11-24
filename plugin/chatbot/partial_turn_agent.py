# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""ToolCallingAgent subclass that keeps partial observations when a multi-call turn fails."""

from __future__ import annotations

from typing import TYPE_CHECKING

from plugin.contrib.smolagents.agents import ToolCallingAgent, ToolOutput
from plugin.contrib.smolagents.memory import ActionStep, ToolCall
from plugin.contrib.smolagents.utils import AgentError

if TYPE_CHECKING:
    from collections.abc import Generator
    from plugin.contrib.smolagents.agents import ActionOutput
    from plugin.contrib.smolagents.models import ChatMessageStreamDelta


class PartialTurnToolCallingAgent(ToolCallingAgent):
    """ToolCallingAgent that keeps the results of calls that ran in a turn that failed.

    In ``plugin/contrib/smolagents/agents.py``, ``ToolCallingAgent.process_tool_calls``
    writes ``memory_step.tool_calls`` and ``memory_step.observations`` only after its
    last call, and ``ToolCallingAgent._step_stream`` raises the multiple-final-answers
    error in its outer loop while ``process_tool_calls`` is suspended at a ``yield``.
    Without this override, a failed multi-call turn leaves both fields empty so the
    model sees only the error and may repeat an edit that already landed.
    """

    def _step_stream(
        self, memory_step: ActionStep
    ) -> Generator[ChatMessageStreamDelta | ToolCall | ToolOutput | ActionOutput, None, None]:
        requested: dict[str, ToolCall] = {}
        outputs: dict[str, ToolOutput] = {}
        try:
            for item in super()._step_stream(memory_step):
                if isinstance(item, ToolCall):
                    requested[item.id] = item
                elif isinstance(item, ToolOutput):
                    outputs[item.id] = item
                yield item
        except AgentError:
            # The error text alone already reports a single-call failure.
            if len(requested) > 1:
                self._record_partial_turn(memory_step, requested, outputs)
            raise

    def _record_partial_turn(
        self,
        memory_step: ActionStep,
        requested: dict[str, ToolCall],
        outputs: dict[str, ToolOutput],
    ) -> None:
        # Calls run one at a time in request order. With two finish calls every started call
        # has an output and none raised; otherwise the first call without one raised.
        failure_passed = sum(o.is_final_answer for o in outputs.values()) > 1
        lines: list[str] = []
        for i, call in enumerate(requested.values(), 1):
            output = outputs.get(call.id)
            if output is not None:
                # Vendored Tool classes do not all define is_final_answer_tool.
                if output.is_final_answer or getattr(self.tools[call.name], "is_final_answer_tool", False):
                    lines.append(f"Call {i} ({call.name}) was not accepted because this turn failed.")
                else:
                    lines.append(f"Call {i} ({call.name}) ran:\n{output.observation}")
            elif not failure_passed:
                failure_passed = True
                lines.append(f"Call {i} ({call.name}) failed; its error is below.")
            else:
                lines.append(f"Call {i} ({call.name}) did not run because this turn had already failed.")
                self.on_call_not_run(call)
        memory_step.tool_calls = list(requested.values())
        memory_step.observations = "\n".join(lines)

    def on_call_not_run(self, call: ToolCall) -> None:
        """Called for each call skipped after the turn failed. Override to record it."""
