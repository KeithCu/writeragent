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
"""Deterministic Settings dialog tab order (XDL generation + runtime TabListener)."""

from __future__ import annotations

from typing import Any, Iterator

# User-facing tab strip after General and Image Settings (pages 1–2).
SETTINGS_TAB_MODULE_ORDER = (
    "doc",
    "chatbot",
    "embeddings",
    "mcp",
    "scripting",
)


def _build_inline_maps(modules: list[dict[str, Any]]) -> tuple[set[str], dict[str, list[tuple[dict[str, Any], dict[str, Any]]]]]:
    """Return inline_set and inline_map matching generate_settings_dialog_tabs."""
    inline_targets: dict[str, str] = {}
    for m in modules:
        inline_val = m.get("config_inline")
        if not inline_val:
            continue
        target = inline_val if isinstance(inline_val, str) else (m["name"].rsplit(".", 1)[0] if "." in m["name"] else None)
        if target:
            inline_targets[m["name"]] = target

    inline_set: set[str] = set()
    for name, target in inline_targets.items():
        if target not in inline_targets:
            inline_set.add(name)

    by_name = {m["name"]: m for m in modules}
    inline_map: dict[str, list[tuple[dict[str, Any], dict[str, Any]]]] = {}
    for name in inline_set:
        target = inline_targets[name]
        inline_map.setdefault(target, []).append((by_name[name], by_name[name].get("config", {})))

    return inline_set, inline_map


def _is_settings_tab_module(
    m: dict[str, Any],
    inline_set: set[str],
    inline_map: dict[str, list[tuple[dict[str, Any], dict[str, Any]]]],
) -> bool:
    """True when generate_settings_dialog_tabs would emit a tab for this module."""
    name = m["name"]
    if name in ("ai", "main", "core") or name in inline_set:
        return False
    if name in ("tunnel", "launcher"):
        return False
    if m.get("settings_tab") is False:
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
