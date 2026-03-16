# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
"""Tests for the Claude ACP backend adapter."""

import json
import os
import queue
import unittest
from unittest.mock import MagicMock, patch

from plugin.modules.acp.claude_proxy import (
    ClaudeBackend,
    _find_claude_binary,
)


class TestFindClaudeBinary(unittest.TestCase):
    """Test binary discovery."""

    @patch("shutil.which")
    def test_finds_rs_first(self, mock_which):
        mock_which.side_effect = lambda name: f"/usr/bin/{name}" if name == "claude-code-acp-rs" else None
        path = _find_claude_binary()
        self.assertEqual(path, "/usr/bin/claude-code-acp-rs")

    @patch("shutil.which")
    def test_falls_back_to_js(self, mock_which):
        mock_which.side_effect = lambda name: "/usr/bin/claude-code-acp" if name == "claude-code-acp" else None
        path = _find_claude_binary()
        self.assertEqual(path, "/usr/bin/claude-code-acp")

    @patch("shutil.which", return_value=None)
    @patch("os.path.isfile", return_value=False)
    def test_returns_none_when_not_found(self, mock_isfile, mock_which):
        path = _find_claude_binary()
        self.assertIsNone(path)


class TestClaudeBackendInit(unittest.TestCase):
    """Test backend initialization."""

    def test_backend_id(self):
        backend = ClaudeBackend()
        self.assertEqual(backend.backend_id, "claude")
        self.assertEqual(backend.display_name, "Claude Code (ACP)")


class TestIsAvailable(unittest.TestCase):
    """Test availability check."""

    @patch("shutil.which", return_value="/usr/bin/claude-code-acp")
    def test_available_when_binary_in_path(self, mock_which):
        backend = ClaudeBackend()
        self.assertTrue(backend.is_available(None))

    @patch("shutil.which", return_value=None)
    @patch("os.path.isfile", return_value=False)
    def test_unavailable_when_no_binary(self, mock_isfile, mock_which):
        backend = ClaudeBackend()
        self.assertFalse(backend.is_available(None))


if __name__ == "__main__":
    unittest.main()
