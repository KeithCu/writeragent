# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Registry-wide validation of JSON Schema ``default`` values on shipped tools."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

_PO = Path(__file__).resolve().parents[2] / "scripts" / "prompt_optimization"
if str(_PO) not in sys.path:
    sys.path.insert(0, str(_PO))

from eval_catalog import _headless_registry
from plugin.framework.tool_schema import call_properties, required_keys, value_matches_type


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
    tools = _headless_registry().get_tools(filter_doc_type=False, exclude_tiers=frozenset())
    modules = {type(tool).__module__ for tool in tools}
    for prefix in ("plugin.calc.", "plugin.draw.", "plugin.writer.images."):
        assert any(mod.startswith(prefix) for mod in modules), f"No tools loaded from {prefix}"

    checked = 0
    for tool in tools:
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
                    f"{tool.name} ({dt}): default {default_val!r} for {key!r} does not match type {spec.get('type')!r}"
                )
                if "enum" in spec:
                    assert default_val in spec["enum"], (
                        f"{tool.name} ({dt}): default {default_val!r} for {key!r} not in enum {spec['enum']!r}"
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
                assert ok, f"{tool.name} ({dt}): defaults failed validate(): {err}"

    assert checked > 0
