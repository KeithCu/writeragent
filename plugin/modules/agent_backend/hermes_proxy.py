"""Hermes agent backend adapter. Stub until Hermes integration is implemented."""

from plugin.modules.agent_backend.base import AgentBackend


class HermesBackend(AgentBackend):
    backend_id = "hermes"
    display_name = "Hermes"

    def __init__(self, ctx=None):
        self._ctx = ctx
        self._stop_requested = False

    def is_available(self, ctx):
        try:
            from plugin.framework.config import get_config
            path = get_config(ctx, "agent_backend.path", "")
            if path:
                return True
        except Exception:
            pass
        return False

    def stop(self):
        self._stop_requested = True

    def send(
        self,
        queue,
        user_message,
        document_context,
        document_url,
        system_prompt=None,
        selection_text=None,
        stop_checker=None,
        **kwargs
    ):
        self._stop_requested = False
        queue.put(("status", "Hermes backend not yet implemented"))
        queue.put((
            "error",
            RuntimeError(
                "Hermes agent backend is not yet implemented. "
                "Use Built-in or Aider, or contribute an implementation in plugin/modules/agent_backend/hermes_proxy.py"
            ),
        ))
