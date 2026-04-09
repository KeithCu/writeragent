import logging

from plugin.framework.tool_base import ToolBase

log = logging.getLogger("writeragent.draw")

class DelegateToSpecializedDraw(ToolBase):
    """Gateway tool to delegate tasks to specialized Draw toolsets.

    This spins up a sub-agent with a limited set of tools to focus on the
    user's specific request, preventing context pollution.
    """

    name = "delegate_to_specialized_draw_toolset"
    description = (
        "Delegates a specialized task to a sub-agent with a focused toolset. "
        "Use this for complex Draw operations."
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
                    "description": (
                        "A detailed description of the task for the specialized "
                        "agent to accomplish."
                    ),
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

        if domain == "web_research":
            from plugin.modules.chatbot.web_research import WebResearchTool
            tool = WebResearchTool()
            return tool.execute(ctx, query=task)

        # Later we can add actual Draw-specific sub-agents here
        from plugin.framework.errors import format_error_payload, ToolExecutionError
        return format_error_payload(ToolExecutionError(f"Domain {domain} not implemented for Draw"))
