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
"""Base class for all modules and manifest-driven module loading."""

from __future__ import annotations

import logging
import os
from abc import ABC
from typing import Any, cast

from plugin.framework.utils import get_plugin_dir

log = logging.getLogger("writeragent.module_base")


class ModuleBase(ABC):
    """Base class for all WriterAgent modules.

    Modules declare their manifest in module.yaml (config, requires,
    provides_services). This class handles the runtime behavior:
    initialization, event wiring, and shutdown.

    The ``name`` attribute is set automatically from _manifest.py at load
    time — it does NOT need to be set in the subclass.
    """

    name: str | None = None

    def initialize(self, services):
        """Phase 1: Called in dependency order during bootstrap.

        Use this to register services, wire event subscriptions, and
        create internal objects. All core services are available.

        Args:
            services: ServiceRegistry with attribute access to all
                      registered services (services.config, services.events …).
        """

    def start(self, services):
        """Phase 2a: Called on the VCL main thread after ALL modules
        have initialized.

        Safe for UNO operations: document listeners, UI setup, toolkit
        calls. Dispatched via QueueExecutor.execute() (blocking).
        Called in dependency order.

        Args:
            services: ServiceRegistry with attribute access to all
                      registered services.
        """

    def start_background(self, services):
        """Phase 2b: Called on the Job thread after all start() complete.

        Launch background tasks: HTTP servers, LLM connections, polling.
        Called in dependency order.

        Args:
            services: ServiceRegistry with attribute access to all
                      registered services.
        """

    def shutdown(self):
        """Stop background tasks, close connections.

        Called in reverse dependency order on extension unload."""

    # ── Action dispatch ──────────────────────────────────────────────

    def on_action(self, action):
        """Handle an action dispatched from menu/shortcut. Override in subclass."""
        log.warning("Unhandled action '%s' on module '%s'", action, self.name)

    def get_menu_text(self, action) -> str | None:
        """Return dynamic menu text for an action, or None for default.

        Override in subclass to provide state-dependent menu labels.
        Return None to keep the static title from module.yaml.
        """
        return None

    def get_menu_icon(self, action) -> str | None:
        """Return dynamic icon name prefix for an action, or None for default.

        Override in subclass to provide state-dependent menu icons.
        Return an icon prefix like "running", "stopped", "starting".
        The framework will load ``{prefix}_16.png`` from ``extension/icons/``.
        Return None to keep the icon declared in module.yaml.
        """
        return None

    # ── Dialog helpers ───────────────────────────────────────────────

    def load_dialog(self, dialog_name):
        """Load an XDL dialog from this module's directory."""
        from plugin.modules.chatbot.dialogs import load_module_dialog

        return load_module_dialog(self.name, dialog_name)

    def load_framework_dialog(self, dialog_name):
        """Load a reusable framework dialog template."""
        from plugin.modules.chatbot.dialogs import load_framework_dialog

        return load_framework_dialog(dialog_name)


class ModuleLoader:
    """
    Handles discovery, topological sorting, and initialization of plugin modules.
    """

    @staticmethod
    def load_manifest() -> list[dict[str, Any]]:
        """Loads the module manifest."""
        try:
            from plugin._manifest import MODULES

            return cast("list[dict[str, Any]]", MODULES)
        except ImportError as e:
            raise RuntimeError("plugin._manifest is missing or invalid (gitignored; run `make manifest` or `python3 scripts/generate_manifest.py`).") from e

    @staticmethod
    def topo_sort(modules: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """
        Sorts modules based on their required services.
        Ensures dependencies are initialized before the modules that require them.
        """
        by_name = {m["name"]: m for m in modules}
        provides = {}
        for m in modules:
            for svc in m.get("provides_services", []):
                provides[svc] = m["name"]

        visited = set()
        order = []

        def visit(name):
            if name in visited:
                return
            visited.add(name)
            m = by_name.get(name)
            if m is None:
                return
            for req in m.get("requires", []):
                provider = provides.get(req, req)
                if provider in by_name:
                    visit(provider)
            order.append(m)

        if "core" in by_name:
            visit("core")
        for name in by_name:
            visit(name)
        return order

    @classmethod
    def load_modules(cls, services_registry) -> list[ModuleBase]:
        """
        Discovers, imports, and initializes modules based on the manifest.
        Returns a list of initialized module instances.
        """
        initialized_modules: list[ModuleBase] = []
        manifests = cls.topo_sort(cls.load_manifest())

        for manifest in manifests:
            name = manifest["name"]
            if name == "core":
                continue

            # Try nested path first (e.g. "launcher/providers/claude")
            rel_path = name.replace(".", os.sep)
            module_dir = os.path.join(get_plugin_dir(), "modules", rel_path)
            import_path = "plugin.modules." + name

            if not os.path.isdir(module_dir):
                # Legacy fallback for flat paths (e.g. "launcher_claude")
                dir_name = name.replace(".", "_")
                module_dir = os.path.join(get_plugin_dir(), "modules", dir_name)
                import_path = "plugin.modules." + dir_name
                if not os.path.isdir(module_dir):
                    continue

            # Dynamic ModuleBase initialization
            try:
                import importlib
                import inspect

                mod_pkg = importlib.import_module(import_path)
                module_class = None

                # Look for a class subclassing ModuleBase by checking MRO names (avoids LO sys.path duplicate issues)
                for attr_name in dir(mod_pkg):
                    attr = getattr(mod_pkg, attr_name)
                    if inspect.isclass(attr) and getattr(attr, "__name__", "") != "ModuleBase":
                        if any(getattr(b, "__name__", "") == "ModuleBase" for b in getattr(attr, "__mro__", [])):
                            module_class = attr
                            break

                if module_class:
                    mod = module_class()
                    # MRO scan above only selects ModuleBase subclasses; cast is for the type checker
                    # (dynamic import cannot prove subclass to static analysis).
                    mod = cast("ModuleBase", mod)
                    mod.name = name
                    mod.initialize(services_registry)
                    initialized_modules.append(mod)
            except Exception as e:
                logging.getLogger("writeragent").warning("Failed to load module %s: %s", name, e)

        return initialized_modules
