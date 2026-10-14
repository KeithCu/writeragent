# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Helpers for Python modules loaded directly by LibreOffice as UNO components.

When LibreOffice loads a .py file registered as a UNO service (XProofreader,
XJob, etc.), it often does so with a very limited sys.path. Code in those files
must manually ensure the extension root is on sys.path so that normal
`import plugin.xxx` statements work.

This module centralizes the fragile path-walking logic that was previously
duplicated (with slight variations) in several places.
"""

import logging
import os
import sys
import types
import importlib
import importlib.util
import importlib.machinery
from typing import Any


import importlib.abc

_WRITERAGENT_API = "plugin.scripting.writeragent_api"
_WRITERAGENT_NAMESPACE = "plugin.scripting.writeragent_namespace"

_ABSENT = object()
_SAVED_ATTRS_KEY = "_writeragent_alias_saved_attrs"
_ATTRS_TO_PRESERVE = (
    "__spec__",
    "__loader__",
    "__name__",
    "__package__",
    "__file__",
    "__cached__",
    "__path__",
)


class _WriteragentNamespaceLoader(importlib.abc.Loader):
    """Empty namespace package when the full tool-proxy API is not bundled (LibrePy)."""

    def create_module(self, spec: Any) -> Any:
        mod = types.ModuleType("writeragent")
        mod.__package__ = "writeragent"
        mod.__path__ = []
        return mod

    def exec_module(self, module: Any) -> None:
        pass


class AliasLoader(importlib.abc.Loader):
    """Loader that returns the already loaded or newly imported real module."""

    real_name: str
    _alias_name: str | None
    _saved_attrs: dict[str, Any] | None

    def __init__(self, real_name: str, alias_name: str | None = None) -> None:
        self.real_name = real_name
        self._alias_name = alias_name
        self._saved_attrs = None

    def create_module(self, spec: Any) -> Any:
        if spec is not None and hasattr(spec, "name") and not self._alias_name:
            self._alias_name = spec.name
        mod = importlib.import_module(self.real_name)
        existing_saved = getattr(mod, _SAVED_ATTRS_KEY, None)
        if isinstance(existing_saved, dict):
            saved = dict(existing_saved)
        else:
            saved = {attr: getattr(mod, attr, _ABSENT) for attr in _ATTRS_TO_PRESERVE}
            try:
                setattr(mod, _SAVED_ATTRS_KEY, saved)
            except Exception:
                pass
        self._saved_attrs = saved
        return mod

    def exec_module(self, module: Any) -> None:
        saved = self._saved_attrs
        if saved is None:
            saved = getattr(module, _SAVED_ATTRS_KEY, None)
        if isinstance(saved, dict):
            try:
                for attr, val in saved.items():
                    if attr == "__path__" and self._alias_name == "writeragent":
                        # Root writeragent namespace package must retain __path__ so child modules
                        # (e.g. writeragent.scripting, writeragent.vision) can be resolved.
                        module.__path__ = []
                        continue
                    if val is _ABSENT:
                        if hasattr(module, attr):
                            try:
                                delattr(module, attr)
                            except AttributeError:
                                pass
                    else:
                        setattr(module, attr, val)
            except Exception:
                logging.getLogger(__name__).debug(
                    "Failed to restore module attributes on %s", self.real_name, exc_info=True
                )
            finally:
                if hasattr(module, _SAVED_ATTRS_KEY):
                    try:
                        delattr(module, _SAVED_ATTRS_KEY)
                    except AttributeError:
                        pass


def _log_find_spec_failure(fullname: str, exc: BaseException) -> None:
    logging.getLogger(__name__).debug("AliasImporter.find_spec failed for %s", fullname, exc_info=True)


def _writeragent_namespace_spec(fullname: str = "writeragent") -> Any:
    # LibrePy excludes writeragent_api; fall back to namespace stub or in-memory package.
    try:
        real_spec = importlib.util.find_spec(_WRITERAGENT_NAMESPACE)
    except Exception as exc:
        _log_find_spec_failure(_WRITERAGENT_NAMESPACE, exc)
        real_spec = None
    if real_spec is not None:
        try:
            spec = importlib.machinery.ModuleSpec(fullname, AliasLoader(_WRITERAGENT_NAMESPACE, fullname))
            spec.submodule_search_locations = []
            return spec
        except Exception as exc:
            _log_find_spec_failure(fullname, exc)
            return None
    try:
        spec = importlib.machinery.ModuleSpec(fullname, _WriteragentNamespaceLoader())
        spec.submodule_search_locations = []
        return spec
    except Exception as exc:
        _log_find_spec_failure(fullname, exc)
        return None


class AliasImporter:
    """Import hook to dynamically map 'writeragent' and 'writeragent.*' imports to 'plugin' equivalents."""

    def find_spec(self, fullname: str, path: Any = None, target: Any = None) -> Any:
        if fullname != "writeragent" and not fullname.startswith("writeragent."):
            return None
        # The venv checker treats writeragent.* as "any submodule" when that
        # pattern is on the list. This hook then loaded the matching plugin
        # module, including framework.config and LlmClient. Refuse unless the
        # same allowlist accepts the plugin target or an explicit alias entry.
        from plugin.scripting.sandbox import VENV_AUTHORIZED_IMPORTS, import_authorized
        if not import_authorized(fullname, VENV_AUTHORIZED_IMPORTS):
            # Import walks parent packages before submodules
            # ('writeragent.scripting' before 'writeragent.scripting.analysis').
            # import_authorized rejects those intermediate names, so a parent
            # of an authorized module still gets a package spec. The parent
            # itself stays blocked; only the walk through it succeeds.
            prefix = fullname + "."
            real_prefix = (
                fullname.replace("writeragent", "plugin", 1) + "."
                if fullname.startswith("writeragent.")
                else None
            )
            has_authorized_child = any(
                (entry != "writeragent.*" and (entry.startswith(prefix) or (real_prefix is not None and entry.startswith(real_prefix))))
                for entry in VENV_AUTHORIZED_IMPORTS
            )
            if not has_authorized_child:
                return None
        if fullname == "writeragent":
            real_name = _WRITERAGENT_API
        else:
            real_name = fullname.replace("writeragent", "plugin", 1)
        try:
            real_spec = importlib.util.find_spec(real_name)
        except Exception as exc:
            _log_find_spec_failure(fullname, exc)
            return None
        if real_spec is None and fullname == "writeragent":
            return _writeragent_namespace_spec(fullname)
        if real_spec is None:
            return None
        try:
            spec = importlib.machinery.ModuleSpec(fullname, AliasLoader(real_name, fullname))
            if fullname == "writeragent":
                spec.submodule_search_locations = []
            elif real_spec.submodule_search_locations is not None:
                spec.submodule_search_locations = list(real_spec.submodule_search_locations)
            return spec
        except Exception as exc:
            _log_find_spec_failure(fullname, exc)
            return None


def register_alias_importer() -> None:
    """Register the AliasImporter hook at the start of sys.meta_path if not already registered."""
    for finder in sys.meta_path:
        if finder.__class__.__name__ == "AliasImporter":
            return
    sys.meta_path.insert(0, AliasImporter())


_stdio_utf8_done = False


def ensure_utf8_stdio() -> None:
    """Reconfigure stdout/stderr to UTF-8 once (macOS GUI LibreOffice often uses ASCII)."""
    global _stdio_utf8_done
    if _stdio_utf8_done:
        return
    _stdio_utf8_done = True
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass


def ensure_plugin_on_path(module_file: str, levels_up: int = 3, also_add_lib: bool = False, also_add_contrib: bool = False, also_add_plugin_dir: bool = False, also_add_vendor: bool = False) -> str:
    """
    Walk up from the caller's __file__ and add the extension root on sys.path.

    This must be called **very early** (ideally as one of the first statements
    in the module, before any `from plugin.xxx import ...`) in any file that
    can be loaded directly by LibreOffice as a UNO component (XJob, XProofreader,
    Calc add-ins, etc.).

    Why this exists:
        unopkg / LibreOffice's Python UNO loader does not reliably put the
        extension root on sys.path for standalone .py components. Without this,
        "import plugin.foo" fails with ModuleNotFoundError.

    Common usage patterns:
        - Most plugin/ submodules: levels_up=3
        - Deeply nested components (e.g. writer/locale/): levels_up=4
        - Main entry points that need vendor/lib paths: also_add_vendor=True,
          also_add_lib=True, also_add_plugin_dir=True

    Args:
        module_file: Pass the module's own __file__ (the standard pattern).
        levels_up: Directory levels to ascend to reach the extension root.
                   3 is correct for most files under plugin/<something>/.
                   4 is required for some deeply nested standalone components
                   (e.g. the AI grammar proofreader under writer/locale/).
        also_add_lib: Add <ext_root>/plugin/lib (vendored pure-Python packages
                      used in the packaged OXT).
        also_add_contrib: Add <ext_root>/plugin/contrib (rarely needed for
                          early bootstrap; prefer runtime resolution when possible).
        also_add_plugin_dir: Add the plugin/ directory itself (useful for some
                             entry points that import things relative to it).
        also_add_vendor: Add the root-level vendor/ directory
                         (<ext_root>/vendor). Used in development for cross-platform
                         wheels (sounddevice, cffi, etc.).

    Returns:
        The extension root path that is now on sys.path.
    """
    register_alias_importer()
    this_file = os.path.abspath(module_file)
    for _unused in range(levels_up):
        this_file = os.path.dirname(this_file)
    ext_root = this_file

    # Each insert(0, ...) runs only when that directory exists and is not already on
    # sys.path. Call order is extension root, then plugin/, plugin/lib, plugin/contrib,
    # vendor/. Each insert puts the new entry in front, so the search order is vendor,
    # contrib, lib, plugin/, root. plugin/lib is in front of the stdlib when
    # also_add_lib=True because the OXT ships pure-Python packages there.
    # also_add_plugin_dir=True also makes top-level names such as doc, calc, and
    # framework importable. Do not reorder these inserts.
    if ext_root not in sys.path:
        sys.path.insert(0, ext_root)

    if also_add_plugin_dir:
        plugin_dir = os.path.join(ext_root, "plugin")
        if os.path.isdir(plugin_dir) and plugin_dir not in sys.path:
            sys.path.insert(0, plugin_dir)

    if also_add_lib:
        lib_dir = os.path.join(ext_root, "plugin", "lib")
        if os.path.isdir(lib_dir) and lib_dir not in sys.path:
            sys.path.insert(0, lib_dir)

    if also_add_contrib:
        contrib_dir = os.path.join(ext_root, "plugin", "contrib")
        if os.path.isdir(contrib_dir) and contrib_dir not in sys.path:
            sys.path.insert(0, contrib_dir)

    if also_add_vendor:
        vendor_dir = os.path.join(ext_root, "vendor")
        if os.path.isdir(vendor_dir) and vendor_dir not in sys.path:
            sys.path.insert(0, vendor_dir)

    ensure_utf8_stdio()
    return ext_root
