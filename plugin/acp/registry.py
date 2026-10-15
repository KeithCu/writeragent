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
"""Registry of agent backends. Backend ids: builtin, hermes, claude, vibe, grok, opencode."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from plugin.acp.base import AgentBackend

from plugin.acp.builtin import BuiltinBackend
from plugin.acp.hermes_simple import HermesBackend
from plugin.acp.claude_simple import ClaudeBackend
from plugin.acp.vibe_simple import VibeBackend
from plugin.acp.grok_simple import GrokBackend
from plugin.acp.opencode_simple import OpenCodeBackend

# The class display_name is the English label; the old tuple repeated it,
# and ACP subclasses otherwise inherited AgentBackend.display_name ("Built-in").
AGENT_BACKEND_REGISTRY = {
    "builtin": BuiltinBackend,
    "hermes": HermesBackend,
    "claude": ClaudeBackend,
    "vibe": VibeBackend,
    "grok": GrokBackend,
    "opencode": OpenCodeBackend,
}


def list_backend_ids() -> list[str]:
    """Return list of registered backend ids."""
    return list(AGENT_BACKEND_REGISTRY.keys())


def normalize_backend_id(backend_id: Any) -> str:
    """Normalize backward-compatible or translated backend IDs to internal IDs."""
    if not backend_id:
        return "builtin"

    b_id = str(backend_id).strip().lower()

    # Official internal IDs
    if b_id in AGENT_BACKEND_REGISTRY:
        return b_id

    # Default to builtin if not found, recovering from any other corrupted string
    return "builtin"


def get_backend(backend_id: Any, ctx: Any | None = None) -> AgentBackend | None:
    """Return an adapter instance for the given backend id, or None."""
    backend_id = normalize_backend_id(backend_id)
    cls = AGENT_BACKEND_REGISTRY.get(backend_id)
    if cls is None:
        return None
    return cls(ctx=ctx)
