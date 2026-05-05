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
"""Shared base class for gateway tools that delegate to specialized toolsets."""

import logging
from typing import cast, Type, ClassVar

from plugin.framework.tool_base import ToolBase
from plugin.framework.constants import DELEGATE_SPECIALIZED_TASK_PARAM_HINT, USE_SUB_AGENT
from plugin.framework.i18n import _
from plugin.contrib.smolagents.toolcalling_agent_prompts import SPECIALIZED_EXAMPLES_BLOCK
from plugin.framework.smol_agent_factory import build_toolcalling_agent
from plugin.framework.smol_executor import SmolAgentExecutor
from plugin.framework.smol_tool_adapter import SmolToolAdapter
from plugin.framework.specialized_shapes_context import format_shapes_canvas_context

log = logging.getLogger("writeragent.specialized")


class DelegateToSpecializedBase(ToolBase):
    """Shared base for tools that delegate tasks to specialized sub-agents."""

    # Subclasses MUST override these
    _special_base_class: ClassVar[Type[ToolBase]]
    _agent_label: ClassVar[str]  # e.g., "Writer", "Calc", "Draw"

    tier = "core"  # Available to the main agent
    is_mutation = True
    long_running = True

    def __init__(self):
        super().__init__()
        domains = []
        # Find all domains by scanning subclasses of the specialized base
        for cls in self._special_base_class.__subclasses__():
            domain = getattr(cls, "specialized_domain", None)
            if domain:
                domains.append(domain)

        self.parameters = {"type": "object", "properties": {"domain": {"type": "string", "enum": domains, "description": "The specialized domain to activate."}, "task": {"type": "string", "description": DELEGATE_SPECIALIZED_TASK_PARAM_HINT}}, "required": ["domain", "task"]}

    def is_async(self):
        """Run in a background thread so the main-thread queue/drain loop isn't blocked."""
        return True

    def execute(self, ctx, **kwargs):
        domain = kwargs.get("domain")
        task = kwargs.get("task")

        status_callback = getattr(ctx, "status_callback", None)
        append_thinking_callback = getattr(ctx, "append_thinking_callback", None)

        if domain == "web_research":
            from plugin.modules.chatbot.web_research import WebResearchTool

            tool = WebResearchTool()
            return tool.execute(ctx, query=task)

        if not USE_SUB_AGENT:
            # Tell the main LLM loop to switch tools for the next round
            if getattr(ctx, "set_active_domain_callback", None):
                ctx.set_active_domain_callback(domain)

            msg = _("Tool call switched to '{0}'. You are in a specialized toolset mode. You must call 'specialized_workflow_finished' when done to restore the full set of APIs.").format(domain)

            if status_callback:
                status_callback(f"Switched to '{domain}' tools.")

            return {"status": "ok", "message": msg}

        if status_callback:
            status_callback(f"Delegating to specialized agent ({domain})...")

        # Gather tools for the requested domain
        registry = ctx.services.get("tools")

        # Get ALL registered tools
        all_tools = registry.get_tools(filter_doc_type=False, exclude_tiers=())

        domain_tools = []
        for t in all_tools:
            # Check if it's a subclass of our specific base and matches the domain
            if isinstance(t, self._special_base_class) and getattr(t, "specialized_domain", None) == domain:
                domain_tools.append(t)

        if not domain_tools:
            return self._tool_error(f"No specialized tools found for domain '{domain}'. Ensure the tools are implemented and registered.")

        smol_tools = [SmolToolAdapter(t, ctx, safe=True, main_thread_sync=True, inputs_style="specialized") for t in domain_tools]

        footnotes_hint = ""
        if domain == "footnotes":
            footnotes_hint = " For footnotes_insert: if the task quotes or names the document anchor (e.g. a sentence), pass that exact string as insert_after_text so the note is placed after that text; the sub-agent cannot move the view cursor."
        shapes_canvas = ""
        if domain == "shapes":
            canvas = format_shapes_canvas_context(getattr(ctx, "doc", None))
            if canvas:
                shapes_canvas = canvas
        instructions = f"You are a specialized {self._agent_label} agent focused on the '{domain}' domain. You have a focused set of tools to accomplish your task. Use them to fulfill the user's request.{footnotes_hint}{shapes_canvas}"

        agent = build_toolcalling_agent(ctx, smol_tools, instructions=instructions, final_answer_tool_name="specialized_workflow_finished", examples_block=SPECIALIZED_EXAMPLES_BLOCK, status_callback=status_callback)

        executor = SmolAgentExecutor(ctx)

        def tool_call_handler(step):
            if append_thinking_callback:
                append_thinking_callback(f"Running specialized tool: {step.name} with {step.arguments}\n")
            if status_callback:
                status_callback(f"Tool: {step.name}...")

        final_ans = executor.execute_safe(agent, cast("str", task), tool_call_handler=tool_call_handler, stop_message="Specialized task stopped by user.", error_prefix="Specialized agent failed")

        if isinstance(final_ans, dict) and "status" in final_ans:
            return final_ans

        return {"status": "ok", "message": _(f"Specialized task ({domain}) completed."), "result": str(final_ans)}
