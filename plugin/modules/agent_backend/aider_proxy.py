"""Aider agent backend adapter. Stub until Aider integration is implemented."""

from plugin.modules.agent_backend.base import AgentBackend


class AiderBackend(AgentBackend):
    backend_id = "aider"
    display_name = "Aider"

    def __init__(self, ctx=None):
        self._ctx = ctx
        self._stop_requested = False

    def is_available(self, ctx):
        # Optional: check for aider on PATH or Python package
        try:
            from plugin.framework.config import get_config
            path = get_config(ctx, "agent_backend.path", "")
            if path:
                return True
            try:
                import aider
                return True
            except ImportError:
                pass
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
        # Stub: push a clear error so user knows Aider is not yet implemented
        queue.put(("status", "Aider backend not yet implemented"))
        queue.put((
            "error",
            RuntimeError(
                "Aider agent backend is not yet implemented. "
                "Use Built-in or Hermes, or contribute an implementation in plugin/modules/agent_backend/aider_proxy.py"
            ),
        ))
