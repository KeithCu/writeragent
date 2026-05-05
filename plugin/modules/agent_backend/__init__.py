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
"""Pluggable agent backends for Chat with Document (Aider, Hermes)."""

from plugin.modules.agent_backend.registry import AGENT_BACKEND_REGISTRY, list_backend_ids, get_backend
from plugin.modules.agent_backend.base import AgentBackend

__all__ = ["AGENT_BACKEND_REGISTRY", "list_backend_ids", "get_backend", "AgentBackend"]
