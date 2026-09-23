# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
"""Tests for the OpenCode ACP backend adapter."""

import unittest
from unittest.mock import patch

from plugin.chatbot.send_handlers import _agent_backend_label
from plugin.acp.opencode_simple import OpenCodeBackend


class TestOpenCodeBinaryDiscovery(unittest.TestCase):
    """Test binary / identity hooks used by ACPBackend._find_binary()."""

    def test_binary_name_is_opencode(self):
        backend = OpenCodeBackend()
        self.assertEqual(backend.get_binary_name(), "opencode")

    def test_display_name(self):
        backend = OpenCodeBackend()
        self.assertEqual(backend.get_display_name(), "OpenCode (ACP)")

    def test_agent_name(self):
        backend = OpenCodeBackend()
        self.assertEqual(backend.get_agent_name(), "opencode")


class TestOpenCodeBackendInit(unittest.TestCase):
    """Test backend initialization."""

    def test_backend_id(self):
        backend = OpenCodeBackend()
        self.assertEqual(backend.backend_id, "opencode")
        self.assertEqual(backend.get_display_name(), "OpenCode (ACP)")


class TestIsAvailable(unittest.TestCase):
    """Test availability check."""

    @patch("os.path.isfile", return_value=True)
    @patch("shutil.which", return_value="/usr/bin/opencode")
    def test_available_when_opencode_in_path(self, mock_which, mock_isfile):
        backend = OpenCodeBackend()
        self.assertTrue(backend.is_available(None))
        self.assertEqual(backend._extra_args, ["acp"])

    @patch("shutil.which", return_value=None)
    @patch("os.path.isfile", return_value=False)
    def test_unavailable_when_no_binary(self, mock_isfile, mock_which):
        backend = OpenCodeBackend()
        self.assertFalse(backend.is_available(None))

    @patch("os.path.isfile", side_effect=lambda p: p == "/usr/bin/opencode")
    @patch(
        "shutil.which",
        side_effect=lambda name: "/usr/bin/opencode" if name == "opencode" else None,
    )
    def test_available_when_opencode_cli_in_path(self, mock_which, mock_isfile):
        """Official install uses `opencode acp`."""
        backend = OpenCodeBackend()
        self.assertTrue(backend.is_available(None))
        self.assertEqual(backend._binary_path, "/usr/bin/opencode")
        self.assertEqual(backend._extra_args, ["acp"])


class TestOpenCodeEnvVars(unittest.TestCase):
    """Auth is via OpenCode login / auth.json; no WriterAgent key forwarding."""

    def test_get_env_vars_empty(self):
        backend = OpenCodeBackend()
        self.assertEqual(backend.get_env_vars(), {})


class TestAgentBackendDisplayLabel(unittest.TestCase):
    """Error messages must use get_display_name(), not inherited display_name."""

    def test_label_opencode(self):
        self.assertEqual(_agent_backend_label(OpenCodeBackend(), "opencode"), "OpenCode (ACP)")


if __name__ == "__main__":
    unittest.main()
