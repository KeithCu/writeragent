"""Pluggable agent backends for Chat with Document (Aider, Hermes)."""

from plugin.modules.agent_backend.registry import (
    AGENT_BACKEND_REGISTRY,
    list_backend_ids,
    get_backend,
)
from plugin.modules.agent_backend.base import AgentBackend

__all__ = [
    "AGENT_BACKEND_REGISTRY",
    "list_backend_ids",
    "get_backend",
    "AgentBackend",
]
