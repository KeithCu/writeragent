# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
"""Tests for agent backend registry and backend specs."""

from pathlib import Path
from unittest.mock import patch
import pytest
import yaml

from plugin.acp.builtin import BuiltinBackend
from plugin.acp.registry import (
    AGENT_BACKEND_REGISTRY,
    _ACP_BACKENDS,
    AntigravityBackend,
    ClaudeBackend,
    GrokBackend,
    HermesBackend,
    OpenCodeBackend,
    VibeBackend,
    get_backend,
    normalize_backend_id,
)

_MODULE_YAML = Path(__file__).resolve().parent.parent.parent / "plugin" / "acp" / "module.yaml"


def test_normalize_known_and_empty_ids():
    assert normalize_backend_id("hermes") == "hermes"
    assert normalize_backend_id("Hermes") == "hermes"
    assert normalize_backend_id("builtin") == "builtin"
    assert normalize_backend_id("") == "builtin"
    assert normalize_backend_id(None) == "builtin"
    assert normalize_backend_id("   ") == "builtin"


def test_normalize_unknown_id_returns_builtin():
    assert normalize_backend_id("gemini") == "builtin"
    assert normalize_backend_id("nonexistent") == "builtin"
    assert isinstance(get_backend("gemini"), BuiltinBackend)


def test_normalize_translated_builtin_labels():
    assert normalize_backend_id("Built-in") == "builtin"
    assert normalize_backend_id("Eingebaut") == "builtin"
    assert isinstance(get_backend("Integriert"), BuiltinBackend)


def test_module_yaml_matches_registry_specs():
    data = yaml.safe_load(_MODULE_YAML.read_text(encoding="utf-8"))
    options = data["config"]["backend_id"]["options"]
    yaml_pairs = [(opt["value"], opt["label"]) for opt in options]
    expected_pairs = [("builtin", "Built-in"), *[(cls.backend_id, cls.display_name) for cls in _ACP_BACKENDS]]
    assert yaml_pairs == expected_pairs


def test_registry_keys_match_backend_id():
    for key, cls in AGENT_BACKEND_REGISTRY.items():
        assert cls.backend_id == key


@pytest.mark.parametrize("cls", _ACP_BACKENDS, ids=lambda c: c.backend_id)
def test_backend_is_available_matrix(cls):
    backend = cls()
    fake_path = f"/usr/bin/{cls.binary_name}"

    with patch("shutil.which", return_value=fake_path), patch("os.path.isfile", return_value=True):
        assert backend.is_available(None) is True

    with patch("shutil.which", return_value=None), patch("os.path.isfile", return_value=False):
        assert backend.is_available(None) is False


def test_claude_env_vars():
    with patch("plugin.acp.acp_backend.get_provider_from_endpoint", return_value="anthropic"), \
         patch("plugin.acp.acp_backend.get_api_key_for_endpoint", return_value="sk-ant"), \
         patch("plugin.acp.acp_backend.get_current_endpoint", return_value="https://api.anthropic.com"):
        assert ClaudeBackend().get_env_vars() == {"ANTHROPIC_API_KEY": "sk-ant"}

    with patch("plugin.acp.acp_backend.get_provider_from_endpoint", return_value="openai"), \
         patch("plugin.acp.acp_backend.get_api_key_for_endpoint") as mock_key, \
         patch("plugin.acp.acp_backend.get_current_endpoint", return_value="https://api.openai.com/v1"):
        assert ClaudeBackend().get_env_vars() == {}
        mock_key.assert_not_called()

    with patch("plugin.acp.acp_backend.get_current_endpoint", side_effect=RuntimeError("bad")):
        with pytest.raises(RuntimeError, match="bad"):
            ClaudeBackend().get_env_vars()


def test_hermes_env_vars():
    with patch("plugin.acp.acp_backend.get_api_key_for_endpoint", return_value="sk-or-test"), \
         patch("plugin.acp.acp_backend.get_current_endpoint", return_value="https://openrouter.ai/api/v1"):
        assert HermesBackend().get_env_vars() == {"OPENROUTER_API_KEY": "sk-or-test"}

    with patch("plugin.acp.acp_backend.get_api_key_for_endpoint", return_value="sk-oai-test"), \
         patch("plugin.acp.acp_backend.get_current_endpoint", return_value="https://api.openai.com/v1"):
        assert HermesBackend().get_env_vars() == {"OPENAI_API_KEY": "sk-oai-test"}

    with patch("plugin.acp.acp_backend.get_api_key_for_endpoint") as mock_key, \
         patch("plugin.acp.acp_backend.get_current_endpoint", return_value="https://api.anthropic.com"):
        assert HermesBackend().get_env_vars() == {}
        mock_key.assert_not_called()

    with patch("plugin.acp.acp_backend.get_current_endpoint", side_effect=RuntimeError("config broken")):
        with pytest.raises(RuntimeError, match="config broken"):
            HermesBackend().get_env_vars()


def test_vibe_env_vars():
    with patch("plugin.acp.acp_backend.get_api_key_for_endpoint", return_value="sk-test"), \
         patch("plugin.acp.acp_backend.get_current_endpoint", return_value="https://api.mistral.ai"):
        assert VibeBackend().get_env_vars() == {"MISTRAL_API_KEY": "sk-test"}

    with patch("plugin.acp.acp_backend.get_api_key_for_endpoint", return_value=""), \
         patch("plugin.acp.acp_backend.get_current_endpoint", return_value=""):
        assert VibeBackend().get_env_vars() == {}

    with patch("plugin.acp.acp_backend.get_api_key_for_endpoint") as mock_key, \
         patch("plugin.acp.acp_backend.get_current_endpoint", return_value="https://api.openai.com/v1"):
        assert VibeBackend().get_env_vars() == {}
        mock_key.assert_not_called()

    with patch("plugin.acp.acp_backend.get_current_endpoint", side_effect=RuntimeError("config broken")):
        with pytest.raises(RuntimeError, match="config broken"):
            VibeBackend().get_env_vars()


def test_grok_and_opencode_env_vars():
    assert GrokBackend().get_env_vars() == {}
    assert OpenCodeBackend().get_env_vars() == {}


def test_antigravity_env_vars():
    assert AntigravityBackend().get_env_vars() == {}
    with patch(
        "plugin.acp.acp_backend.get_current_endpoint",
        side_effect=RuntimeError("should not be called"),
    ) as mock_endpoint:
        assert AntigravityBackend().get_env_vars() == {}
        mock_endpoint.assert_not_called()
