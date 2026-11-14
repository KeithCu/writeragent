# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for vision availability checking and schema/tool filtering."""
from unittest.mock import MagicMock, patch

import pytest

from plugin.framework.errors import ConfigError
from plugin.vision.vision_availability import (
    filter_vision_delegate_schemas,
    filter_vision_specialized_tools,
    specialized_domain_available,
    vision_venv_configured,
)


class _Tool:
    def __init__(self, name: str) -> None:
        self.name = name


def test_vision_venv_configured_propagates_config_error():
    # Config typos must propagate ConfigError rather than being swallowed as False.
    with patch("plugin.vision.vision_availability.get_config", side_effect=ConfigError("broken config")):
        with pytest.raises(ConfigError):
            vision_venv_configured()


@pytest.mark.parametrize(
    ("config_value", "resolve_result", "expected"),
    [
        pytest.param("", "/fake/venv/bin/python", False, id="empty"),
        pytest.param("   \t  ", "/fake/venv/bin/python", False, id="whitespace"),
        pytest.param("/fake/venv", None, False, id="unresolvable_python"),
        pytest.param("/fake/venv", "/fake/venv/bin/python", True, id="configured"),
    ],
)
def test_vision_venv_configured(config_value: str, resolve_result: str | None, expected: bool):
    with (
        patch("plugin.vision.vision_availability.get_config", return_value=config_value),
        patch("plugin.vision.vision_availability.resolve_venv_python", return_value=resolve_result),
    ):
        assert vision_venv_configured() is expected


def _delegate_schemas() -> list[dict]:
    return [
        {
            "type": "function",
            "function": {
                "name": "delegate_to_specialized_writer_toolset",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "domain": {
                            "type": "string",
                            "enum": ["writing", "vision", "formatting"],
                        }
                    },
                },
            },
        }
    ]


@pytest.mark.parametrize(
    ("available", "use_ctx", "expected_enum"),
    [
        pytest.param(False, False, ["writing", "formatting"], id="unavailable_ctx_none"),
        pytest.param(False, True, ["writing", "formatting"], id="unavailable_with_ctx"),
        pytest.param(True, False, ["writing", "vision", "formatting"], id="available_ctx_none"),
        pytest.param(True, True, ["writing", "vision", "formatting"], id="available_with_ctx"),
    ],
)
def test_filter_vision_delegate_schemas(available: bool, use_ctx: bool, expected_enum: list[str]) -> None:
    ctx = MagicMock() if use_ctx else None
    with patch("plugin.vision.vision_availability.vision_venv_configured", return_value=available):
        filtered = filter_vision_delegate_schemas(_delegate_schemas(), ctx=ctx)
    assert filtered[0]["function"]["parameters"]["properties"]["domain"]["enum"] == expected_enum


@pytest.mark.parametrize(
    ("available", "use_ctx", "expected_names"),
    [
        pytest.param(False, False, ["apply_document_content"], id="unavailable_ctx_none"),
        pytest.param(False, True, ["apply_document_content"], id="unavailable_with_ctx"),
        pytest.param(True, False, ["apply_document_content", "extract_structure_from_image"], id="available_ctx_none"),
        pytest.param(True, True, ["apply_document_content", "extract_structure_from_image"], id="available_with_ctx"),
    ],
)
def test_filter_vision_specialized_tools(available: bool, use_ctx: bool, expected_names: list[str]) -> None:
    tools = [_Tool("apply_document_content"), _Tool("extract_structure_from_image")]
    ctx = MagicMock() if use_ctx else None
    with patch("plugin.vision.vision_availability.vision_venv_configured", return_value=available):
        filtered = filter_vision_specialized_tools(tools, ctx=ctx)
    assert [t.name for t in filtered] == expected_names


def test_specialized_domain_available():
    with patch("plugin.vision.vision_availability.vision_venv_configured", return_value=True):
        assert specialized_domain_available("vision") is True
    with patch("plugin.vision.vision_availability.vision_venv_configured", return_value=False):
        assert specialized_domain_available("vision") is False
    assert specialized_domain_available("writing") is True


def test_filter_vision_delegate_schemas_skips_odd_entries_and_does_not_mutate():
    schema = {
        "type": "function",
        "function": {
            "name": "delegate_to_specialized_writer_toolset",
            "parameters": {"type": "object", "properties": {"domain": {"type": "string", "enum": ["writing", "vision"]}}},
        },
    }
    junk = {"function": "not-a-dict"}
    with patch("plugin.vision.vision_availability.vision_venv_configured", return_value=False):
        filtered = filter_vision_delegate_schemas([schema, "junk", junk, {"type": "function"}], ctx=None)
    assert schema["function"]["parameters"]["properties"]["domain"]["enum"] == ["writing", "vision"]
    assert filtered[0] == {**schema, "function": {**schema["function"], "parameters": {"type": "object", "properties": {"domain": {"type": "string", "enum": ["writing"]}}}}}
    assert filtered[1:] == ["junk", junk, {"type": "function"}]


@patch("plugin.vision.vision_availability.vision_venv_configured", return_value=True)
def test_get_vision_core_directive_writer_is_empty(_mock_avail):
    from plugin.vision.vision_availability import get_vision_core_directive

    writer_doc = MagicMock()
    writer_doc.supportsService.side_effect = lambda svc: svc == "com.sun.star.text.TextDocument"
    assert get_vision_core_directive(writer_doc, MagicMock()) == ""

