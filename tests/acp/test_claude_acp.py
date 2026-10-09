# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
"""Tests for the Claude ACP backend adapter."""

from unittest.mock import patch

from plugin.acp.claude_simple import (
    ClaudeBackend,
)


class TestClaudeBinaryDiscovery:
    """Test binary discovery through backend methods."""

    def test_binary_name_is_correct(self):
        """Test that the backend returns the correct binary name."""
        backend = ClaudeBackend()
        assert (backend.get_binary_name()) == ("claude-code-acp-rs")

    def test_display_name_is_correct(self):
        """Test that the backend returns the correct display name."""
        backend = ClaudeBackend()
        assert (backend.get_display_name()) == ("Claude Code (ACP)")


class TestClaudeBackendInit:
    """Test backend initialization."""

    def test_backend_id(self):
        backend = ClaudeBackend()
        assert (backend.backend_id) == ("claude")
        assert (backend.get_display_name()) == ("Claude Code (ACP)")


class TestIsAvailable:
    """Test availability check."""

    @patch("shutil.which", return_value="/usr/bin/claude-code-acp")
    def test_available_when_binary_in_path(self, mock_which):
        backend = ClaudeBackend()
        assert (backend.is_available(None))

    @patch("shutil.which", return_value=None)
    @patch("os.path.isfile", return_value=False)
    def test_unavailable_when_no_binary(self, mock_isfile, mock_which):
        backend = ClaudeBackend()
        assert not (backend.is_available(None))


class TestClaudeEnvVars:
    """Only an Anthropic endpoint key is exported as ANTHROPIC_API_KEY."""

    @patch("plugin.acp.claude_simple.get_provider_from_endpoint", return_value="anthropic")
    @patch("plugin.acp.claude_simple.get_api_key_for_endpoint", return_value="sk-ant")
    @patch("plugin.acp.claude_simple.get_current_endpoint", return_value="https://api.anthropic.com")
    def test_forwards_anthropic_key(self, mock_endpoint, mock_key, mock_provider):
        assert (ClaudeBackend().get_env_vars()) == ({"ANTHROPIC_API_KEY": "sk-ant"})

    @patch("plugin.acp.claude_simple.get_provider_from_endpoint", return_value="openai")
    @patch("plugin.acp.claude_simple.get_api_key_for_endpoint", return_value="sk-oai")
    @patch("plugin.acp.claude_simple.get_current_endpoint", return_value="https://api.openai.com/v1")
    def test_does_not_forward_other_provider_key(self, mock_endpoint, mock_key, mock_provider):
        assert (ClaudeBackend().get_env_vars()) == ({})
        mock_key.assert_not_called()

    @patch("plugin.acp.claude_simple.get_current_endpoint", side_effect=RuntimeError("bad"))
    def test_config_error_returns_empty(self, mock_endpoint, caplog):
        caplog.set_level("ERROR", logger="plugin.acp.claude_simple")
        assert (ClaudeBackend().get_env_vars()) == ({})
        assert ("Failed to read API key for Claude ACP") in (caplog.text)


