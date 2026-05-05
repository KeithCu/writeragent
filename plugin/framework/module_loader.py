from __future__ import annotations

import os
import logging
from typing import TYPE_CHECKING, Any, List, cast

from plugin.framework.utils import get_plugin_dir

if TYPE_CHECKING:
    from plugin.framework.module_base import ModuleBase


class ModuleLoader:
    """
    Handles discovery, topological sorting, and initialization of plugin modules.
    """

    @staticmethod
    def load_manifest() -> List[dict[str, Any]]:
        """Loads the module manifest."""
        try:
            from plugin._manifest import MODULES

            return cast("list[dict[str, Any]]", MODULES)
        except ImportError as e:
            raise RuntimeError("plugin._manifest is missing or invalid (gitignored; run `make manifest` or `python3 scripts/generate_manifest.py`).") from e

    @staticmethod
    def topo_sort(modules: List[dict[str, Any]]) -> List[dict[str, Any]]:
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
