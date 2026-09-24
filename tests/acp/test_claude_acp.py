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


