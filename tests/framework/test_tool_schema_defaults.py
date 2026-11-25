# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Registry-wide validation of shipped tool schemas."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any


def _prompt_optimization_dir() -> Path:
    # release_verify runs pytest inside a stripped temp tree without scripts/,
    # setting WRITERAGENT_PROJECT_ROOT to the real checkout.
    env_root = os.environ.get("WRITERAGENT_PROJECT_ROOT", "").strip()
    if env_root:
        candidate = Path(env_root) / "scripts" / "prompt_optimization"
        if candidate.is_dir():
            return candidate
    return Path(__file__).resolve().parents[2] / "scripts" / "prompt_optimization"


_PO = _prompt_optimization_dir()
if str(_PO) not in sys.path:
    sys.path.insert(0, str(_PO))

from eval_catalog import headless_registry  # noqa: E402
from plugin.framework.tool_schema import call_properties, page_index_param, required_keys, value_matches_type  # noqa: E402


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
    tools = headless_registry().get_tools(filter_doc_type=False, exclude_tiers=frozenset())
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


def _iter_property_specs(props: dict[str, Any]) -> Any:
    for key, spec in props.items():
        if not isinstance(spec, dict):
            continue
        yield key, spec
        nested = spec.get("properties")
        if isinstance(nested, dict):
            yield from _iter_property_specs(nested)
        items = spec.get("items")
        if isinstance(items, dict) and isinstance(items.get("properties"), dict):
            yield from _iter_property_specs(items["properties"])


def test_page_index_params_come_from_the_factory() -> None:
    """Every integer ``page`` / ``*_page`` parameter must use ``page_index_param``."""
    tools = headless_registry().get_tools(filter_doc_type=False, exclude_tiers=frozenset())
    seen: set[tuple[str | None, str]] = set()
    for tool in tools:
        for dt in (None, "writer", "calc", "draw", "impress"):
            schema = tool.get_parameters(dt) or {}
            props = call_properties(schema) or {}
            for key, spec in _iter_property_specs(props):
                if spec.get("type") != "integer":
                    continue
                if key != "page" and not key.endswith("_page"):
                    continue
                expected_prefix = page_index_param(name=key)["description"]
                desc = spec.get("description", "")
                assert desc.startswith(expected_prefix), (
                    f"{tool.name} ({dt}) {key!r}: description {desc!r} does not start with {expected_prefix!r}"
                )
                seen.add((tool.name, key))

    expected_pairs = {
        ("slide_move", "to_page"),
        ("image_get", "page"),
        ("chart_upsert", "page"),
        ("form_create", "page"),
        ("nav_goto_page", "page"),
    }
    assert expected_pairs <= seen


def test_prompt_optimization_dir_uses_project_root_env(monkeypatch: Any, tmp_path: Path) -> None:
    expected = tmp_path / "scripts" / "prompt_optimization"
    expected.mkdir(parents=True)
    monkeypatch.setenv("WRITERAGENT_PROJECT_ROOT", str(tmp_path))
    assert _prompt_optimization_dir() == expected
