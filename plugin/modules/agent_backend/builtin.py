"""Built-in backend: no-op. Sidebar uses the existing in-process LlmClient path."""

from plugin.modules.agent_backend.base import AgentBackend


class BuiltinBackend(AgentBackend):
    backend_id = "builtin"
    display_name = "Built-in"

    def send(self, queue, user_message, document_context, document_url, **kwargs):
        # Should not be called; sidebar branches away when backend is builtin.
        queue.put(("error", RuntimeError("Built-in backend should not receive send()")))
