# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Filesystem-free tests for plugin.framework.config_schema."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from plugin.framework.errors import ConfigValidationError
from plugin.framework.config_schema import (
    WriterAgentConfig,
    get_config_schema,
    is_default_value,
    is_known_config_key,
    parse_float_robust,
    set_endpoint_normalizer,
    set_manifest_modules,
    validate,
)

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
    )
    yield schema
    schema.MODULES, schema.CONFIG_DEFAULTS, schema.CONFIG_SCHEMAS = orig


def test_config_schema_has_no_forbidden_imports() -> None:
    source = _SCHEMA_PATH.read_text(encoding="utf-8")
    imported = _imported_modules(ast.parse(source))
    forbidden = sorted(mod for mod in imported if _is_forbidden(mod))
    assert forbidden == []
    assert "plugin.framework.config" not in imported


def test_config_schema_import_populates_modules_and_defaults() -> None:
    """Real import of config_schema loads manifest MODULES and module.yaml defaults."""
    import plugin.framework.config_schema as schema

    assert schema.MODULES
    assert schema.CONFIG_DEFAULTS


def test_manifest_config_keys_are_known_in_schema() -> None:
    from plugin._manifest import MODULES
    from plugin.framework.config_schema import is_known_config_key

    for mod in MODULES:
        if not isinstance(mod, dict):
            continue
        mod_name = mod.get("name")
        for key, spec in (mod.get("config") or {}).items():
            if key.startswith("_") or not isinstance(spec, dict) or spec.get("type") == "separator":
                continue
            assert is_known_config_key(f"{mod_name}.{key}")


def test_parse_float_robust_huge_int_is_value_error() -> None:
    """A ≥309-digit int must degrade as ValueError, not OverflowError.

    JSON decodes that integer. float() then overflows. Config load catches
    ValueError and continues.
    """
    huge = 10**400
    with pytest.raises(ValueError):
        parse_float_robust(huge)
    with pytest.raises(ValueError):
        parse_float_robust(-huge)
    assert is_default_value("temperature", huge) is False
    with pytest.raises(ConfigValidationError):
        validate({"temperature": huge})


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
    assert validate({"demo.count": "3"})["demo.count"] == 3
    with pytest.raises(ConfigValidationError):
        validate({"demo.count": "not-a-number"})
    with pytest.raises(ConfigValidationError):
        validate({"demo.count": 1})
    with pytest.raises(ConfigValidationError):
        validate({"demo.count": 99})
    schema = get_config_schema("demo.count")
    assert schema is not None
    assert schema["default"] == 5
    assert is_known_config_key("demo.count")
    assert is_default_value("demo.count", 5)
    assert not is_default_value("demo.count", 8)


def test_validate_and_defaults() -> None:
    res = validate(
        {
            "temperature": "0,7",
            "chat_max_tokens": 16384.0,
            "unknown_extra": "keep-until-prune",
        }
    )
    assert res["temperature"] == pytest.approx(0.7)
    assert res["chat_max_tokens"] == 16384
    assert res["unknown_extra"] == "keep-until-prune"


def test_validate_constraints() -> None:
    with pytest.raises(ConfigValidationError) as err:
        validate({"temperature": 1.5})
    assert err.value.code == "INVALID_TEMPERATURE"


def test_strict_coerce_uses_dataclass_bounds_without_rewriting_defaults() -> None:
    """Settings compare trusts strict coerce. Defaults must still compare equal.

    Strict coerce must reject out-of-range values such as temperature 5.0 or
    negative token limits. In-range defaults, including the temperature sentinel
    -1.0, must round-trip unchanged or an untouched Settings OK looks like a write.
    """
    temperature = get_config_schema("temperature")
    tokens = get_config_schema("chat_max_tokens")
    timeout = get_config_schema("request_timeout")
    assert temperature is not None and temperature["max"] == 1.0
    assert tokens is not None and tokens["min"] == 0
    assert timeout is not None and timeout["min_exclusive"] == 0

    with pytest.raises(ConfigValidationError):
        validate({"temperature": 5.0})
    with pytest.raises(ConfigValidationError):
        validate({"chat_max_tokens": -1})
    with pytest.raises(ConfigValidationError):
        validate({"request_timeout": 0})

    assert validate({"temperature": -1.0})["temperature"] == -1.0
    assert validate({"temperature": "-1.0"})["temperature"] == -1.0
    assert validate({"chat_max_tokens": 16384})["chat_max_tokens"] == 16384
    assert validate({"chat_max_tokens": "16384"})["chat_max_tokens"] == 16384
    assert validate({"request_timeout": 120})["request_timeout"] == 120
    assert validate({"request_timeout": "120"})["request_timeout"] == 120
    assert validate({"temperature": 1.0})["temperature"] == 1.0
    assert validate({"chat_max_tokens": 0})["chat_max_tokens"] == 0
    assert validate({"request_timeout": 1})["request_timeout"] == 1


def test_set_and_get_manifest_modules(restore_manifest) -> None:
    modules = [{"name": "only", "config": {"flag": {"type": "boolean", "default": False}}}]
    set_manifest_modules(modules)
    assert restore_manifest.MODULES is modules
    assert restore_manifest.CONFIG_DEFAULTS["only.flag"] is False
    assert validate({"only.flag": "yes"})["only.flag"] is True


def test_dataclass_field_type_resolves_int_with_postponed_annotations() -> None:
    import dataclasses

    from plugin.framework.config_schema import _dataclass_field_type

    field = next(f for f in dataclasses.fields(WriterAgentConfig) if f.name == "chat_max_tokens")
    assert _dataclass_field_type(field) == "int"


def test_set_endpoint_normalizer() -> None:
    # Restoring normalize_endpoint_url dropped the selector resolver that
    # config.py installs at import, so a later test saw the raw "OpenRouter" label.
    from plugin.framework import config_schema as schema

    previous = schema._endpoint_normalizer
    try:
        assert schema._endpoint_normalizer("localhost", False) == "localhost"

        def mock_normalizer(endpoint_str: str, is_openwebui: bool) -> str:
            return f"mock_{endpoint_str}_{is_openwebui}"

        set_endpoint_normalizer(mock_normalizer)
        assert schema._endpoint_normalizer("localhost", False) == "mock_localhost_False"
    finally:
        set_endpoint_normalizer(previous)


# PO-header junk mistakenly saved into config via gettext/translation bugs (i18n + load path)
_PO_JUNK = "Project-Id-Version: WriterAgent 1.0\nReport-Msgid-Bugs-To: x\n"


def test_config_validate_maps_translated_label_to_canonical_in_extra_config(restore_manifest) -> None:
    """Saved UI label (wrong) in dotted key is normalized to schema value via _()."""
    from unittest.mock import patch

    set_manifest_modules(
        [
            {
                "name": "agent_backend",
                "config": {
                    "backend_id": {
                        "type": "string",
                        "default": "builtin",
                        "options": [{"value": "hermes", "label": "Hermes"}],
                    },
                },
            }
        ]
    )

    def _fake(msg: str) -> str:
        if msg == "Hermes":
            return "GERMAN_HERMES"
        return msg

    with patch("plugin.framework.config_schema._", side_effect=_fake):
        validated = validate({"endpoint": "http://127.0.0.1:11434", "agent_backend.backend_id": "GERMAN_HERMES"})
    assert validated["agent_backend.backend_id"] == "hermes"


def test_po_strip_extra_config_on_validate() -> None:
    data = {
        "endpoint": "http://localhost:11434",
        "agent_backend.path": _PO_JUNK,
        "agent_backend.args": _PO_JUNK,
    }
    validated = validate(data)
    assert validated.get("agent_backend.path") == ""
    assert validated.get("agent_backend.args") == ""


def test_po_strip_seed_to_minus_one() -> None:
    data = {"endpoint": "http://x", "seed": _PO_JUNK}
    validated = validate(data)
    assert validated.get("seed") == "-1"


def test_po_strip_top_level_string_field() -> None:
    data = {"endpoint": "http://x", "additional_instructions": _PO_JUNK}
    validated = validate(data)
    assert validated.get("additional_instructions") == ""


def test_export_dataclass_keys_from_attributes() -> None:
    data = {
        "endpoint": "http://example.com/v1",
        "chat_max_tokens": 2048,
        "agent_backend.path": "",
    }
    out = validate(data)
    assert out["endpoint"] == "http://example.com"
    assert out["chat_max_tokens"] == 2048


def test_extra_key_fallback_when_missing_from_extra_config() -> None:
    """If a key is absent from _extra_config, keep JSON value (edge case)."""
    data = {"endpoint": "http://x", "orphan.key": "keep-me"}
    out = validate(data)
    assert out.get("orphan.key") == "keep-me"

