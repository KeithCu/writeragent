# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2024 John Balis
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.
"""Shared utility to execute and stream smolagents steps to the UI."""

import logging
from typing import Any, Callable, Iterable, cast

from plugin.contrib.smolagents.memory import ActionStep, FinalAnswerStep, ToolCall
from plugin.framework.errors import ToolExecutionError

log = logging.getLogger("writeragent.smol_executor")


class SmolAgentExecutor:
    """Executes a smolagent and streams its progress to the document chat UI."""

    def __init__(self, ctx):
        """Initialize the executor with the tool context.

        Args:
            ctx: ToolContext with doc, services, and UI callbacks.
        """
        self.ctx = ctx
        self.status_callback = getattr(ctx, "status_callback", None)
        self.append_thinking_callback = getattr(ctx, "append_thinking_callback", None)
        self.stop_checker = getattr(ctx, "stop_checker", None)

    def run(self, agent, task: str, tool_call_handler: Callable[[ToolCall], Any] | None = None) -> Any:
        """Run the agent and stream its steps.

        Args:
            agent: The smolagents Agent instance to run.
            task: The task string for the agent.
            tool_call_handler: Optional callback to handle ToolCall steps.
                              If provided, it should handle UI reporting for tools.
                              If it returns a value that is not None, the loop exits
                              and returns that value (useful for error payloads).

        Returns:
            The final answer from the agent.

        Raises:
            ToolExecutionError: If the task is stopped by the user or an error occurs.
        """
        final_ans = None
        run_stream = cast("Iterable", agent.run(task, stream=True))

        for step in run_stream:
            if self.stop_checker and self.stop_checker():
                raise ToolExecutionError("Task stopped by user.", code="USER_STOPPED")

            if isinstance(step, ToolCall):
                if tool_call_handler:
                    res = tool_call_handler(step)
                    if res is not None:
                        return res
                else:
                    # Default ToolCall handling
                    if self.append_thinking_callback:
                        self.append_thinking_callback(f"Running tool: {step.name} with {step.arguments}\n")
                    if self.status_callback:
                        self.status_callback(f"Tool: {step.name}...")

            elif isinstance(step, ActionStep):
                if self.append_thinking_callback:
                    msg = f"Step {step.step_number}:\n"
                    if step.model_output:
                        mo = step.model_output
                        msg += f"{(mo.strip() if isinstance(mo, str) else str(mo).strip())}\n"
                    else:
                        mom = getattr(step, "model_output_message", None)
                        if mom is not None and getattr(mom, "content", None):
                            mc = mom.content
                            msg += f"{(mc.strip() if isinstance(mc, str) else str(mc).strip())}\n"

                    if step.observations:
                        msg += f"Observation: {str(step.observations).strip()}\n"

                    self.append_thinking_callback(msg + "\n")

            elif isinstance(step, FinalAnswerStep):
                final_ans = step.output

        return final_ans

    def execute_safe(
        self,
        agent,
        task: str,
        tool_call_handler: Callable[[ToolCall], Any] | None = None,
        stop_message: str = "Stopped by user.",
        error_prefix: str = "Task failed",
    ) -> Any:
        """Execute the agent safely, catching errors and formatting them for the UI.

        Args:
            agent: The smolagents Agent instance to run.
            task: The task string for the agent.
            tool_call_handler: Optional callback to handle ToolCall steps.
            stop_message: Message to show if the user stops the task.
            error_prefix: Prefix for general error messages.

        Returns:
            The final answer or a formatted error payload.
        """
        from plugin.framework.errors import format_error_payload, ToolExecutionError
        from plugin.framework.i18n import _

        try:
            return self.run(agent, task, tool_call_handler=tool_call_handler)
        except ToolExecutionError as e:
            if e.code == "USER_STOPPED":
                err = ToolExecutionError(_(stop_message), code="USER_STOPPED")
                return format_error_payload(err)
            log.error(f"{error_prefix}: %s", e)
            err = ToolExecutionError(f"{error_prefix}: {str(e)}", code=e.code, details=e.details)
            return format_error_payload(err)
        except Exception as e:
            from plugin.framework.errors import NetworkError

            if isinstance(e, NetworkError):
                log.error(f"{error_prefix} NetworkError: %s", e)
            else:
                log.error(f"{error_prefix}: %s", e)
            err = ToolExecutionError(f"{error_prefix}: {str(e)}")
            return format_error_payload(err)
