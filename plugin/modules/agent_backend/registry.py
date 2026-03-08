"""Registry of agent backends. Backend ids: builtin, aider, hermes."""

from plugin.modules.agent_backend.builtin import BuiltinBackend
from plugin.modules.agent_backend.aider_proxy import AiderBackend
from plugin.modules.agent_backend.hermes_proxy import HermesBackend
from plugin.modules.agent_backend.openhands_proxy import OpenHandsBackend
from plugin.modules.agent_backend.opencode_proxy import OpenCodeBackend

AGENT_BACKEND_REGISTRY = {
    "builtin": ("Built-in", BuiltinBackend),
    "aider": ("Aider", AiderBackend),
    "hermes": ("Hermes", HermesBackend),
    "openhands": ("OpenHands", OpenHandsBackend),
    "opencode": ("OpenCode", OpenCodeBackend),
}


def list_backend_ids():
    """Return list of registered backend ids."""
    return list(AGENT_BACKEND_REGISTRY.keys())


def get_backend(backend_id, ctx=None):
    """Return an adapter instance for the given backend id, or None."""
    entry = AGENT_BACKEND_REGISTRY.get(backend_id)
    if not entry:
        return None
    _name, cls = entry
    return cls(ctx=ctx)
