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
"""OpenHands agent backend adapter. Wraps the OpenHands CLI in a long-lived process."""

from plugin.modules.agent_backend.cli_backend import CLIProcessBackend, strip_ansi


class OpenHandsBackend(CLIProcessBackend):
    backend_id = "openhands"
    display_name = "OpenHands"

    def get_default_cmd(self):
        return "openhands"

    def is_ready_prompt(self, line):
        # Depending on how OpenHands exposes its interactive CLI, we watch for its prompt.
        if not line:
            return False
        s = strip_ansi(line).strip()
        # Common pattern: "User> " or "OpenHands> "
        return s.endswith(">") or "Please enter your message" in s

    def is_end_of_response(self, line):
        return self.is_ready_prompt(line)

    def format_input(self, user_message, document_context, document_url, system_prompt, selection_text, mcp_url=None, **kwargs):
        parts = []
        if system_prompt:
            parts.append("System Instructions:\n")
            parts.append(system_prompt)
            parts.append("\n\n")
        if document_url:
            parts.append(f"Current Document URL: {document_url}\n")
        if mcp_url:
            parts.append(f"WriterAgent MCP Server: {mcp_url}\n")
        if document_context:
            parts.append("\nExcerpt of document context (for quick reference):\n")
            parts.append(document_context)
            parts.append("\n\n")
        parts.append(user_message)
        parts.append("\n")
        return "".join(parts)
