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

import logging
from typing import TYPE_CHECKING, Any, Mapping, final

if TYPE_CHECKING:
    from plugin.acp.base import AgentBackend

from plugin.acp.acp_backend import ACPBackend
from plugin.acp.builtin import BuiltinBackend

log = logging.getLogger(__name__)


@final
class HermesBackend(ACPBackend):
    """``hermes`` with no subcommand is interactive; ``hermes acp`` is the stdio CLI.

    The external CLI consumes the app key only for OpenRouter and OpenAI.
    Other providers use Hermes's own auth.
    """

    backend_id = "hermes"
    display_name = "Hermes"
    binary_name = "hermes"
    default_extra_args = ("acp",)
    api_key_env_by_provider = {
        "openrouter": "OPENROUTER_API_KEY",
        "openai": "OPENAI_API_KEY",
    }


@final
class ClaudeBackend(ACPBackend):
    """An OpenAI key must not be exported as ``ANTHROPIC_API_KEY``."""

    backend_id = "claude"
    display_name = "Claude Code"
    binary_name = "claude-code-acp-rs"
    api_key_env_by_provider = {"anthropic": "ANTHROPIC_API_KEY"}


@final
class VibeBackend(ACPBackend):
    """No default CLI args. ``contentBlocks`` on the prompt result are drained by ``send()``."""

    backend_id = "vibe"
    display_name = "Mistral Vibe"
    binary_name = "vibe-acp"
    api_key_env_by_provider = {"mistral": "MISTRAL_API_KEY"}


@final
class GrokBackend(ACPBackend):
    """``grok``, ``grok-cli``, and ``grok.exe`` share these args. Auth is ``~/.grok``."""

    backend_id = "grok"
    display_name = "Grok Build"
    binary_name = "grok"
    default_extra_args = ("--no-auto-update", "agent", "stdio")
    api_key_env_by_provider: Mapping[str, str] = {}
    binary_prefixes = ("grok",)


@final
class OpenCodeBackend(ACPBackend):
    """Auth is the OpenCode login file. ``acp`` is the stdio subcommand."""

    backend_id = "opencode"
    display_name = "OpenCode"
    binary_name = "opencode"
    default_extra_args = ("acp",)
    api_key_env_by_provider: Mapping[str, str] = {}


_ACP_BACKENDS: tuple[type[ACPBackend], ...] = (
    HermesBackend,
    ClaudeBackend,
    VibeBackend,
    GrokBackend,
    OpenCodeBackend,
)

AGENT_BACKEND_REGISTRY: dict[str, type[AgentBackend]] = {
    "builtin": BuiltinBackend,
    **{cls.backend_id: cls for cls in _ACP_BACKENDS},
}


def normalize_backend_id(backend_id: Any) -> str:
    """Normalize backend ID: strip, lowercase, or fall back to builtin with a warning.

    Empty becomes builtin. A non-empty id that is not in the registry is
    still builtin, and is logged.
    """
    b_id = str(backend_id or "").strip().lower()
    if b_id in AGENT_BACKEND_REGISTRY:
        return b_id
    if b_id:
        log.warning("Unknown agent backend id %r; using builtin", b_id)
    return "builtin"


def get_backend(backend_id: Any) -> AgentBackend:
    """Return an adapter instance for the given backend id."""
    return AGENT_BACKEND_REGISTRY[normalize_backend_id(backend_id)]()
