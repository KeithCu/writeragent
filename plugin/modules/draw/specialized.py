import logging

from plugin.framework.tool_base import ToolBase
from plugin.framework.constants import DELEGATE_SPECIALIZED_TASK_PARAM_HINT, USE_SUB_AGENT
from plugin.framework.errors import format_error_payload, ToolExecutionError
from plugin.framework.i18n import _

log = logging.getLogger("writeragent.draw")


class DelegateToSpecializedDraw(ToolBase):
    """Gateway tool to delegate tasks to specialized Draw toolsets.

    This spins up a sub-agent with a limited set of tools to focus on the
    user's specific request, preventing context pollution.
    """

    name = "delegate_to_specialized_draw_toolset"
    description = (
        "Delegates a specialized task to a sub-agent with a focused toolset. "
        "Use this for complex Draw operations like creating and editing shapes, "
        "charts, and other page elements."
    )

    def __init__(self):
        super().__init__()
        from plugin.modules.draw.base import ToolDrawSpecialBase
        domains = []
        for cls in ToolDrawSpecialBase.__subclasses__():
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
                    "description": DELEGATE_SPECIALIZED_TASK_PARAM_HINT,
                },
            },
            "required": ["domain", "task"],
        }

    uno_services = [
        "com.sun.star.drawing.DrawingDocument",
        "com.sun.star.presentation.PresentationDocument",
    ]
    tier = "core"  # Available to the main agent
    is_mutation = True
    long_running = True

    def is_async(self):
        return True

    def execute(self, ctx, **kwargs):
        domain = kwargs.get("domain")
        task = kwargs.get("task")

        status_callback = getattr(ctx, "status_callback", None)

        if domain == "web_research":
            from plugin.modules.chatbot.web_research import WebResearchTool
            tool = WebResearchTool()
            return tool.execute(ctx, query=task)

        if not USE_SUB_AGENT:
            # Tell the main LLM loop to switch tools for the next round
            if getattr(ctx, "set_active_domain_callback", None):
                ctx.set_active_domain_callback(domain)

            msg = _("Tool call switched to '{0}'. You are in a specialized toolset mode. "
                    "You must call 'specialized_workflow_finished' when done to restore "
                    "the full set of APIs.").format(domain)

            if status_callback:
                status_callback(f"Switched to '{domain}' tools.")

            return {
                "status": "ok",
                "message": msg,
            }

        # For now, we only support in-place switching for Draw specialized tools
        # unless we want to copy the full smolagents loop from Writer.
        # Given the user request, let's at least enable the in-place switching.
        
        return format_error_payload(ToolExecutionError(f"Sub-agent mode for domain {domain} not yet implemented for Draw. Try setting USE_SUB_AGENT=False in constants.py or use in-place switching."))
