# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Lazy delegation from public ``plugin.scripting.*`` facades to ``plugin.scripting.venv.*``.

Host facades call :func:`install_lazy_exports` once at import. That installs
PEP 562 ``__getattr__`` and ``__dir__`` and does not import the venv module.
``__dir__`` reads the module dict when it runs, so names assigned after
``install_lazy_exports`` are included. A successful lookup is stored on the
module dict; the next lookup does not call ``__getattr__`` again. An unknown
name raises ``AttributeError`` using the facade ``__name__``. A listed name
that the venv module does not define propagates that module's ``AttributeError``
and is not stored.
"""

from __future__ import annotations

import importlib
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Mapping


def venv_attr(venv_module: str, name: str) -> Any:
    """Load *name* from ``plugin.scripting.venv.<venv_module>`` on demand."""
    mod = importlib.import_module(f"plugin.scripting.venv.{venv_module}")
    return getattr(mod, name)


def install_lazy_exports(
    module_globals: dict[str, Any],
    venv_module: str,
    exports: frozenset[str],
    *,
    extra: Mapping[str, frozenset[str]] | None = None,
) -> None:
    """Publish *exports* from ``plugin.scripting.venv.<venv_module>`` on the calling module.

    *extra* maps another venv submodule name to names loaded from that submodule.
    The same name in two sets raises ``ValueError``.
    """
    owners: dict[str, str] = {}
    for name in exports:
        owners[name] = venv_module
    for mod_name, names in (extra or {}).items():
        for name in names:
            previous = owners.get(name)
            if previous is not None:
                raise ValueError(
                    f"lazy export {name!r} listed for both {previous!r} and {mod_name!r}"
                )
            owners[name] = mod_name

    lazy_names = frozenset(owners)

    def __getattr__(name: str) -> Any:
        owner = owners.get(name)
        if owner is None:
            raise AttributeError(
                f"module {module_globals['__name__']!r} has no attribute {name!r}"
            )
        value = venv_attr(owner, name)
        # PEP 562: a stored name is served by normal lookup and skips __getattr__.
        module_globals[name] = value
        return value

    def __dir__() -> list[str]:
        return sorted(set(module_globals) | lazy_names)

    module_globals["__getattr__"] = __getattr__
    module_globals["__dir__"] = __dir__
