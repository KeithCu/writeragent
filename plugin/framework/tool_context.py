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
"""Per-invocation context passed to every tool execution."""


class ToolContext:
    """Immutable-ish context for a single tool invocation.

    Attributes:
        doc:       UNO document model.
        ctx:       UNO component context.
        doc_type:  Detected document type ("writer", "calc", "draw").
        services:  ServiceRegistry — access to all services.
        caller:    Who triggered the call ("chatbot", "mcp", "menu").
        status_callback: Optional callback for status updates (Writer tools).
        append_thinking_callback: Optional callback for thinking text (Writer tools).
        stop_checker: Optional callable () -> bool; if present and returns True, tool should stop.
        approval_callback: Optional callable for human-in-the-loop approval.
        chat_append_callback: Optional callable(str) to append plain text to the chat response.
    """

    __slots__ = ("doc", "ctx", "doc_type", "services", "caller", "status_callback", "append_thinking_callback", "stop_checker", "approval_callback", "chat_append_callback", "set_active_domain_callback")

    def __init__(self, doc, ctx, doc_type, services, caller="", status_callback=None, append_thinking_callback=None, stop_checker=None, approval_callback=None, chat_append_callback=None, set_active_domain_callback=None):
        self.doc = doc
        self.ctx = ctx
        self.doc_type = doc_type
        self.services = services
        self.caller = caller
        self.status_callback = status_callback
        self.append_thinking_callback = append_thinking_callback
        self.stop_checker = stop_checker
        self.approval_callback = approval_callback
        self.chat_append_callback = chat_append_callback
        self.set_active_domain_callback = set_active_domain_callback
