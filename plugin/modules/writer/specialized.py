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


# Available domains matching the specialized_domain attributes of subclasses
_AVAILABLE_DOMAINS = [
    "tables",
    "styles",
    "layout",
    "embedded",
    "shapes",
    "charts",
    "indexes",
    "fields",
    "bookmarks",
    "tracking",
]


class DelegateToSpecializedWriter(ToolBase):
    """Gateway tool to delegate tasks to specialized Writer toolsets.

    This spins up a sub-agent with a limited set of tools (e.g., only Table tools)
    to focus on the user's specific request, preventing context pollution.
    """

    name = "delegate_to_specialized_writer_toolset"
    description = (
        "Delegates a specialized task to a sub-agent with a focused toolset. "
        "Use this for complex Writer operations like manipulating tables, "
        "charts, fields, styles, layout, embedded objects, shapes, indexes, "
        "bookmarks, or track changes (tracking)."
    )
    parameters = {
        "type": "object",
        "properties": {
            "domain": {
                "type": "string",
                "enum": _AVAILABLE_DOMAINS,
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

        domain = kwargs.get("domain")
        task = kwargs.get("task")

        status_callback = getattr(ctx, "status_callback", None)
        append_thinking_callback = getattr(ctx, "append_thinking_callback", None)
        stop_checker = getattr(ctx, "stop_checker", None)

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
                elif getattr(t, "name", "") == "specialized_workflow_finished":
                    domain_tools.append(t)

            if not domain_tools:
                return self._tool_error(
                    f"No specialized tools found for domain '{domain}'. "
                    f"Ensure the tools are implemented and registered."
                )

            # Create a simple wrapper for each ToolBase to expose it to smolagents
            from plugin.contrib.smolagents.tools import Tool as SmolTool

            class WrappedSmolTool(SmolTool):
                def __init__(self, writer_tool, ctx):
                    self.writer_tool = writer_tool
                    self.ctx = ctx
                    self.name = writer_tool.name
                    self.description = writer_tool.description
                    super().__init__()

                def __call__(self, *args, **kwargs):
                    return self.forward(*args, **kwargs)

                def forward(self, *args, **kwargs):
                    # Convert arguments and execute via the writer tool
                    return self.writer_tool.execute_safe(self.ctx, **kwargs)

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
                f"Use them to fulfill the user's request. When you are finished, "
                f"you MUST call the 'specialized_workflow_finished' tool with a summary. "
                f"Do not attempt to provide a final answer directly until you have called this tool."
            )

            agent = ToolCallingAgent(
                tools=smol_tools,
                model=smol_model,
                max_steps=10,
                instructions=instructions,
            )

            final_ans = None

            for step in agent.run(task, stream=True):
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
                            msg += f"{step.model_output.strip()}\n"
                        elif getattr(step, "model_output_message", None) and step.model_output_message.content:
                            msg += f"{str(step.model_output_message.content).strip()}\n"

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
