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
"""WriterAgent smolagents integration: model wrapper, executor, and factory."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Callable, Iterable, Literal, cast

from plugin.contrib.smolagents.agents import ToolCallingAgent
from plugin.contrib.smolagents.memory import ActionStep, FinalAnswerStep, ToolCall
from plugin.contrib.smolagents.models import ChatMessage, Model, TokenUsage
from plugin.contrib.smolagents.tools import Tool as SmolTool
from plugin.framework.config import get_api_config, get_config_int
from plugin.framework.errors import ToolExecutionError
from plugin.modules.http.client import LlmClient

if TYPE_CHECKING:
    from collections.abc import Sequence

    from plugin.framework.tool_base import ToolBase
    from plugin.framework.tool_context import ToolContext

log = logging.getLogger("writeragent.smol_agent")

# Match ``writeragent.specialized`` messages when ``safe=True`` (delegation path).
_spec_log = logging.getLogger("writeragent.specialized")

SmolInputsStyle = Literal["librarian", "specialized"]


def to_smol_inputs(parameters: dict[str, Any] | None, *, style: SmolInputsStyle = "librarian") -> dict[str, dict[str, Any]]:
    """Convert ToolBase ``parameters`` (JSON Schema) to smolagents ``inputs`` dict.

    * **librarian** — minimal keys, ``nullable`` from ``required`` (legacy librarian onboarding).
    * **specialized** — merge each property schema so ``enum`` and extra keys are preserved;
      default missing ``type`` to ``\"any\"`` (legacy specialized delegation).
    """
    schema = parameters or {}
    props = schema.get("properties") or {}
    if style == "librarian":
        required = set(schema.get("required") or [])
        out: dict[str, dict[str, Any]] = {}
        for p_name, p_schema in props.items():
            out[p_name] = {"type": p_schema.get("type", "string"), "description": p_schema.get("description", ""), "nullable": p_name not in required}
        return out

    out_sp: dict[str, dict[str, Any]] = {}
    for param_name, spec in props.items():
        merged = dict(spec)
        merged["type"] = spec.get("type", "any")
        merged["description"] = spec.get("description", "")
        out_sp[param_name] = merged
    return out_sp


class SmolToolAdapter(SmolTool):
    """Wraps a ``ToolBase`` for smolagents with configurable execution semantics."""

    skip_forward_signature_validation = True

    def __init__(self, tool: ToolBase, tctx: ToolContext, *, safe: bool = False, main_thread_sync: bool = False, inputs_style: SmolInputsStyle = "librarian", output_type: str | None = None) -> None:
        self._inner_tool = tool
        self._inner_tctx = tctx
        self._safe = safe
        self._main_thread_sync = main_thread_sync
        self.name = cast("str", tool.name or "")
        self.description = tool.description
        params = getattr(tool, "parameters", None) or {}
        self.inputs = to_smol_inputs(params, style=inputs_style)
        if output_type is not None:
            self.output_type = output_type
        elif inputs_style == "librarian":
            self.output_type = "any"
        else:
            self.output_type = "object"
        super().__init__()

    def __call__(self, *args: Any, sanitize_inputs_outputs: bool = False, **kwargs: Any) -> Any:
        return super().__call__(*args, sanitize_inputs_outputs=sanitize_inputs_outputs, **kwargs)

    def forward(self, **kwargs: Any) -> Any:
        tool = self._inner_tool
        ctx = self._inner_tctx
        if not self._safe:
            return tool.execute(ctx, **kwargs)
        if getattr(tool, "is_async", lambda: False)():
            _spec_log.debug("Specialized agent executing async tool '%s' on worker", self.name)
            return tool.execute_safe(ctx, **kwargs)
        if self._main_thread_sync:
            from plugin.framework.queue_executor import execute_on_main_thread

            _spec_log.debug("Specialized agent executing sync tool '%s' on main thread", self.name)
            return execute_on_main_thread(tool.execute_safe, ctx, **kwargs)
        return tool.execute_safe(ctx, **kwargs)


class WriterAgentSmolModel(Model):
    """
    A wrapper that implements `smolagents.models.Model` by delegating
    requests to WriterAgent's `LlmClient` (`core.api`).
    """

    def __init__(self, llm_client, max_tokens=1024, status_callback=None, **kwargs):
        super().__init__(**kwargs)
        self.api = llm_client
        self.max_tokens = max_tokens
        self.model_id = self.api.config.get("model", "localwriter/model")
        self._status_callback = status_callback

    def generate(self, messages, stop_sequences=None, response_format=None, tools_to_call_from=None, **kwargs):
        completion_kwargs = self._prepare_completion_kwargs(messages=cast("list[ChatMessage | dict[str, Any]]", messages), stop_sequences=stop_sequences, tools_to_call_from=tools_to_call_from, **kwargs)

        msg_dicts = completion_kwargs.get("messages", [])

        if self._status_callback:
            self._status_callback("Thinking...")

        # Preserve the known-good smolagents request shape: schemas are both in the
        # smol prompt and on the wire. Some local backends select a different parser
        # path when OpenAI-style tools are present.
        tools = completion_kwargs.get("tools", None)
        result = self.api.request_with_tools(msg_dicts, max_tokens=self.max_tokens, tools=tools, model=self.model_id, response_format=response_format, prepend_dev_build_system_prefix=False)

        if self._status_callback:
            self._status_callback("Model responded, processing...")

        usage = result.get("usage") or {}
        token_usage = TokenUsage(input_tokens=usage.get("prompt_tokens", 0), output_tokens=usage.get("completion_tokens", 0)) if usage else None
        return ChatMessage.from_dict({"role": "assistant", "content": result.get("content") or "", "tool_calls": result.get("tool_calls") or None}, raw=result, token_usage=token_usage)


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

    def execute_safe(self, agent, task: str, tool_call_handler: Callable[[ToolCall], Any] | None = None, stop_message: str = "Stopped by user.", error_prefix: str = "Task failed") -> Any:
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
        from plugin.framework.errors import format_error_payload
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


def build_toolcalling_agent(ctx: ToolContext, tools: Sequence[SmolTool], *, instructions: str, final_answer_tool_name: str, examples_block: str, status_callback: object | None = None) -> ToolCallingAgent:
    """Shared construction for smolagents runs (same config as main chat: model, max_tokens, max_steps)."""
    uno_ctx = ctx.ctx
    config = get_api_config(uno_ctx)
    max_tokens = get_config_int(uno_ctx, "chat_max_tokens")
    max_steps = get_config_int(uno_ctx, "chat_max_tool_rounds")

    smol_model = WriterAgentSmolModel(LlmClient(config, uno_ctx), max_tokens=max_tokens, status_callback=status_callback)
    return ToolCallingAgent(tools=list(tools), model=smol_model, max_steps=max_steps, instructions=instructions, final_answer_tool_name=final_answer_tool_name, system_prompt_examples=examples_block)
