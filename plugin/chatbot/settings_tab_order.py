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
"""Deterministic Settings dialog tab order and field sources (XDL generation + runtime)."""

from __future__ import annotations

from typing import Any, Iterator

# User-facing tab strip after General and Image (pages 1–2).
# Speech is the first module tab so it sits immediately after Image.
SETTINGS_TAB_MODULE_ORDER = (
    "audio",
    "doc",
    "chatbot",
    "embeddings",
    "mcp",
    "scripting",
)


def _build_inline_maps(modules: list[dict[str, Any]]) -> tuple[set[str], dict[str, list[tuple[dict[str, Any], dict[str, Any]]]]]:
    """Return inline_set and inline_map matching generate_settings_dialog_tabs."""
    # generate_manifest rejects a non-string target, a self-inline, and a
    # chained config_inline, so every child's target is a real tab module.
    inline_set: set[str] = set()
    inline_map: dict[str, list[tuple[dict[str, Any], dict[str, Any]]]] = {}
    for m in modules:
        target = m.get("config_inline")
        if not target:
            continue
        inline_set.add(m["name"])
        inline_map.setdefault(target, []).append((m, m.get("config", {})))
    return inline_set, inline_map


def _is_settings_tab_module(
    m: dict[str, Any],
    inline_set: set[str],
    inline_map: dict[str, list[tuple[dict[str, Any], dict[str, Any]]]],
) -> bool:
    """True when generate_settings_dialog_tabs would emit a tab for this module."""
    name = m["name"]
    if name in ("main", "core") or name in inline_set:
        return False
    # Standalone dialogs are not Settings pages.
    if m.get("settings_tab") is False or m.get("config_dialog"):
        return False

    config = m.get("config", {})
    children = inline_map.get(name)
    if not config and not children:
        return False
    return True


def iter_settings_tab_modules(modules: list[dict[str, Any]]) -> Iterator[dict[str, Any]]:
    """Yield modules that get Settings tabs, in stable user-facing order."""
    inline_set, inline_map = _build_inline_maps(modules)
    by_name = {m["name"]: m for m in modules}

    seen: set[str] = set()
    for name in SETTINGS_TAB_MODULE_ORDER:
        m = by_name.get(name)
        if m is None or not _is_settings_tab_module(m, inline_set, inline_map):
            continue
        seen.add(name)
        yield m

    extras = sorted(
        name
        for name, m in by_name.items()
        if name not in seen and _is_settings_tab_module(m, inline_set, inline_map)
    )
    for name in extras:
        yield by_name[name]


def iter_settings_field_modules(modules: list[dict[str, Any]]) -> Iterator[dict[str, Any]]:
    """Yield modules providing Settings fields (tab modules and their inline children)."""
    _, inline_map = _build_inline_maps(modules)
    yielded: set[str] = set()
    for m in iter_settings_tab_modules(modules):
        name = m["name"]
        if name not in yielded:
            yielded.add(name)
            yield m
        for child_m, _ in inline_map.get(name, []):
            child_name = child_m["name"]
            if child_name not in yielded:
                yielded.add(child_name)
                yield child_m
