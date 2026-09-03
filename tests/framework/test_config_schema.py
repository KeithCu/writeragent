# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Filesystem-free tests for plugin.framework.config_schema."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from plugin.framework.config_schema import (
    WriterAgentConfig,
    as_bool,
    clamp_schema_value,
    coerce_config_value,
    get_config_schema,
    get_manifest_modules,
    is_default_value,
    is_known_config_key,
    parse_float_robust,
    parse_int_robust,
    prune_default_values,
    set_endpoint_normalizer,
    set_manifest_modules,
    _get_schema_default,
    _normalize_configured_endpoint,
)
from plugin.framework.errors import ConfigValidationError
from plugin.framework.url_utils import normalize_endpoint_url

_SCHEMA_PATH = Path(__file__).resolve().parents[2] / "plugin" / "framework" / "config_schema.py"
_FORBIDDEN_IMPORT_ROOTS = frozenset(
    {
        "plugin.framework.config",
        "plugin.chatbot",
        "plugin.calc",
        "plugin.framework.uno_context",
        "plugin.framework.event_bus",
    }
)


def _imported_modules(tree: ast.AST) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                names.add(alias.name)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    return names


def _is_forbidden(mod: str) -> bool:
    return any(mod == root or mod.startswith(root + ".") for root in _FORBIDDEN_IMPORT_ROOTS)


@pytest.fixture
def restore_manifest():
    from plugin.framework import config_schema as schema

    orig = (
        schema.MODULES,
        schema.CONFIG_DEFAULTS,
        schema.CONFIG_SCHEMAS,
        schema.DOTTED_FALLBACKS,
    )
    yield schema
    schema.MODULES, schema.CONFIG_DEFAULTS, schema.CONFIG_SCHEMAS, schema.DOTTED_FALLBACKS = orig


def test_config_schema_imports_only_modules_from_manifest() -> None:
    """Do not require derived _manifest dicts; ImportError on those emptied MODULES."""
    source = _SCHEMA_PATH.read_text(encoding="utf-8")
    imported_from_manifest: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.ImportFrom) and node.module == "plugin._manifest":
            imported_from_manifest.update(alias.name for alias in node.names)
    assert imported_from_manifest == {"MODULES"}
    assert "set_manifest_modules(_DEFAULT_MODULES)" in source


def test_module_yaml_defaults_bind_from_manifest_modules() -> None:
    """module.yaml keys resolve from MODULES; no writeragent.json required."""
    from plugin.framework import config_schema as schema

    assert schema.MODULES, "empty MODULES; run make manifest"
    assert _get_schema_default("scripting.python_max_data_cells") == 250000
    assert _get_schema_default("scripting.python_venv_path") == ""
    assert _get_schema_default("scripting.python_auto_spill") is True
    assert _get_schema_default("scripting.python_geometric_recalc_order") is False
    assert _get_schema_default("doc.agent_edit_review_mode") == "off"
    assert _get_schema_default("chatbot.max_tool_rounds") == 15
    assert schema.CONFIG_DEFAULTS["scripting.python_max_data_cells"] == 250000
    assert schema.MODULES is schema._DEFAULT_MODULES


def test_config_schema_has_no_forbidden_imports() -> None:
    source = _SCHEMA_PATH.read_text(encoding="utf-8")
    imported = _imported_modules(ast.parse(source))
    forbidden = sorted(mod for mod in imported if _is_forbidden(mod))
    assert forbidden == []
    assert "plugin.framework.config" not in imported


def test_config_does_not_reexport_schema_names() -> None:
    import plugin.framework.config as config

    for name in (
        "as_bool",
        "clamp_schema_value",
        "coerce_config_value",
        "WriterAgentConfig",
        "is_default_value",
        "set_manifest_modules",
        "MODULES",
    ):
        assert name not in vars(config), name
        with pytest.raises(ImportError):
            exec(f"from plugin.framework.config import {name}")


def test_as_bool_and_numeric_parsers() -> None:
    assert as_bool("true") is True
    assert as_bool("off") is False
    assert parse_int_robust("8765,0") == 8765
    assert parse_float_robust("1,5") == pytest.approx(1.5)


def test_coerce_and_clamp_use_manifest_schema(restore_manifest) -> None:
    set_manifest_modules(
        [
            {
                "name": "demo",
                "config": {
                    "count": {"type": "int", "default": 5, "min": 2, "max": 10},
                    "mode": {
                        "type": "string",
                        "default": "fast",
                        "options": [{"value": "fast", "label": "Fast Mode"}],
                    },
                },
            }
        ]
    )
    assert coerce_config_value("demo.count", "3") == 3
    assert clamp_schema_value("demo.count", 1) == 2
    assert clamp_schema_value("demo.count", 99) == 10
    assert coerce_config_value("demo.count", "not-a-number") == 5
    schema = get_config_schema("demo.count")
    assert schema is not None
    assert schema["default"] == 5
    assert is_known_config_key("demo.count")
    assert is_default_value("demo.count", 5)
    assert not is_default_value("demo.count", 8)


def test_prune_default_values_drops_unknown_and_defaults() -> None:
    pruned = prune_default_values(
        {
            "endpoint": "http://localhost:11434",
            "request_timeout": 60,
            "not_a_real_config_key": "x",
        }
    )
    assert pruned == {"request_timeout": 60}


def test_writeragent_config_from_dict_and_defaults() -> None:
    cfg = WriterAgentConfig.from_dict(
        {
            "temperature": "0,7",
            "chat_max_tokens": 16384.0,
            "unknown_extra": "keep-until-prune",
        }
    )
    cfg.validate()
    assert cfg.temperature == pytest.approx(0.7)
    assert cfg.chat_max_tokens == 16384
    dumped = cfg.to_dict()
    assert "chat_max_tokens" not in dumped
    assert dumped["temperature"] == pytest.approx(0.7)
    assert "unknown_extra" not in dumped


def test_writeragent_config_validate_constraints() -> None:
    with pytest.raises(ConfigValidationError) as err:
        WriterAgentConfig(temperature=1.5).validate()
    assert err.value.code == "INVALID_TEMPERATURE"


def test_validate_coerce_out_of_range_clamps_instead_of_raising():
    from plugin.framework.config_schema import WriterAgentConfig

    cfg = WriterAgentConfig(temperature=1.5, chat_max_tokens=-1, request_timeout=0)
    cfg.validate(coerce_out_of_range=True)
    assert cfg.temperature == 1.0
    assert cfg.chat_max_tokens == 16384
    assert cfg.request_timeout == 120


def test_set_and_get_manifest_modules(restore_manifest) -> None:
    modules = [{"name": "only", "config": {"flag": {"type": "boolean", "default": False}}}]
    set_manifest_modules(modules)
    assert get_manifest_modules() is restore_manifest.MODULES
    assert restore_manifest.CONFIG_DEFAULTS["only.flag"] is False
    assert coerce_config_value("only.flag", "yes") is True


def test_set_endpoint_normalizer() -> None:
    try:
        assert _normalize_configured_endpoint("localhost", False) == "localhost"

        def mock_normalizer(endpoint_str: str, is_openwebui: bool) -> str:
            return f"mock_{endpoint_str}_{is_openwebui}"

        set_endpoint_normalizer(mock_normalizer)
        assert _normalize_configured_endpoint("localhost", False) == "mock_localhost_False"
    finally:
        set_endpoint_normalizer(normalize_endpoint_url)
