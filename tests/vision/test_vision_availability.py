# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for vision availability checking and schema/tool filtering."""
from unittest.mock import MagicMock, patch

import pytest

from plugin.framework.errors import ConfigError
from plugin.vision.vision_availability import (
    filter_unavailable_delegate_domains,
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
                "name": "delegate_to_subagent",
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


# ctx=None must filter the same as a live ctx (same policy for delegate schemas and specialized tools).
@pytest.mark.parametrize(
    ("vision_available", "mcp_servers", "use_ctx", "expected_enum"),
    [
        pytest.param(False, [], False, ["writing", "formatting"], id="neither_ctx_none"),
        pytest.param(False, [], True, ["writing", "formatting"], id="neither_with_ctx"),
        pytest.param(True, [], False, ["writing", "vision", "formatting"], id="vision_only_ctx_none"),
        pytest.param(True, [], True, ["writing", "vision", "formatting"], id="vision_only_with_ctx"),
        pytest.param(False, [{"name": "s", "url": "http://x"}], True, ["writing", "mcp", "formatting"], id="mcp_only_with_ctx"),
        pytest.param(True, [{"name": "s", "url": "http://x"}], True, ["writing", "vision", "mcp", "formatting"], id="both_with_ctx"),
    ],
)
def test_filter_unavailable_delegate_domains(
    vision_available: bool, mcp_servers: list[dict], use_ctx: bool, expected_enum: list[str]
) -> None:
    schemas = _delegate_schemas()
    schemas[0]["function"]["parameters"]["properties"]["domain"]["enum"] = ["writing", "vision", "mcp", "formatting"]
    ctx = MagicMock() if use_ctx else None
    with (
        patch("plugin.vision.vision_availability.vision_venv_configured", return_value=vision_available),
        patch("plugin.mcp_client.client.configured_servers", return_value=mcp_servers),
    ):
        filtered = filter_unavailable_delegate_domains(schemas, ctx=ctx)
    assert filtered[0]["function"]["parameters"]["properties"]["domain"]["enum"] == expected_enum


@pytest.mark.parametrize(
    ("available", "use_ctx", "expected_names"),
    [
        pytest.param(False, False, ["apply_content"], id="unavailable_ctx_none"),
        pytest.param(False, True, ["apply_content"], id="unavailable_with_ctx"),
        pytest.param(True, False, ["apply_content", "image_extract_structure"], id="available_ctx_none"),
        pytest.param(True, True, ["apply_content", "image_extract_structure"], id="available_with_ctx"),
    ],
)
def test_filter_vision_specialized_tools(available: bool, use_ctx: bool, expected_names: list[str]) -> None:
    tools = [_Tool("apply_content"), _Tool("image_extract_structure")]
    ctx = MagicMock() if use_ctx else None
    with patch("plugin.vision.vision_availability.vision_venv_configured", return_value=available):
        filtered = filter_vision_specialized_tools(tools, ctx=ctx)
    assert [t.name for t in filtered] == expected_names


def test_specialized_domain_available():
    with patch("plugin.vision.vision_availability.vision_venv_configured", return_value=True):
        assert specialized_domain_available("vision") is True
    with patch("plugin.vision.vision_availability.vision_venv_configured", return_value=False):
        assert specialized_domain_available("vision") is False
    with patch("plugin.mcp_client.client.configured_servers", return_value=[{"name": "s", "url": "http://x"}]):
        assert specialized_domain_available("mcp") is True
    with patch("plugin.mcp_client.client.configured_servers", return_value=[]):
        assert specialized_domain_available("mcp") is False
    assert specialized_domain_available("writing") is True


def test_filter_unavailable_delegate_domains_skips_odd_entries_and_does_not_mutate():
    schema = {
        "type": "function",
        "function": {
            "name": "delegate_to_subagent",
            "parameters": {"type": "object", "properties": {"domain": {"type": "string", "enum": ["writing", "vision"]}}},
        },
    }
    junk = {"function": "not-a-dict"}
    with patch("plugin.vision.vision_availability.vision_venv_configured", return_value=False):
        filtered = filter_unavailable_delegate_domains([schema, "junk", junk, {"type": "function"}], ctx=None)
    assert schema["function"]["parameters"]["properties"]["domain"]["enum"] == ["writing", "vision"]
    assert filtered[0] == {**schema, "function": {**schema["function"], "parameters": {"type": "object", "properties": {"domain": {"type": "string", "enum": ["writing"]}}}}}
    assert filtered[1:] == ["junk", junk, {"type": "function"}]

