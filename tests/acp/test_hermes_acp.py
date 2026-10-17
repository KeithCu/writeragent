# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
"""Tests for the Hermes ACP backend adapter (stdio JSON-RPC transport)."""

import json
import queue
import threading
from unittest.mock import MagicMock, patch

import pytest

from plugin.framework.worker_pool import run_in_background

from plugin.chatbot.send_handlers import _agent_backend_label

from plugin.framework.stream_batch import StreamQueueKind
from plugin.acp.acp_connection import ACPConnection, _Pending
from plugin.acp.builtin import BuiltinBackend
from plugin.acp.hermes_simple import HermesBackend


class TestHermesBinaryDiscovery:
    """Test binary / identity hooks used by ACPBackend._find_binary()."""

    def test_binary_name_is_hermes(self):
        backend = HermesBackend()
        assert (backend.get_binary_name()) == ("hermes")

    def test_display_name(self):
        backend = HermesBackend()
        assert (backend.display_name) == ("Hermes")


class TestHermesBackendInit:
    """Test backend initialization."""

    def test_backend_id(self):
        backend = HermesBackend()
        assert (backend.backend_id) == ("hermes")
        assert (backend.display_name) == ("Hermes")


class TestIsAvailable:
    """Test availability check."""

    @patch("os.path.isfile", return_value=True)
    @patch("shutil.which", return_value="/usr/bin/hermes")
    def test_available_when_hermes_in_path(self, mock_which, mock_isfile):
        backend = HermesBackend()
        assert (backend.is_available(None))
        assert (backend._extra_args) == (["acp"])

    @patch("shutil.which", return_value=None)
    @patch("os.path.isfile", return_value=False)
    def test_unavailable_when_no_binary(self, mock_isfile, mock_which):
        backend = HermesBackend()
        assert not (backend.is_available(None))

    @patch("os.path.isfile", side_effect=lambda p: p == "/usr/bin/hermes")
    @patch(
        "shutil.which",
        side_effect=lambda name: "/usr/bin/hermes" if name == "hermes" else None,
    )
    def test_available_when_hermes_cli_in_path(self, mock_which, mock_isfile):
        """Official install uses `hermes` + `acp` subcommand."""
        backend = HermesBackend()
        assert (backend.is_available(None))
        assert (backend._binary_path) == ("/usr/bin/hermes")
        assert (backend._extra_args) == (["acp"])


class TestAgentBackendDisplayLabel:
    """The label is the class display_name, not AgentBackend's "Built-in"."""

    def test_label_builtin(self):
        assert (_agent_backend_label(BuiltinBackend(), "builtin")) == ("Built-in")

    def test_label_hermes(self):
        assert "display_name" in HermesBackend.__dict__
        assert (_agent_backend_label(HermesBackend(), "hermes")) == ("Hermes")


class TestACPConnection:
    """Test the JSON-RPC connection logic."""

    def test_reader_parses_json_response(self):
        """Reader loop correctly parses a JSON-RPC response."""
        conn = ACPConnection(cmd_line=["/bin/echo"])

        # Simulate a response
        response = {"jsonrpc": "2.0", "id": 1, "result": {"session_id": "test-123"}}
        response_line = json.dumps(response) + "\n"

        # Set up pending request
        event = threading.Event()
        conn._pending[1] = _Pending(event)
        conn._running = True

        # Create a mock proc with stdout
        mock_proc = MagicMock()
        mock_proc.poll.side_effect = [None, 0]  # alive first, then done
        mock_proc.stdout.readline.side_effect = [
            response_line.encode("utf-8"),
            b"",  # EOF
        ]
        mock_proc.stderr = MagicMock()
        mock_proc.stderr.read.return_value = b""
        conn._proc = mock_proc

        # Run reader in a thread briefly
        reader = run_in_background(conn._reader_loop, daemon=True)
        event.wait(timeout=2)
        conn._running = False
        reader.join(timeout=2)

        # Check the response was stored
        assert (conn._pending[1].response) == (response)

    def test_reader_dispatches_notifications(self):
        """Reader loop dispatches notifications to callback."""
        conn = ACPConnection(cmd_line=["/bin/echo"])

        received = []
        conn.set_notification_callback(lambda method, params, msg_id=None: received.append((method, params)))
        conn._running = True

        notification = {
            "jsonrpc": "2.0",
            "method": "notifications/session",
            "params": {"update": {"session_update": "text", "text": "Hello"}},
        }
        notification_line = json.dumps(notification) + "\n"

        mock_proc = MagicMock()
        mock_proc.poll.side_effect = [None, 0]
        mock_proc.stdout.readline.side_effect = [
            notification_line.encode("utf-8"),
            b"",
        ]
        mock_proc.stderr = MagicMock()
        mock_proc.stderr.read.return_value = b""
        conn._proc = mock_proc

        reader = run_in_background(conn._reader_loop, daemon=True)
        reader.join(timeout=2)
        conn._running = False

        assert (len(received)) == (1)
        assert (received[0][0]) == ("notifications/session")


class TestHandleAcpUpdate:
    """Test ACPBackend update handling (used by Hermes via ACP)."""

    def test_text_content_list_queues_chunk(self):
        backend = HermesBackend()
        q = queue.Queue()
        backend._handle_acp_update(
            {"content": [{"type": "text", "text": "Hello world"}]},
            q,
        )
        events = []
        while not q.empty():
            events.append(q.get_nowait())
        assert (len(events)) == (1)
        assert (events[0]) == ((StreamQueueKind.CHUNK, "Hello world"))

    def test_text_content_dict_queues_chunk(self):
        backend = HermesBackend()
        q = queue.Queue()
        backend._handle_acp_update(
            {"content": {"type": "text", "text": "Hello"}},
            q,
        )
        events = []
        while not q.empty():
            events.append(q.get_nowait())
        assert (events) == ([(StreamQueueKind.CHUNK, "Hello")])

    def test_tool_call_in_content_queues_tool_call(self):
        backend = HermesBackend()
        q = queue.Queue()
        item = {"type": "tool_call", "name": "read_file", "id": "tc-1"}
        backend._handle_acp_update({"content": [item]}, q)
        events = []
        while not q.empty():
            events.append(q.get_nowait())
        assert (len(events)) == (1)
        assert (events[0][0]) == (StreamQueueKind.TOOL_CALL)
        assert (events[0][1]) == (item)

    def test_tool_result_in_content_queues_tool_result(self):
        backend = HermesBackend()
        q = queue.Queue()
        item = {"type": "tool_result", "content": "Found 3 results"}
        backend._handle_acp_update({"content": [item]}, q)
        events = []
        while not q.empty():
            events.append(q.get_nowait())
        assert (len(events)) == (1)
        assert (events[0]) == ((StreamQueueKind.TOOL_RESULT, item))


class TestSend:
    """Test the send method with mocked connection."""

    @patch("shutil.which", return_value="/usr/bin/hermes")
    def test_send_error_when_process_fails(self, mock_which):
        """send() should queue an error if connection fails."""
        backend = HermesBackend()
        backend._binary_path = "/nonexistent/hermes"
        q = queue.Queue()

        # Mock _ensure_connection to raise
        backend._ensure_connection = MagicMock(side_effect=RuntimeError("spawn failed"))

        backend.send(
            queue=q,
            user_message="test",
            document_context=None,
            document_url=None,
        )

        events = []
        while not q.empty():
            events.append(q.get_nowait())

        types = [e[0] for e in events]
        assert (StreamQueueKind.ERROR) in (types)


class TestHermesEnvVars:
    """Forward OPENROUTER_API_KEY or OPENAI_API_KEY from general settings when matched."""

    @patch("plugin.acp.hermes_simple.get_api_key_for_endpoint", return_value="sk-or-test")
    @patch("plugin.acp.hermes_simple.get_current_endpoint", return_value="https://openrouter.ai/api/v1")
    def test_forwards_openrouter_api_key(self, mock_endpoint, mock_key):
        env = HermesBackend().get_env_vars()
        assert env == {"OPENROUTER_API_KEY": "sk-or-test"}

    @patch("plugin.acp.hermes_simple.get_api_key_for_endpoint", return_value="sk-oai-test")
    @patch("plugin.acp.hermes_simple.get_current_endpoint", return_value="https://api.openai.com/v1")
    def test_forwards_openai_api_key(self, mock_endpoint, mock_key):
        env = HermesBackend().get_env_vars()
        assert env == {"OPENAI_API_KEY": "sk-oai-test"}

    @patch("plugin.acp.hermes_simple.get_api_key_for_endpoint")
    @patch("plugin.acp.hermes_simple.get_current_endpoint", return_value="https://api.anthropic.com")
    def test_other_provider_returns_empty(self, mock_endpoint, mock_key):
        assert HermesBackend().get_env_vars() == {}
        mock_key.assert_not_called()

    @patch("plugin.acp.hermes_simple.get_current_endpoint", side_effect=RuntimeError("config broken"))
    def test_runtime_error_propagates(self, mock_endpoint):
        with pytest.raises(RuntimeError, match="config broken"):
            HermesBackend().get_env_vars()


