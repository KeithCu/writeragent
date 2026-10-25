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
from plugin.contrib.smolagents.models import ChatMessage, Model, TokenUsage, remove_content_after_stop_sequences
from plugin.contrib.smolagents.tools import Tool as SmolTool
from plugin.chatbot import smol_examples
from plugin.framework.errors import ToolExecutionError, make_tool_error
from plugin.framework.client import llm_client
from plugin.framework.client.llm_client import LlmClient
_ORIGINAL_LLM_CLIENT = LlmClient
from plugin.framework.tool import field_from_tool_arguments, prepare_tool_call


def get_api_config() -> dict[str, Any]:
    from plugin.framework.config import get_api_config as _gac
    return _gac()


def get_config_int(key: str) -> int:
    from plugin.framework.config import get_config_int as _gci
    return _gci(key)

if TYPE_CHECKING:
    from collections.abc import Sequence

    from plugin.chatbot.sticky_reply import StickyReplySpec
    from plugin.framework.tool import ToolBase
    from plugin.framework.tool import ToolContext

log = logging.getLogger("writeragent.smol_agent")

# Match ``writeragent.specialized`` messages when ``safe=True`` (delegation path).
_spec_log = logging.getLogger("writeragent.specialized")

SmolInputsStyle = Literal["librarian", "specialized"]
_SMOL_WIRE_KEYS = frozenset({"messages", "tools", "response_format"})


def to_smol_inputs(parameters: dict[str, Any] | None, *, style: SmolInputsStyle = "librarian") -> dict[str, dict[str, Any]]:
    """Convert ToolBase ``parameters`` (JSON Schema) to smolagents ``inputs`` dict.

    Always copy ``enum`` and ``items`` when present. Default type is ``string`` for
    librarian style, ``any`` for specialized style.
    """
    schema = parameters or {}
    props = schema.get("properties") or {}
    required = set(schema.get("required") or [])
    default_type = "string" if style == "librarian" else "any"
    out: dict[str, dict[str, Any]] = {}
    for param_name, spec in props.items():
        merged = dict(spec)
        merged["type"] = spec.get("type", default_type)
        merged["description"] = spec.get("description", "")
        merged["nullable"] = param_name not in required
        out[param_name] = merged
    return out


class SmolToolAdapter(SmolTool):
    """Wraps a ``ToolBase`` for smolagents with configurable execution semantics."""

    skip_forward_signature_validation: bool = True
    _inner_tool: ToolBase
    _inner_tctx: ToolContext
    _safe: bool
    name: str
    description: str
    is_final_answer_tool: bool
    inputs: dict[str, dict[str, str | type | bool]]
    output_type: str

    def __init__(self, tool: ToolBase, tctx: ToolContext, *, safe: bool = False, inputs_style: SmolInputsStyle = "librarian", output_type: str | None = None) -> None:
        self._inner_tool = tool
        self._inner_tctx = tctx
        self._safe = safe
        self.name = cast("str", tool.name or "")
        doc_type = getattr(tctx, "doc_type", None)
        if hasattr(tool, "get_description") and callable(tool.get_description):
            self.description = tool.get_description(doc_type)
        else:
            self.description = tool.description
        self.is_final_answer_tool = getattr(tool, "is_final_answer_tool", False)
        params = getattr(tool, "parameters", None) or {}
        if hasattr(tool, "get_parameters") and callable(tool.get_parameters):
            params = tool.get_parameters(tctx.doc_type) or params
        self.inputs = to_smol_inputs(params, style=inputs_style)
        if output_type is not None:
            self.output_type = output_type
        elif inputs_style == "librarian":
            self.output_type = "any"
        else:
            self.output_type = "object"
        super().__init__()

    def forward(self, *args: Any, **kwargs: Any) -> Any:
        # Handle positional args robustly
        if len(args) == 1 and isinstance(args[0], dict):
            kwargs = {**args[0], **kwargs}
        elif args:
            input_keys = list(self.inputs.keys())
            for i, val in enumerate(args):
                if i < len(input_keys):
                    kwargs[input_keys[i]] = val

        tool = self._inner_tool
        ctx = self._inner_tctx

        # Same schema filter, coercion, validate() and VALIDATION_ERROR payload as
        # ToolRegistry.execute. forward used to skip them, so a bad enum value
        # (e.g. position='start') ran the tool instead of returning the valid-value error.
        prepared = prepare_tool_call(tool, ctx, tool.name or self.name, kwargs)
        if prepared.error is not None:
            return prepared.error
        kwargs = prepared.kwargs

        # Refuse before dispatch with the same flag, detects_mutation(), and
        # READ_ONLY_TARGET payload as ToolRegistry.execute. forward used to
        # call ToolBase.execute and skip that check, so a mutating tool ran
        # on a read_only_target context. The field is a bool, so only an
        # explicit True counts.
        if getattr(ctx, "read_only_target", False) is True and tool.detects_mutation():
            return make_tool_error("This document is open for read-only document_research access; writes are not allowed.", code="READ_ONLY_TARGET", **prepared.details)

        is_async = getattr(tool, "is_async", lambda: False)()
        if is_async:
            _spec_log.debug("SmolToolAdapter executing async tool '%s' on worker", self.name)
            result = (tool.execute_safe if self._safe else tool.execute)(ctx, **kwargs)
        else:
            from plugin.framework.queue_executor import execute_on_main_thread

            # Note: Even tools that do not strictly require UNO (like Librarian's MemoryTool)
            # are marshalled to the main thread here if they are sync.
            # This sync-default policy is intentional and the extra hop is acceptable.
            _spec_log.debug("SmolToolAdapter executing sync tool '%s' on main thread", self.name)
            result = execute_on_main_thread(tool.execute_safe if self._safe else tool.execute, ctx, **kwargs)
        # Like ToolRegistry.execute: say which arguments were dropped instead of ignoring them silently.
        if prepared.ignored and isinstance(result, dict):
            result.setdefault("ignored_parameters", prepared.ignored)
        return result


class WriterAgentSmolModel(Model):
    """
    A wrapper that implements `smolagents.models.Model` by delegating
    requests to WriterAgent's `LlmClient` (`core.api`).
    """

    api: LlmClient
    max_tokens: int
    model_id: str | None
    _status_callback: Any
    _stop_checker: Any

    def __init__(
        self,
        llm_client: LlmClient,
        max_tokens: int = 1024,
        status_callback: Any = None,
        stop_checker: Any = None,
    ) -> None:
        # Note: Model.kwargs are intentionally ignored; request_with_tools defines wire keys.
        super().__init__()
        self.api = llm_client
        self.max_tokens = max_tokens
        self.model_id = self.api.config.get("model")
        self._status_callback = status_callback
        self._stop_checker = stop_checker

    def generate(
        self,
        messages: list[ChatMessage],
        stop_sequences: list[str] | None = None,
        response_format: dict[str, str] | None = None,
        tools_to_call_from: list[SmolTool] | None = None,
        **kwargs: Any,
    ) -> ChatMessage:
        completion_kwargs = self._prepare_completion_kwargs(
            messages=cast("list[ChatMessage | dict[str, Any]]", messages),
            stop_sequences=stop_sequences,
            tools_to_call_from=tools_to_call_from,
            **kwargs,
        )

        dropped = set(completion_kwargs.keys()) - _SMOL_WIRE_KEYS
        if dropped:
            log.debug("Smol completion kwargs dropped: %s", dropped)

        msg_dicts = completion_kwargs.get("messages", [])
        tools = completion_kwargs.get("tools", None)
        fmt = completion_kwargs.get("response_format", response_format)

        if self._status_callback:
            self._status_callback("Thinking...")

        # Preserve the known-good smolagents request shape: schemas are both in the
        # smol prompt and on the wire. Some local backends select a different parser
        # path when OpenAI-style tools are present.
        result = self.api.request_with_tools(
            msg_dicts,
            max_tokens=self.max_tokens,
            tools=tools,
            model=self.model_id,
            response_format=fmt,
            prepend_dev_build_system_prefix=False,
            stop_checker=self._stop_checker,
        )

        if self._status_callback:
            self._status_callback("Model responded, processing...")

        content = result.get("content") or ""
        if stop_sequences is not None:
            trimmed = remove_content_after_stop_sequences(content, stop_sequences)
            if trimmed is not None:
                content = trimmed

        usage = result.get("usage") or {}
        token_usage = TokenUsage(input_tokens=usage.get("prompt_tokens", 0), output_tokens=usage.get("completion_tokens", 0)) if usage else None
        return ChatMessage.from_dict({"role": "assistant", "content": content, "tool_calls": result.get("tool_calls") or None}, raw=result, token_usage=token_usage)


class SmolAgentExecutor:
    """Executes a smolagent and streams its progress to the document chat UI."""

    ctx: ToolContext
    status_callback: Any
    append_thinking_callback: Any
    stop_checker: Any

    def __init__(self, ctx: ToolContext) -> None:
        """Initialize the executor with the tool context.

        Args:
            ctx: ToolContext with doc, services, and UI callbacks.
        """
        self.ctx = ctx
        self.status_callback = getattr(ctx, "status_callback", None)
        self.append_thinking_callback = getattr(ctx, "append_thinking_callback", None)
        self.stop_checker = getattr(ctx, "stop_checker", None)

    def _abort_if_stopped(self, agent: Any) -> None:
        if self.stop_checker and self.stop_checker():
            interrupt = getattr(agent, "interrupt", None)
            if callable(interrupt):
                interrupt()
            raise ToolExecutionError("Task stopped by user.", code="USER_STOPPED")

    def run(self, agent: Any, task: str, tool_call_handler: Callable[[ToolCall], Any] | None = None) -> Any:
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
        stream_iter = iter(cast("Iterable[Any]", agent.run(task, stream=True)))

        try:
            while True:
                self._abort_if_stopped(agent)
                try:
                    step = next(stream_iter)
                except StopIteration:
                    break
                self._abort_if_stopped(agent)

                if isinstance(step, ToolCall):
                    if tool_call_handler:
                        res = tool_call_handler(step)
                        if res is not None:
                            interrupt = getattr(agent, "interrupt", None)
                            if callable(interrupt):
                                interrupt()
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
        finally:
            close_fn = getattr(stream_iter, "close", None)
            if callable(close_fn):
                try:
                    close_fn()
                except Exception as err:
                    log.debug("Error closing agent run generator: %s", err)

    def execute_safe(self, agent: Any, task: str, tool_call_handler: Callable[[ToolCall], Any] | None = None, stop_message: str = "Stopped by user.", error_prefix: str = "Task failed") -> Any:
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
            log.exception("%s execution failed", error_prefix)
            err = ToolExecutionError(f"{error_prefix}: {str(e)}", code=e.code, details=e.details)
            return format_error_payload(err)
        except Exception as e:
            log.exception("%s failed", error_prefix)
            err = ToolExecutionError(f"{error_prefix}: {str(e)}")
            return format_error_payload(err)


def create_smol_model(
    ctx: ToolContext | None = None,
    *,
    client: LlmClient | None = None,
    max_tokens: int | None = None,
    status_callback: object | None = None,
    stop_checker: Callable[[], bool] | None = None,
    cancellation_scope: Any | None = None,
) -> WriterAgentSmolModel:
    """Construct a WriterAgentSmolModel with configured client, max tokens, and callbacks."""
    if max_tokens is None:
        max_tokens = get_config_int("chat_max_tokens")
    if stop_checker is None and ctx is not None:
        stop_checker = getattr(ctx, "stop_checker", None)
    if cancellation_scope is None and ctx is not None:
        cancellation_scope = getattr(ctx, "send_cancellation", None)
    if status_callback is None and ctx is not None:
        status_callback = getattr(ctx, "status_callback", None)
    if client is None:
        uno_ctx = ctx.ctx if ctx is not None else None
        config = get_api_config()
        client_cls = LlmClient if LlmClient is not _ORIGINAL_LLM_CLIENT else llm_client.LlmClient
        client = client_cls(config, uno_ctx, cancellation_scope=cancellation_scope)
    return WriterAgentSmolModel(
        client,
        max_tokens=max_tokens,
        status_callback=status_callback,
        stop_checker=stop_checker,
    )


def build_toolcalling_agent(ctx: ToolContext, tools: Sequence[SmolTool], *, instructions: str, final_answer_tool_name: str, examples_block: str, status_callback: object | None = None) -> ToolCallingAgent:
    """Shared construction for smolagents runs (same config as main chat: model, max_tokens, max_steps)."""
    max_steps = get_config_int("chatbot.max_tool_rounds")
    smol_model = create_smol_model(ctx, status_callback=status_callback)
    return ToolCallingAgent(tools=list(tools), model=smol_model, max_steps=max_steps, instructions=instructions, final_answer_tool_name=final_answer_tool_name, system_prompt_examples=examples_block)


def run_inner_tool_agent(
    ctx: ToolContext,
    tools: Sequence[SmolTool],
    *,
    instructions: str,
    examples_key: str,
    final_answer_tool_name: str,
    task: str,
    stop_message: str,
    error_prefix: str,
    tool_call_handler: Callable[[Any], Any] | None = None,
) -> Any:
    """Build and execute a tool-calling agent loop safely."""
    examples_block = smol_examples.get_examples_block(examples_key)
    agent = build_toolcalling_agent(
        ctx,
        tools,
        instructions=instructions,
        final_answer_tool_name=final_answer_tool_name,
        examples_block=examples_block,
        status_callback=getattr(ctx, "status_callback", None),
    )
    # execute_safe calls interrupt() before it reports USER_STOPPED, which
    # closes the model's HTTP client. Returning from this loop without that
    # leaves agent.run() going after the sidebar has stopped.
    return SmolAgentExecutor(ctx).execute_safe(
        agent,
        task,
        tool_call_handler=tool_call_handler,
        stop_message=stop_message,
        error_prefix=error_prefix,
    )


def _document_open_tool_handler(ctx: ToolContext) -> Callable[[ToolCall], None]:
    """Brainstorming and writing report each delegate_read_document in the chat.

    The status line is the tool name alone. Deep research does not use this
    handler, so its steps keep the executor default (``Tool: name...``).
    """
    status_callback = getattr(ctx, "status_callback", None)
    append_thinking_callback = getattr(ctx, "append_thinking_callback", None)
    chat_append_callback = getattr(ctx, "chat_append_callback", None)
    document_open_step_index = 0

    def tool_call_handler(step: ToolCall) -> None:
        nonlocal document_open_step_index
        if step.name == "delegate_read_document" and chat_append_callback:
            from plugin.chatbot.web_research_chat import document_open_step_chat_text

            path_or_name = field_from_tool_arguments(step.arguments, "path_or_name")
            chat_append_callback(document_open_step_chat_text(path_or_name, document_open_step_index))
            document_open_step_index += 1
        if append_thinking_callback:
            append_thinking_callback(f"Running tool: {step.name} with {step.arguments}\n")
        if status_callback:
            status_callback(f"{step.name}...")

    return tool_call_handler


def run_smol_side_turn(
    ctx: ToolContext,
    *,
    query: str,
    history_text: str | None,
    collector: Callable[[ToolContext], Sequence[ToolBase]],
    instructions: str,
    examples_key: str,
    reply_spec: StickyReplySpec | None = None,
    report_document_opens: bool = False,
    status_message: str,
    stop_message: str,
    error_prefix: str,
    collector_safe: bool = True,
    inputs_style: SmolInputsStyle = "specialized",
    tool_call_handler: Callable[[Any], Any] | None = None,
) -> dict[str, Any]:
    """One smol turn shared by brainstorming, writing plan, and deep research.

    The three runners were the same setup. Status text, the stop message, and
    whether a document open is written into the chat stay arguments so deep
    research can keep the executor's default tool status.
    """
    from plugin.chatbot.web_research_chat import clip_sub_agent_history

    status_callback = getattr(ctx, "status_callback", None)
    history_text = clip_sub_agent_history(history_text)
    if status_callback:
        status_callback(status_message)

    reply_tool: Any | None = None
    if reply_spec is not None:
        from plugin.chatbot.sticky_reply import StickyReplyToUserTool

        reply_tool = StickyReplyToUserTool(reply_spec)

    class _ObservedSmolToolAdapter(SmolToolAdapter):
        def forward(self, *args: Any, **kwargs: Any) -> Any:
            result = super().forward(*args, **kwargs)
            if reply_tool is not None:
                reply_tool.record_tool_result(self.name or "", result)
            return result

    collector_tools = collector(ctx)
    adapter_cls = _ObservedSmolToolAdapter if reply_tool is not None else SmolToolAdapter
    smol_tools: list[SmolTool] = [
        adapter_cls(t, ctx, safe=collector_safe, inputs_style=inputs_style) for t in collector_tools
    ]

    if reply_tool is not None:
        smol_tools.append(SmolToolAdapter(reply_tool, ctx, safe=False, inputs_style="librarian"))

    task = f"### CONVERSATION HISTORY:\n{history_text or 'None'}\n\n### CURRENT QUERY:\n{query}"
    effective_handler = tool_call_handler or (_document_open_tool_handler(ctx) if report_document_opens else None)
    res = run_inner_tool_agent(
        ctx,
        smol_tools,
        instructions=instructions,
        examples_key=examples_key,
        final_answer_tool_name="reply_to_user",
        task=task,
        stop_message=stop_message,
        error_prefix=error_prefix,
        tool_call_handler=effective_handler,
    )
    if isinstance(res, dict) and res.get("status") == "error":
        return res
    if reply_spec is not None:
        from plugin.chatbot.sticky_reply import interpret_sticky_final_answer

        return interpret_sticky_final_answer(res, leave_status=reply_spec.leave_status)
    return {"status": "ok", "result": str(res)}
