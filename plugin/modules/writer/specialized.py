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
"""Gateway tool to delegate tasks to specialized Writer toolsets."""

import logging

from plugin.framework.tool_base import ToolBase
from plugin.modules.writer.base import ToolWriterSpecialBase

log = logging.getLogger("writeragent.writer")


# Global variable to toggle between the sub-agent approach (True) and the
# in-place tool-switching approach (False).
USE_SUB_AGENT = True


class DelegateToSpecializedWriter(ToolBase):
    """Gateway tool to delegate tasks to specialized Writer toolsets.

    This spins up a sub-agent with a limited set of tools (e.g., only Table tools)
    to focus on the user's specific request, preventing context pollution.
    """

    name = "delegate_to_specialized_writer_toolset"
    description = (
        "Delegates a specialized task to a sub-agent with a focused toolset. "
        "Use this for complex Writer operations like manipulating tables, "
        "charts, fields, styles, layout (page styles, margins, headers/footers, columns, page breaks), "
        "textframes (list_text_frames, get_text_frame_info, set_text_frame_properties), "
        "embedded objects, shapes, indexes, "
        "bookmarks, track changes (tracking), footnotes/endnotes (domain=footnotes), "
        "or in-document image work (domain=images: generate, list, insert, replace images, etc.)."
    )

    def __init__(self):
        super().__init__()
        from plugin.modules.writer.base import ToolWriterSpecialBase
        domains = []
        for cls in ToolWriterSpecialBase.__subclasses__():
            if getattr(cls, "specialized_domain", None):
                domains.append(cls.specialized_domain)

        self.parameters = {
            "type": "object",
            "properties": {
                "domain": {
                    "type": "string",
                    "enum": domains,
                    "description": "The specialized domain to activate.",
                },
                "task": {
                    "type": "string",
                    "description": (
                        "A detailed description of the task for the specialized "
                        "agent to accomplish."
                    ),
                },
            },
            "required": ["domain", "task"],
        }

    uno_services = ["com.sun.star.text.TextDocument"]
    tier = "core"  # Available to the main agent
    is_mutation = True
    long_running = True

    def is_async(self):
        """Run in a background thread so the main-thread queue/drain loop isn't blocked."""
        return True

    def execute(self, ctx, **kwargs):
        from plugin.framework.errors import format_error_payload, ToolExecutionError
        from plugin.framework.config import get_api_config
        from plugin.modules.http.client import LlmClient
        from plugin.framework.smol_model import WriterAgentSmolModel
        from plugin.contrib.smolagents.agents import ToolCallingAgent
        from plugin.contrib.smolagents.memory import ActionStep, FinalAnswerStep, ToolCall
        from plugin.contrib.smolagents.toolcalling_agent_prompts import SPECIALIZED_EXAMPLES_BLOCK

        domain = kwargs.get("domain")
        task = kwargs.get("task")

        status_callback = getattr(ctx, "status_callback", None)
        append_thinking_callback = getattr(ctx, "append_thinking_callback", None)
        stop_checker = getattr(ctx, "stop_checker", None)

        if not USE_SUB_AGENT:
            # Tell the main LLM loop to switch tools for the next round
            if getattr(ctx, "set_active_domain_callback", None):
                ctx.set_active_domain_callback(domain)

            from plugin.framework.i18n import _
            msg = _("Tool call switched to '{0}'. You are in a specialized toolset mode. "
                    "You must call 'specialized_workflow_finished' when done to restore "
                    "the full set of APIs.").format(domain)

            if status_callback:
                status_callback(f"Switched to '{domain}' tools.")

            return {
                "status": "ok",
                "message": msg,
            }

        if status_callback:
            status_callback(f"Delegating to specialized agent ({domain})...")

        try:
            # Gather tools for the requested domain
            registry = ctx.services.get("tools")

            # Add the control tool
            all_tools = registry.get_tools(
                filter_doc_type=False,
                exclude_tiers=(),
            )

            domain_tools = []
            for t in all_tools:
                # Check if it's a subclass of our special base and matches the domain
                if isinstance(t, ToolWriterSpecialBase) and t.specialized_domain == domain:
                    domain_tools.append(t)

            if not domain_tools:
                return self._tool_error(
                    f"No specialized tools found for domain '{domain}'. "
                    f"Ensure the tools are implemented and registered."
                )

            # Create a simple wrapper for each ToolBase to expose it to smolagents
            from plugin.contrib.smolagents.tools import Tool as SmolTool

            class WrappedSmolTool(SmolTool):
                skip_forward_signature_validation = True
                def __init__(self, writer_tool, ctx):
                    self.writer_tool = writer_tool
                    self.ctx = ctx
                    self.name = writer_tool.name
                    self.description = writer_tool.description
                    #FIXME: the code used to work without this
                    # Convert JSON Schema parameters to smolagents inputs
                    self.inputs = {}
                    params = getattr(writer_tool, "parameters", {}) or {}
                    props = params.get("properties", {})
                    for param_name, spec in props.items():
                        # smolagents expects a dict with 'type' and 'description'
                        # but we also need to pass through 'items' for array types, etc.
                        self.inputs[param_name] = {**spec}
                        self.inputs[param_name]["type"] = spec.get("type", "any")
                        self.inputs[param_name]["description"] = spec.get("description", "")
                    
                    self.output_type = "object"
                    super().__init__()

                def __call__(self, *args, **kwargs):
                    return self.forward(*args, **kwargs)

                def forward(self, *args, **kwargs):
                    from plugin.framework.queue_executor import execute_on_main_thread

                    tool = self.writer_tool
                    if getattr(tool, "is_async", lambda: False)():
                        log.debug(
                            "Specialized agent executing async tool '%s' on worker",
                            self.name,
                        )
                        res = tool.execute_safe(self.ctx, **kwargs)
                    else:
                        log.debug(
                            "Specialized agent executing tool '%s' on main thread",
                            self.name,
                        )
                        res = execute_on_main_thread(
                            tool.execute_safe, self.ctx, **kwargs
                        )
                    log.debug("Specialized agent tool '%s' finished", self.name)
                    return res

            smol_tools = [WrappedSmolTool(t, ctx) for t in domain_tools]

            config = get_api_config(ctx.ctx)
            max_tokens = int(config.get("chat_max_tokens", 2048))

            # Using the same model configuration as the main chat
            smol_model = WriterAgentSmolModel(
                LlmClient(config, ctx.ctx), max_tokens=max_tokens,
                status_callback=status_callback,
            )

            instructions = (
                f"You are a specialized Writer agent focused on the '{domain}' domain. "
                f"You have a focused set of tools to accomplish your task. "
                f"Use them to fulfill the user's request."
            )

            from plugin.framework.config import get_config_int
            max_steps = get_config_int(ctx.ctx, "chat_max_tool_rounds")

            from typing import cast, Iterable
            from plugin.contrib.smolagents.tools import Tool as SmolTool
            agent = ToolCallingAgent(
                tools=cast(list[SmolTool], smol_tools),
                model=smol_model,
                max_steps=max_steps,
                instructions=instructions,
                final_answer_tool_name="specialized_workflow_finished",
                system_prompt_examples=SPECIALIZED_EXAMPLES_BLOCK,
            )

            final_ans = None

            run_stream = cast(Iterable, agent.run(cast(str, task), stream=True))
            for step in run_stream:
                if stop_checker and stop_checker():
                    return format_error_payload(ToolExecutionError("Specialized task stopped by user.", code="USER_STOPPED"))

                if isinstance(step, ToolCall):
                    if append_thinking_callback:
                        append_thinking_callback(f"Running specialized tool: {step.name} with {step.arguments}\n")
                    if status_callback:
                        status_callback(f"Tool: {step.name}...")

                elif isinstance(step, ActionStep):
                    if append_thinking_callback:
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

                        append_thinking_callback(msg + "\n")

                elif isinstance(step, FinalAnswerStep):
                    final_ans = step.output

            from plugin.framework.i18n import _

            return {
                "status": "ok",
                "message": _(f"Specialized task ({domain}) completed."),
                "result": str(final_ans),
            }

        except Exception as e:
            log.error("Specialized agent error: %s", e)
            err = ToolExecutionError(f"Specialized agent failed: {str(e)}", details={"domain": domain, "task": task})
            return format_error_payload(err)
