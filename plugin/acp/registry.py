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

from dataclasses import dataclass
import logging
from typing import TYPE_CHECKING, Any, Mapping

if TYPE_CHECKING:
    from plugin.acp.base import AgentBackend

from plugin.acp.acp_backend import ACPBackend
from plugin.acp.builtin import BuiltinBackend

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class BackendSpec:
    backend_id: str
    display_name: str
    binary_name: str
    default_extra_args: tuple[str, ...]
    api_key_env_by_provider: Mapping[str, str]
    binary_prefixes: tuple[str, ...]
    class_name: str


_BACKEND_SPECS: tuple[BackendSpec, ...] = (
    BackendSpec(
        backend_id="hermes",
        display_name="Hermes",
        binary_name="hermes",
        # hermes with no subcommand is interactive; hermes acp is the stdio CLI.
        default_extra_args=("acp",),
        # The external CLI consumes the app key only for OpenRouter and OpenAI; other providers use Hermes's own auth.
        api_key_env_by_provider={"openrouter": "OPENROUTER_API_KEY", "openai": "OPENAI_API_KEY"},
        binary_prefixes=(),
        class_name="HermesBackend",
    ),
    BackendSpec(
        backend_id="claude",
        display_name="Claude Code",
        binary_name="claude-code-acp-rs",
        default_extra_args=(),
        # An OpenAI (or any non-Anthropic) key must not be exported as ANTHROPIC_API_KEY.
        api_key_env_by_provider={"anthropic": "ANTHROPIC_API_KEY"},
        binary_prefixes=(),
        class_name="ClaudeBackend",
    ),
    BackendSpec(
        backend_id="vibe",
        display_name="Mistral Vibe",
        binary_name="vibe-acp",
        # Empty default_extra_args. contentBlocks on the prompt result are drained by base send().
        default_extra_args=(),
        api_key_env_by_provider={"mistral": "MISTRAL_API_KEY"},
        binary_prefixes=(),
        class_name="VibeBackend",
    ),
    BackendSpec(
        backend_id="grok",
        display_name="Grok Build",
        binary_name="grok",
        default_extra_args=("--no-auto-update", "agent", "stdio"),
        api_key_env_by_provider={},
        # Official names grok, grok-cli, and grok.exe share those args. Auth is ~/.grok, so env map stays empty.
        binary_prefixes=("grok",),
        class_name="GrokBackend",
    ),
    BackendSpec(
        backend_id="opencode",
        display_name="OpenCode",
        binary_name="opencode",
        # Empty env map; auth is the OpenCode login file. ("acp",) is the stdio subcommand.
        default_extra_args=("acp",),
        api_key_env_by_provider={},
        binary_prefixes=(),
        class_name="OpenCodeBackend",
    ),
)

for spec in _BACKEND_SPECS:
    cls = type(
        spec.class_name,
        (ACPBackend,),
        {
            "backend_id": spec.backend_id,
            "display_name": spec.display_name,
            "binary_name": spec.binary_name,
            "default_extra_args": spec.default_extra_args,
            "api_key_env_by_provider": spec.api_key_env_by_provider,
            "binary_prefixes": spec.binary_prefixes,
            "__module__": "plugin.acp.registry",
        },
    )
    globals()[spec.class_name] = cls

HermesBackend: type[ACPBackend] = globals()["HermesBackend"]
ClaudeBackend: type[ACPBackend] = globals()["ClaudeBackend"]
VibeBackend: type[ACPBackend] = globals()["VibeBackend"]
GrokBackend: type[ACPBackend] = globals()["GrokBackend"]
OpenCodeBackend: type[ACPBackend] = globals()["OpenCodeBackend"]

AGENT_BACKEND_REGISTRY: dict[str, type[AgentBackend]] = {
    "builtin": BuiltinBackend,
    **{spec.backend_id: globals()[spec.class_name] for spec in _BACKEND_SPECS},
}


def normalize_backend_id(backend_id: Any) -> str:
    """Normalize backend ID: strip, lowercase, or fall back to builtin with a warning.

    Empty becomes builtin. A non-empty id that is not in the registry is
    still builtin, and is logged.
    """
    if not backend_id:
        return "builtin"

    b_id = str(backend_id).strip().lower()
    if not b_id:
        return "builtin"

    if b_id in AGENT_BACKEND_REGISTRY:
        return b_id

    # A typo or a stale id used to become the built-in agent with no log,
    # so the sidebar ran the wrong backend and the user was not told.
    log.warning("Unknown agent backend id %r; using builtin", b_id)
    return "builtin"


def get_backend(backend_id: Any) -> AgentBackend:
    """Return an adapter instance for the given backend id."""
    backend_id = normalize_backend_id(backend_id)
    # normalize_backend_id guarantees cls is never None.
    cls = AGENT_BACKEND_REGISTRY[backend_id]
    return cls()
