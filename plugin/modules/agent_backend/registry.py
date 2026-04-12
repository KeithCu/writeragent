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
"""Registry of agent backends. Backend ids: builtin, aider, hermes."""

from plugin.modules.agent_backend.builtin import BuiltinBackend
from plugin.modules.agent_backend.hermes_simple import HermesBackend
from plugin.modules.agent_backend.claude_simple import ClaudeBackend
from plugin.modules.agent_backend.vibe_simple import VibeBackend

AGENT_BACKEND_REGISTRY = {
    "builtin": ("Built-in", BuiltinBackend),
    # "aider": ("Aider", AiderBackend),
    "hermes": ("Hermes", HermesBackend),
    "claude": ("Claude Code (ACP)", ClaudeBackend),
    "vibe": ("Mistral Vibe (ACP)", VibeBackend),
    # "openhands": ("OpenHands", OpenHandsBackend),
    # "opencode": ("OpenCode", OpenCodeBackend),
}


def list_backend_ids():
    """Return list of registered backend ids."""
    return list(AGENT_BACKEND_REGISTRY.keys())


def normalize_backend_id(backend_id):
    """Normalize backward-compatible or translated backend IDs to internal IDs."""
    if not backend_id:
        return "builtin"

    b_id = str(backend_id).strip().lower()

    # Official internal IDs
    if b_id in AGENT_BACKEND_REGISTRY:
        return b_id

    # Default to builtin if not found, recovering from any other corrupted string
    return "builtin"


def get_backend(backend_id, ctx=None):
    """Return an adapter instance for the given backend id, or None."""
    backend_id = normalize_backend_id(backend_id)
    entry = AGENT_BACKEND_REGISTRY.get(backend_id)
    if not entry:
        return None
    _name, cls = entry
    return cls(ctx=ctx)
