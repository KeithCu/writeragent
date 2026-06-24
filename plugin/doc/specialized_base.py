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
from typing import Any, cast, Type, ClassVar

from plugin.framework.tool import ToolBase
from plugin.framework.constants import DELEGATE_SPECIALIZED_TASK_PARAM_HINT, USE_SUB_AGENT, WRITER_SIDEBAR_ONLY_DOMAINS, python_specialized_sub_agent_hint
from plugin.framework.i18n import _
from plugin.chatbot.smol_agent import build_toolcalling_agent, SmolAgentExecutor, SmolToolAdapter
from plugin.chatbot.smol_examples import get_examples_block
from plugin.doc.document_research import get_document_research_workflow_hint
from plugin.doc.specialized_shapes_context import format_shapes_canvas_context
from plugin.framework import queue_executor

log = logging.getLogger("writeragent.specialized")


def _field_from_tool_arguments(arguments: Any, field: str) -> Any:
    """Read *field* from tool arguments (dict or JSON string), or None."""
    if arguments is None:
        return None
    if isinstance(arguments, dict):
        return arguments.get(field)
    if isinstance(arguments, str):
        try:
            from plugin.framework.errors import safe_json_loads

            data = safe_json_loads(arguments)
            if isinstance(data, dict):
                return data.get(field)
        except Exception:
            pass
    return None


def _path_or_name_from_tool_arguments(arguments: Any) -> str:
    val = _field_from_tool_arguments(arguments, "path_or_name")
    return str(val) if val is not None else ""


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
                if self._agent_label == "Calc" and domain == "python":
                    continue
                if self._agent_label == "Writer" and domain in WRITER_SIDEBAR_ONLY_DOMAINS:
                    continue
                domains.append(domain)

        self.parameters = {
            "type": "object",
            "properties": {
                "domain": {"type": "string", "enum": domains, "description": "The specialized domain to activate."},
                # python_tool_domain: enable when domain-scoped writeragent_api proxy (venv → LO RPC) is tested.
                # "python_tool_domain": {
                #     "type": "string",
                #     "description": "Optional domain for tools to expose to Python, e.g. 'core', or a specialized domain like 'footnotes'. Required when domain='python' to specify tool access for the script.",
                # },
                "task": {"type": "string", "description": DELEGATE_SPECIALIZED_TASK_PARAM_HINT},
            },
            "required": ["domain", "task"],
        }

    def is_async(self):
        """Run in a background thread so the main-thread queue/drain loop isn't blocked."""
        return True

    # Domains whose work is read-only -> a long-running delegation to them must NOT
    # take the per-document mutation lock (it would needlessly serialize research on
    # the same doc). The gateway itself is is_mutation=True for the mutating domains.
    _READ_ONLY_DOMAINS = frozenset({"document_research", "web_research", "vision"})

    def requires_document_lock(self, arguments=None):
        domain = _field_from_tool_arguments(arguments, "domain")
        if domain in self._READ_ONLY_DOMAINS:
            return False
        return super().requires_document_lock(arguments)

    def execute(self, ctx, **kwargs):
        domain = kwargs.get("domain")
        python_tool_domain = kwargs.get("python_tool_domain")
        task = kwargs.get("task")

        status_callback = getattr(ctx, "status_callback", None)
        append_thinking_callback = getattr(ctx, "append_thinking_callback", None)
        chat_append_callback = getattr(ctx, "chat_append_callback", None)

        if domain == "web_research":
            from plugin.chatbot.web_research import WebResearchTool

            tool = WebResearchTool()
            return tool.execute(ctx, query=task)

        if domain == "vision":
            from plugin.vision.vision_availability import vision_ocr_available
            from plugin.vision.vision_tools import ExtractTextFromImage

            if not vision_ocr_available(ctx.ctx):
                return self._tool_error(
                    _(
                        "Local OCR requires Settings → Python venv with Docling or PaddleOCR "
                        "(Settings → Python → Test for install hints)."
                    ),
                    code="VISION_UNAVAILABLE",
                )

            if USE_SUB_AGENT:
                if status_callback:
                    status_callback(_("Running local OCR on selected image..."))
                # Gateway shortcut: no sub-agent parses task — always insert OCR at cursor/cell.
                return ExtractTextFromImage().execute(ctx, insert_into_document=True)

        if domain == "document_research" and not USE_SUB_AGENT:
            return self._tool_error(
                _("Document research reads require specialized task delegation (USE_SUB_AGENT). Enable this in configuration."),
                code="DOCUMENT_RESEARCH_REQUIRES_SUB_AGENT",
            )

        if not USE_SUB_AGENT:
            # Tell the main LLM loop to switch tools for the next round
            callback = getattr(ctx, "set_active_domain_callback", None)
            if callback:
                callback(domain, python_tool_domain=python_tool_domain)

            msg = _("Tool call switched to '{0}'. You are in a specialized toolset mode. You must call 'specialized_workflow_finished' when done to restore the full set of APIs.").format(domain)

            if status_callback:
                status_callback(f"Switched to '{domain}' tools.")

            return {"status": "ok", "message": msg}

        if domain == "document_research":
            try:
                from plugin.embeddings.embeddings_indexer import enqueue_folder_index

                # Sub-agent runs on a worker thread; resolve_index_context reads the active doc path via UNO.
                queue_executor.execute_on_main_thread(lambda: enqueue_folder_index(ctx.ctx, ctx.services, ctx.doc))
            except Exception:
                log.debug("embeddings index wakeup failed", exc_info=True)

        if status_callback:
            status_callback(f"Delegating to specialized agent ({domain})...")

        # Gather tools for the requested domain (same rules as main chat ``active_domain``).
        # Must use ``ToolRegistry.get_tools(..., active_domain=...)`` so cross-app tools
        # (e.g. ``RunVenvPythonScript`` with ``specialized_cross_cutting``) are included;
        # ``isinstance(..., _special_base_class)`` alone misses Calc-registered tools on Writer delegate.
        registry = ctx.services.get("tools")

        def _fetch_domain_tools():
            tools = registry.get_tools(
                doc=getattr(ctx, "doc", None),
                active_domain=domain,
                exclude_tiers=(),
                ctx=ctx.ctx,
            )
            if domain == "document_research":
                from plugin.doc.document_research import filter_document_research_discovery_tools

                tools = filter_document_research_discovery_tools(tools, ctx.ctx)
            return tools

        # get_tools(doc=...) calls doc.supportsService — must not run on the sub-agent worker.
        domain_tools = queue_executor.execute_on_main_thread(_fetch_domain_tools)

        if not domain_tools:
            return self._tool_error(f"No specialized tools found for domain '{domain}'. Ensure the tools are implemented and registered.")

        smol_tools = [SmolToolAdapter(t, ctx, safe=True, main_thread_sync=True, inputs_style="specialized") for t in domain_tools]

        footnotes_hint = ""
        if domain == "footnotes":
            footnotes_hint = " For footnotes_insert: if the task quotes or names the document anchor (e.g. a sentence), pass that exact string as insert_after_text so the note is placed after that text; the task executor cannot move the view cursor."
        shapes_canvas = ""
        if domain == "shapes":
            try:
                canvas = queue_executor.execute_on_main_thread(lambda: format_shapes_canvas_context(getattr(ctx, "doc", None)))
            except Exception as e:
                log.warning("Failed to get shapes canvas for sub-agent: %s", e)
                canvas = ""
            if canvas:
                shapes_canvas = canvas

        charts_hint = ""
        if domain == "charts":
            if self._agent_label == "Calc":
                charts_hint = " When creating a chart in Calc, you MUST specify the data range explicitly (e.g. data_range='A1:B10')."
            elif self._agent_label in ("Writer", "Draw"):
                charts_hint = " When creating or editing a chart in Writer or Draw/Impress, you MUST specify both the `headers` and `rows` parameters."

        calc_ctx = ""
        if self._agent_label == "Calc" and getattr(ctx, "doc", None):
            from plugin.doc.document_helpers import get_calc_context_for_chat

            def _fetch_calc_context() -> str:
                return "\n\n[SPREADSHEET CONTEXT]\n" + get_calc_context_for_chat(ctx.doc, ctx=ctx.ctx)

            try:
                # Sub-agent runs on a worker thread; UNO reads must go through the main thread.
                calc_ctx = queue_executor.execute_on_main_thread(_fetch_calc_context)
            except Exception as e:
                log.warning("Failed to get Calc context for sub-agent: %s", e)

        document_research_hint = get_document_research_workflow_hint(ctx.ctx) if domain == "document_research" else ""
        open_docs_context = ""
        if domain == "document_research":
            try:
                from plugin.doc.document_research import get_open_documents

                open_docs = queue_executor.execute_on_main_thread(lambda: get_open_documents(ctx.ctx, ctx.doc))
                if open_docs:
                    lines = []
                    for d in open_docs:
                        path_or_url = d["path"] or d["url"] or "Untitled"
                        doc_type = d["doc_type"]
                        active_str = " (Active)" if d["is_active"] else ""
                        lines.append(f"- {path_or_url} [{doc_type}]{active_str}")
                    open_docs_context = (
                        "\n\n[OPEN DOCUMENTS CONTEXT]\n"
                        "Note: These are the currently open files in LibreOffice. "
                        "Some of these files may be completely unrelated to the task at hand:\n"
                        + "\n".join(lines)
                    )
            except Exception as e:
                log.warning("Failed to get open documents for sub-agent: %s", e)

        images_hint = (
            " Discover local image files with list_nearby_image_files before insert_image when the user refers to a photo in the folder."
            if domain == "images"
            else ""
        )
        analysis_hint = (
            " For stats, cleaning, regression, clustering, or simulation on tabular data, use analyze_data with the appropriate helper. "
            "For charts and plots, use plot_data (or set auto_plot=true on analyze_data when the user wants a chart). "
            "For live formula what-if (single target cell), use calc_goal_seek. "
            "For constrained optimization on formula cells, use calc_solver. Prefer these over inventing raw pandas code. "
            "For bulk data always pass a data_range (A1 address); the host performs the extraction."
            if domain == "analysis"
            else ""
        )
        python_hint = python_specialized_sub_agent_hint(self._agent_label) if domain == "python" else ""
        instructions = (
            f"You are a specialized {self._agent_label} task executor focused on the '{domain}' domain. "
            f"You have a focused set of tools to accomplish your task. Use them to fulfill the user's request."
            f"{footnotes_hint}{shapes_canvas}{charts_hint}{calc_ctx}{document_research_hint}{open_docs_context}{images_hint}{analysis_hint}{python_hint}"
        )


        examples_key = f"{self._agent_label.lower()}:{domain}"
        agent = build_toolcalling_agent(ctx, smol_tools, instructions=instructions, final_answer_tool_name="specialized_workflow_finished", examples_block=get_examples_block(examples_key), status_callback=status_callback)

        executor = SmolAgentExecutor(ctx)

        document_open_step_index = 0

        def tool_call_handler(step):
            nonlocal document_open_step_index
            if domain == "document_research" and step.name == "delegate_read_document" and chat_append_callback:
                from plugin.chatbot.document_research_chat import document_open_step_chat_text

                path_or_name = _path_or_name_from_tool_arguments(step.arguments)
                chat_append_callback(document_open_step_chat_text(path_or_name, document_open_step_index))
                document_open_step_index += 1
            if append_thinking_callback:
                append_thinking_callback(f"Running specialized tool: {step.name} with {step.arguments}\n")
            if status_callback:
                status_callback(f"Tool: {step.name}...")

        final_ans = executor.execute_safe(agent, cast("str", task), tool_call_handler=tool_call_handler, stop_message="Specialized task stopped by user.", error_prefix="Specialized agent failed")

        if isinstance(final_ans, dict) and "status" in final_ans:
            return final_ans

        return {"status": "ok", "message": _(f"Specialized task ({domain}) completed."), "result": str(final_ans)}
