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
import unittest
from unittest.mock import MagicMock, patch

from plugin.framework.worker_pool import run_in_background

from plugin.modules.agent_backend.acp_connection import ACPConnection
from plugin.modules.agent_backend.hermes_proxy import (
    HermesBackend,
    _find_hermes_binary,
)


class TestFindHermesBinary(unittest.TestCase):
    """Test binary discovery."""

    @patch("shutil.which")
    def test_finds_hermes_acp_first(self, mock_which):
        mock_which.side_effect = lambda name: f"/usr/bin/{name}" if name == "hermes-acp" else None
        path, name = _find_hermes_binary()
        self.assertEqual(path, "/usr/bin/hermes-acp")
        self.assertEqual(name, "hermes-acp")

    @patch("shutil.which")
    def test_falls_back_to_hermes(self, mock_which):
        mock_which.side_effect = lambda name: "/usr/bin/hermes" if name == "hermes" else None
        path, name = _find_hermes_binary()
        self.assertEqual(path, "/usr/bin/hermes")
        self.assertEqual(name, "hermes")

    @patch("shutil.which", return_value=None)
    @patch("os.path.isfile", return_value=False)
    def test_returns_none_when_not_found(self, mock_isfile, mock_which):
        path, name = _find_hermes_binary()
        self.assertIsNone(path)
        self.assertIsNone(name)


class TestHermesBackendInit(unittest.TestCase):
    """Test backend initialization."""

    def test_backend_id(self):
        backend = HermesBackend()
        self.assertEqual(backend.backend_id, "hermes")
        self.assertEqual(backend.display_name, "Hermes")


class TestIsAvailable(unittest.TestCase):
    """Test availability check."""

    @patch("shutil.which", return_value="/usr/bin/hermes")
    def test_available_when_binary_in_path(self, mock_which):
        backend = HermesBackend()
        self.assertTrue(backend.is_available(None))

    @patch("shutil.which", return_value=None)
    @patch("os.path.isfile", return_value=False)
    def test_unavailable_when_no_binary(self, mock_isfile, mock_which):
        backend = HermesBackend()
        self.assertFalse(backend.is_available(None))


class TestACPConnection(unittest.TestCase):
    """Test the JSON-RPC connection logic."""

    def test_reader_parses_json_response(self):
        """Reader loop correctly parses a JSON-RPC response."""
        conn = ACPConnection(cmd_line=["/bin/echo"])

        # Simulate a response
        response = {"jsonrpc": "2.0", "id": 1, "result": {"session_id": "test-123"}}
        response_line = json.dumps(response) + "\n"

        # Set up pending request
        event = threading.Event()
        conn._pending[1] = {"event": event, "response": None}
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
        self.assertEqual(conn._pending.get(1, {}).get("response"), response)

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

        self.assertEqual(len(received), 1)
        self.assertEqual(received[0][0], "notifications/session")


class TestHandleNotifications(unittest.TestCase):
    """Test notification handling in the backend."""

    def test_text_update_produces_chunk(self):
        backend = HermesBackend()
        q = queue.Queue()
        backend._handle_notification(
            "notifications/session",
            {"update": {"session_update": "text", "text": "Hello world"}},
            q,
        )
        events = []
        while not q.empty():
            events.append(q.get_nowait())
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0], ("chunk", "Hello world"))

    def test_tool_call_in_progress_produces_thinking(self):
        backend = HermesBackend()
        q = queue.Queue()
        backend._handle_notification(
            "notifications/session",
            {"update": {
                "session_update": "tool_call",
                "status": "in_progress",
                "title": "read_file",
                "tool_call_id": "tc-1",
                "raw_input": {"path": "/tmp/test.txt"},
            }},
            q,
        )
        events = []
        while not q.empty():
            events.append(q.get_nowait())
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0][0], "thinking")
        self.assertIn("read_file", events[0][1])

    def test_tool_call_completed_produces_thinking(self):
        backend = HermesBackend()
        q = queue.Queue()
        backend._handle_notification(
            "notifications/session",
            {"update": {
                "session_update": "tool_call",
                "status": "completed",
                "title": "search",
                "tool_call_id": "tc-2",
                "raw_output": "Found 3 results",
            }},
            q,
        )
        events = []
        while not q.empty():
            events.append(q.get_nowait())
        self.assertEqual(len(events), 1)
        self.assertIn("Tool result", events[0][1])

    def test_plan_update_produces_thinking(self):
        backend = HermesBackend()
        q = queue.Queue()
        backend._handle_notification(
            "notifications/session",
            {"update": {
                "session_update": "plan",
                "entries": [
                    {"text": "Read the document", "done": True},
                    {"text": "Edit the section", "done": False},
                ],
            }},
            q,
        )
        events = []
        while not q.empty():
            events.append(q.get_nowait())
        self.assertEqual(len(events), 1)
        self.assertIn("Plan", events[0][1])
        self.assertIn("✓", events[0][1])
        self.assertIn("○", events[0][1])


class TestSend(unittest.TestCase):
    """Test the send method with mocked connection."""

    @patch("shutil.which", return_value="/usr/bin/hermes")
    def test_send_error_when_process_fails(self, mock_which):
        """send() should queue an error if connection fails."""
        backend = HermesBackend()
        backend._hermes_cmd = "/nonexistent/hermes"
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
        self.assertIn("error", types)


if __name__ == "__main__":
    unittest.main()
