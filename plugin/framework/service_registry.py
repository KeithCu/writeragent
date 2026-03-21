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
"""Dependency injection container for services."""


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

    def __init__(self):
        self._services = {}

    def register(self, name, instance):
        """Register an arbitrary object as a named service."""
        if name in self._services:
            raise ValueError(f"Service already registered: {name}")
        self._services[name] = instance

    def auto_discover(self, module):
        """Automatically discover and register ServiceBase subclasses in a module."""
        import inspect
        import logging
        from plugin.framework.service_base import ServiceBase

        log = logging.getLogger("writeragent.services")

        for name, obj in inspect.getmembers(module, inspect.isclass):
            if (issubclass(obj, ServiceBase) and
                obj is not ServiceBase and
                obj.__module__ == module.__name__ and
                not inspect.isabstract(obj) and
                getattr(obj, "name", None)):

                try:
                    # Instantiate by passing the registry itself as 'services'
                    svc_instance = obj(self)
                    self.register(obj.name, svc_instance)
                except Exception as e:
                    log.error("Failed to instantiate service %s: %s", obj.__name__, e)

    def get(self, name):
        """Get a service by name, or None if not registered."""
        return self._services.get(name)

    def __getattr__(self, name):
        if name.startswith("_"):
            raise AttributeError(name)
        if name in self._services:
            return self._services[name]
        raise AttributeError(f"No service registered: {name}")

    def __contains__(self, name):
        return name in self._services

    def initialize_all(self, ctx):
        """Call ``initialize(ctx)`` on every service that supports it."""
        for svc in self._services.values():
            init = getattr(svc, "initialize", None)
            if callable(init):
                init(ctx)

    def shutdown_all(self):
        """Call ``shutdown()`` on every service that supports it."""
        for svc in self._services.values():
            shutdown = getattr(svc, "shutdown", None)
            if callable(shutdown):
                try:
                    shutdown()
                except Exception:
                    pass

    @property
    def service_names(self):
        return list(self._services.keys())
