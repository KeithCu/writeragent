# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Topological dependency sort for plugin modules."""

from __future__ import annotations

from typing import Any

from plugin.framework.errors import ConfigError


def topo_sort(modules: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Sort modules based on their required services and dependencies.

    Ensures dependencies are ordered before the modules that require them.
    Core is prioritized first when present. Tie-breaks follow input order.
    """
    by_name: dict[str, dict[str, Any]] = {m["name"]: m for m in modules}
    provides: dict[str, str] = {}
    for m in modules:
        for svc in m.get("provides_services", []):
            provides[svc] = m["name"]

    visited: set[str] = set()
    visiting: set[str] = set()
    order: list[dict[str, Any]] = []

    def visit(name: str) -> None:
        if name in visited:
            return
        if name in visiting:
            raise ConfigError(f"Cyclic module requires graph at {name!r}", code="MODULE_CYCLE")
        visiting.add(name)
        m = by_name.get(name)
        if m is None:
            visiting.discard(name)
            return
        for req in m.get("requires", []):
            provider = provides.get(req, req)
            if provider in by_name:
                visit(provider)
        visiting.discard(name)
        visited.add(name)
        order.append(m)

    if "core" in by_name:
        visit("core")
    for name in by_name:
        visit(name)
    return order
