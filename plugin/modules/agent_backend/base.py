"""Abstract base for agent backends. All adapters push events into a queue.Queue."""

import threading


class AgentBackend:
    """Contract for pluggable agent backends (Aider, Hermes, etc.).

    Inputs: user_message, document_context, document_url (for MCP targeting),
    optional selection_text, optional system_prompt.
    Outputs: events pushed to queue — ("chunk", text), ("thinking", text),
    ("status", text), ("stream_done", response), ("tool_done", ...),
    ("final_done", text), ("error", exc), ("approval_required", ...).
    """

    backend_id = "builtin"
    display_name = "Built-in"

    def is_available(self, ctx):
        """Return True if this backend can be used (e.g. CLI installed, config valid)."""
        return True

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
        """Run the agent; push events to queue. Block until done or stopped.

        Called from a worker thread. stop_checker() should return True when user pressed Stop.
        """
        raise NotImplementedError

    def stop(self):
        """Interrupt the current run (e.g. kill subprocess). No-op if not running."""
        pass

    def submit_approval(self, request_id, approved):
        """Submit HITL result so the agent can continue. Default no-op."""
        pass
