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
"""Abstract base for agent backends. All adapters push events into a queue.Queue."""



class AgentBackend:
    """Contract for pluggable agent backends (Aider, Hermes, etc.).

    Inputs: user_message, document_context, document_url (for MCP targeting),
    optional selection_text, optional system_prompt.
    Outputs: events pushed to queue — first element is :class:`~plugin.framework.async_stream.StreamQueueKind`
    (e.g. ``CHUNK``, ``STATUS``, ``STREAM_DONE``, ``TOOL_CALL``, ``TOOL_RESULT``, ``ERROR``, …).
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
        mcp_url=None,
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
