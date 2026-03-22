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
"""Built-in backend: no-op. Sidebar uses the existing in-process LlmClient path."""

from plugin.framework.errors import format_error_payload
from plugin.modules.agent_backend.base import AgentBackend


class BuiltinBackend(AgentBackend):
    backend_id = "builtin"
    display_name = "Built-in"

    def __init__(self, ctx=None):
        pass

    def send(self, queue, user_message, document_context, document_url, **kwargs):
        # Should not be called; sidebar branches away when backend is builtin.
        queue.put(("error", format_error_payload(RuntimeError("Built-in backend should not receive send()"))))
