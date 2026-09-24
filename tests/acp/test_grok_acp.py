# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
"""Tests for the Grok Build ACP backend adapter."""

from unittest.mock import patch

from plugin.chatbot.send_handlers import _agent_backend_label
from plugin.acp.grok_simple import GrokBackend


class TestGrokBinaryDiscovery:
    """Test binary / identity hooks used by ACPBackend._find_binary()."""

    def test_binary_name_is_grok(self):
        backend = GrokBackend()
        assert (backend.get_binary_name()) == ("grok")

    def test_display_name(self):
        backend = GrokBackend()
        assert (backend.get_display_name()) == ("Grok Build (ACP)")

    def test_agent_name(self):
        backend = GrokBackend()
        assert (backend.get_agent_name()) == ("grok")


class TestGrokBackendInit:
    """Test backend initialization."""

    def test_backend_id(self):
        backend = GrokBackend()
        assert (backend.backend_id) == ("grok")
        assert (backend.get_display_name()) == ("Grok Build (ACP)")


class TestIsAvailable:
    """Test availability check."""

    @patch("os.path.isfile", return_value=True)
    @patch("shutil.which", return_value="/usr/bin/grok")
    def test_available_when_grok_in_path(self, mock_which, mock_isfile):
        backend = GrokBackend()
        assert (backend.is_available(None))
        assert (backend._extra_args) == (["--no-auto-update", "agent", "stdio"])

    @patch("shutil.which", return_value=None)
    @patch("os.path.isfile", return_value=False)
    def test_unavailable_when_no_binary(self, mock_isfile, mock_which):
        backend = GrokBackend()
        assert not (backend.is_available(None))

    @patch("os.path.isfile", side_effect=lambda p: p == "/usr/bin/grok")
    @patch(
        "shutil.which",
        side_effect=lambda name: "/usr/bin/grok" if name == "grok" else None,
    )
    def test_available_when_grok_cli_in_path(self, mock_which, mock_isfile):
        """Official install uses `grok --no-auto-update agent stdio`."""
        backend = GrokBackend()
        assert (backend.is_available(None))
        assert (backend._binary_path) == ("/usr/bin/grok")
        assert (backend._extra_args) == (["--no-auto-update", "agent", "stdio"])


class TestGrokEnvVars:
    """Auth is via grok login (~/.grok/auth.json); no WriterAgent key forwarding."""

    def test_get_env_vars_empty(self):
        backend = GrokBackend()
        assert (backend.get_env_vars()) == ({})


class TestAgentBackendDisplayLabel:
    """Error messages must use get_display_name(), not inherited display_name."""

    def test_label_grok(self):
        assert (_agent_backend_label(GrokBackend(), "grok")) == ("Grok Build (ACP)")


