# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Registry-wide validation of JSON Schema ``default`` values on shipped tools."""

from __future__ import annotations

import importlib
from typing import Any

from plugin._manifest import MODULES
from plugin.framework.tool import ToolBase
from plugin.framework.tool_schema import call_properties, required_keys, value_matches_type
from plugin.mcp_client.client import RemoteMcpTool


def _import_all_tool_modules() -> None:
    """Import every plugin package/module listed in the manifest so all ToolBase subclasses load."""
    for mod_info in MODULES:
        pkg = mod_info.get("package", mod_info["name"])
        try:
            importlib.import_module(f"plugin.{pkg}")
        except ImportError:
            continue


def _shipped_tool_classes() -> list[type[ToolBase]]:
    _import_all_tool_modules()
    seen: set[int] = set()
    stack: list[type[ToolBase]] = list(ToolBase.__subclasses__())
    out: list[type[ToolBase]] = []
    while stack:
        cls = stack.pop()
        if id(cls) in seen:
            continue
        seen.add(id(cls))
        stack.extend(cls.__subclasses__())
        if cls is RemoteMcpTool or issubclass(cls, RemoteMcpTool):
            continue
        if not (isinstance(getattr(cls, "name", None), str) and cls.name):
            continue
        if str(getattr(cls, "__module__", "")).startswith(("tests.", "test_")):
            continue
        out.append(cls)
    return out


def _minimal_value(spec: dict[str, Any]) -> Any:
    if "enum" in spec and spec["enum"]:
        return spec["enum"][0]
    schema_type = spec.get("type")
    if isinstance(schema_type, list):
        schema_type = schema_type[0] if schema_type else "string"
    return {
        "string": "x",
        "integer": 1,
        "number": 1.0,
        "boolean": False,
        "array": [],
        "object": {},
    }.get(schema_type, "x")


def test_every_registered_schema_default_validates() -> None:
    """Every shipped tool's schema defaults must pass type, enum, and tool.validate()."""
    tool_classes = _shipped_tool_classes()
    assert len(tool_classes) > 0

    checked = 0
    for cls in tool_classes:
        tool = cls()
        doc_types: list[str | None] = list(tool.doc_types) if tool.doc_types else [None, "writer", "calc", "draw", "impress"]
        for dt in doc_types:
            schema = tool.get_parameters(dt) or {}
            props = call_properties(schema) or {}
            defaults: dict[str, Any] = {}
            for key, spec in props.items():
                if not (isinstance(spec, dict) and "default" in spec):
                    continue
                default_val = spec["default"]
                assert value_matches_type(default_val, spec.get("type")), (
                    f"{cls.name} ({dt}): default {default_val!r} for {key!r} does not match type {spec.get('type')!r}"
                )
                if "enum" in spec:
                    assert default_val in spec["enum"], (
                        f"{cls.name} ({dt}): default {default_val!r} for {key!r} not in enum {spec['enum']!r}"
                    )
                defaults[key] = default_val
                checked += 1

            if defaults:
                req_kwargs = {
                    key: _minimal_value(props[key])
                    for key in required_keys(schema)
                    if key not in defaults and isinstance(props.get(key), dict)
                }
                ok, err = tool.validate(doc_type=dt, **{**req_kwargs, **defaults})
                assert ok, f"{cls.name} ({dt}): defaults failed validate(): {err}"

    assert checked > 0
