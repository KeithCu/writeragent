# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for vision availability checking and schema/tool filtering."""
from unittest.mock import MagicMock, patch

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


def test_vision_venv_configured_catches_config_error():
    # What was wrong: pytest escape hatch returned False unconditionally in tests, hiding real config behavior.
    # Why this change: verify ConfigError is caught, logged, and returns False safely.
    with patch("plugin.vision.vision_availability.get_config_str", side_effect=ConfigError("broken config")):
        assert vision_venv_configured() is False


def test_vision_venv_configured_success():
    with (
        patch("plugin.vision.vision_availability.get_config_str", return_value="/fake/venv"),
        patch("plugin.vision.vision_availability.resolve_venv_python", return_value="/fake/venv/bin/python"),
    ):
        assert vision_venv_configured() is True


def test_filter_vision_delegate_schemas_consistent_with_ctx_none():
    # What was wrong: filter_vision_delegate_schemas returned early when ctx=None, advertising vision when unavailable,
    # while filter_vision_specialized_tools hid it.
    # Why this change: verify schemas are filtered out when vision is unavailable even if ctx is None.
    schemas = [
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
    with patch("plugin.vision.vision_availability.vision_venv_configured", return_value=False):
        filtered_none = filter_vision_delegate_schemas(schemas, ctx=None)
        filtered_ctx = filter_vision_delegate_schemas(schemas, ctx=MagicMock())

    enum_none = filtered_none[0]["function"]["parameters"]["properties"]["domain"]["enum"]
    enum_ctx = filtered_ctx[0]["function"]["parameters"]["properties"]["domain"]["enum"]
    assert enum_none == ["writing", "formatting"]
    assert enum_ctx == ["writing", "formatting"]


def test_filter_vision_delegate_schemas_kept_when_available():
    schemas = [
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
    with patch("plugin.vision.vision_availability.vision_venv_configured", return_value=True):
        filtered = filter_vision_delegate_schemas(schemas, ctx=None)
    enum = filtered[0]["function"]["parameters"]["properties"]["domain"]["enum"]
    assert enum == ["writing", "vision", "formatting"]


def test_filter_vision_specialized_tools_consistent_with_ctx_none():
    tools = [_Tool("apply_document_content"), _Tool("extract_structure_from_image")]
    with patch("plugin.vision.vision_availability.vision_venv_configured", return_value=False):
        filtered_none = filter_vision_specialized_tools(tools, ctx=None)
        filtered_ctx = filter_vision_specialized_tools(tools, ctx=MagicMock())

    assert [t.name for t in filtered_none] == ["apply_document_content"]
    assert [t.name for t in filtered_ctx] == ["apply_document_content"]


def test_filter_vision_specialized_tools_kept_when_available():
    tools = [_Tool("apply_document_content"), _Tool("extract_structure_from_image")]
    with patch("plugin.vision.vision_availability.vision_venv_configured", return_value=True):
        filtered = filter_vision_specialized_tools(tools, ctx=None)
    assert [t.name for t in filtered] == ["apply_document_content", "extract_structure_from_image"]


def test_specialized_domain_available():
    with patch("plugin.vision.vision_availability.vision_venv_configured", return_value=True):
        assert specialized_domain_available("vision") is True
    with patch("plugin.vision.vision_availability.vision_venv_configured", return_value=False):
        assert specialized_domain_available("vision") is False
    assert specialized_domain_available("writing") is True
