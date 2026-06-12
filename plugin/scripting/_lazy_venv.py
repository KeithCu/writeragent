# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Lazy delegation from public ``plugin.scripting.*`` facades to ``plugin.scripting.venv.*``."""

from __future__ import annotations

import importlib
from typing import Any, Callable


def venv_attr(venv_module: str, name: str) -> Any:
    """Load *name* from ``plugin.scripting.venv.<venv_module>`` on demand."""
    mod = importlib.import_module(f"plugin.scripting.venv.{venv_module}")
    return getattr(mod, name)


def make_getattr(
    venv_module: str,
    exports: frozenset[str],
    *,
    fallback: Callable[[str], Any] | None = None,
) -> Callable[[str], Any]:
    """Return a PEP 562 ``__getattr__`` that lazy-loads *exports* from a venv submodule."""

    def __getattr__(name: str) -> Any:
        if name in exports:
            return venv_attr(venv_module, name)
        if fallback is not None:
            return fallback(name)
        raise AttributeError(f"module 'plugin.scripting.{venv_module}' has no attribute {name!r}")

    return __getattr__
