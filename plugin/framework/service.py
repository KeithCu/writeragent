# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2024 John Balis
# Copyright (c) 2026 KeithCu (modifications and relicensing)
# Copyright (c) 2025-2026 quazardous (config, registries, build system)
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
"""Service infrastructure: base class and registry.

Concurrency: ``ServiceRegistry`` (``services.document``, ``services.events``,
…) is populated while the extension bootstraps on the UI thread, then
read for the rest of the session. There is no lock. Do not call
``register`` from a background worker — you can race the dict and you
will confuse shutdown order.
"""

from __future__ import annotations

from abc import ABC
from dataclasses import dataclass
from typing import Any, Generic, List, Protocol, TypeVar, cast


# ── FSM State Markers ─────────────────────────────────────────────


@dataclass(frozen=True)
class BaseState:
    """Marker base for immutable FSM state. Subclasses add domain fields."""


class Effect(Protocol):
    """Structural marker for side-effect descriptions (interpreted outside FSM)."""


StateT = TypeVar("StateT", bound=BaseState)


@dataclass(frozen=True)
class FsmTransition(Generic[StateT]):
    """Result of a pure transition: successor state and effects to run."""

    state: StateT
    effects: List[Any]


# ── Service Infrastructure ─────────────────────────────────────────


class ServiceBase(ABC):
    """Abstract base for services registered in the ServiceRegistry.

    Services provide horizontal capabilities (document manipulation,
    config access, LLM streaming, etc.) that modules and tools consume.

    Attributes:
        name: Unique service identifier (e.g. "document", "config").
    """

    name: str | None = None

    def initialize(self, ctx: Any) -> None:
        """Called once during bootstrap with the UNO component context.

        Override to perform setup that requires UNO (desktop access,
        service manager, etc.).

        Args:
            ctx: UNO component context (com.sun.star.uno.XComponentContext).
        """

    def shutdown(self):
        """Called on extension unload. Override to clean up."""


class ServiceRegistry:
    """Registry that holds all services and provides attribute access.

    Usage::

        services = ServiceRegistry()
        services.register(my_document_service)
        services.register(my_config_service)

        # Access by name:
        services.document.build_heading_tree(doc)
        services.config.get("mcp.port")

        # Or explicit:
        services.get("document")
    """

    def __init__(self) -> None:
        self._services: dict[str, Any] = {}

    def register(self, name: Any, instance: Any) -> None:
        """Register an arbitrary object as a named service."""
        if name in self._services:
            raise ValueError(f"Service already registered: {name}")
        self._services[name] = instance

    def auto_discover(self, module: Any) -> None:
        """Automatically discover and register ServiceBase subclasses in a module."""
        import inspect
        import logging

        log = logging.getLogger("writeragent.services")

        for _name, obj in inspect.getmembers(module, inspect.isclass):
            if issubclass(obj, ServiceBase) and obj is not ServiceBase and obj.__module__ == module.__name__ and not inspect.isabstract(obj) and getattr(obj, "name", None):
                try:
                    # Contract: override __init__ → __init__(self, registry); else no-arg.
                    # Do not use inspect.signature (UNO/C types). TypeError is logged and skipped.
                    if obj.__init__ is not object.__init__:
                        svc_instance = cast("Any", obj)(self)
                    else:
                        svc_instance = obj()
                    self.register(obj.name, svc_instance)
                except (TypeError, ValueError, ImportError):
                    log.exception("Failed to instantiate service %s (TypeError/ValueError/ImportError)", obj.__name__)
                except Exception:
                    log.exception("Failed to instantiate service %s (unexpected)", obj.__name__)

    def get(self, name: str) -> Any:
        """Get a service by name, or None if not registered."""
        return self._services.get(name)

    def __getattr__(self, name: str) -> Any:
        if name.startswith("_"):
            raise AttributeError(name)
        if name in self._services:
            return self._services[name]
        raise AttributeError(f"No service registered: {name}")

    def __contains__(self, name: str) -> bool:
        return name in self._services

    def initialize_all(self, ctx: Any) -> None:
        """Call ``initialize(ctx)`` on every service that supports it.

        ``register()`` accepts arbitrary objects, not only ServiceBase, so
        getattr+callable is required (ServiceBase already defines no-op methods).
        """
        for svc in self._services.values():
            init = getattr(svc, "initialize", None)
            if callable(init):
                init(ctx)

    def shutdown_all(self):
        """Call ``shutdown()`` on every service that supports it.

        Same getattr guard as initialize_all: non-ServiceBase registrations.
        """
        for name, svc in self._services.items():
            shutdown = getattr(svc, "shutdown", None)
            if callable(shutdown):
                try:
                    shutdown()
                except Exception as e:
                    # Generic catch is somewhat acceptable during global teardown to ensure other services
                    # still get their shutdown called, but we must log it so we aren't swallowing shutdown errors silently.
                    import logging

                    logging.getLogger(__name__).error("Service %s failed during shutdown: %s", name, e)

    @property
    def service_names(self):
        return list(self._services.keys())
