# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
"""Tests for shared ACPBackend default-args and notification handling."""

import os
import queue
import threading
from unittest.mock import MagicMock, patch

from plugin.acp.acp_backend import ACPBackend
from plugin.acp.claude_simple import ClaudeBackend
from plugin.acp.grok_simple import GrokBackend
from plugin.acp.hermes_simple import HermesBackend
from plugin.acp.opencode_simple import OpenCodeBackend
from plugin.framework.async_stream import StreamQueueKind


def _config_get(path="", args=""):
    def getter(key, default=None):
        values = {
            "agent_backend.path": path,
            "agent_backend.args": args,
        }
        return values.get(key, default)

    return getter


class TestDefaultExtraArgs:
    """Default CLI args apply only when settings args are empty and the basename matches."""

    def test_hermes_defaults_when_settings_args_empty(self):
        with (
            patch("plugin.framework.config.get_config", side_effect=_config_get(path="/usr/bin/hermes", args="")),
            patch("os.path.isfile", return_value=True),
            patch("shutil.which", return_value=None),
        ):
            backend = HermesBackend()
        assert (backend._binary_path) == ("/usr/bin/hermes")
        assert (backend._extra_args) == (["acp"])

    def test_hermes_settings_args_win(self):
        with (
            patch("plugin.framework.config.get_config", side_effect=_config_get(path="/usr/bin/hermes", args="--keep-going")),
            patch("os.path.isfile", return_value=True),
            patch("shutil.which", return_value=None),
        ):
            backend = HermesBackend()
        assert (backend._extra_args) == (["--keep-going"])

    def test_hermes_unrelated_basename_skips_defaults(self):
        with (
            patch("plugin.framework.config.get_config", side_effect=_config_get(path="/opt/my-wrapper", args="")),
            patch("os.path.isfile", return_value=True),
            patch("shutil.which", return_value=None),
        ):
            backend = HermesBackend()
        assert (backend._binary_path) == ("/opt/my-wrapper")
        assert (backend._extra_args) == ([])

    def test_opencode_defaults_when_settings_args_empty(self):
        with (
            patch("plugin.framework.config.get_config", side_effect=_config_get(path="/usr/bin/opencode", args="")),
            patch("os.path.isfile", return_value=True),
            patch("shutil.which", return_value=None),
        ):
            backend = OpenCodeBackend()
        assert (backend._extra_args) == (["acp"])

    def test_opencode_settings_args_win(self):
        with (
            patch("plugin.framework.config.get_config", side_effect=_config_get(path="/usr/bin/opencode", args="serve --port 4096")),
            patch("os.path.isfile", return_value=True),
            patch("shutil.which", return_value=None),
        ):
            backend = OpenCodeBackend()
        assert (backend._extra_args) == (["serve", "--port", "4096"])

    def test_grok_defaults_when_settings_args_empty(self):
        with (
            patch("plugin.framework.config.get_config", side_effect=_config_get(path="/usr/bin/grok", args="")),
            patch("os.path.isfile", return_value=True),
            patch("shutil.which", return_value=None),
        ):
            backend = GrokBackend()
        assert (backend._extra_args) == (["--no-auto-update", "agent", "stdio"])

    def test_grok_prefix_basename_gets_defaults(self):
        with (
            patch("plugin.framework.config.get_config", side_effect=_config_get(path="/usr/local/bin/grok-cli", args="")),
            patch("os.path.isfile", return_value=True),
            patch("shutil.which", return_value=None),
        ):
            backend = GrokBackend()
        assert (backend._binary_path) == ("/usr/local/bin/grok-cli")
        assert (backend._extra_args) == (["--no-auto-update", "agent", "stdio"])

    def test_grok_settings_args_win(self):
        with (
            patch("plugin.framework.config.get_config", side_effect=_config_get(path="/usr/bin/grok", args="agent stdio")),
            patch("os.path.isfile", return_value=True),
            patch("shutil.which", return_value=None),
        ):
            backend = GrokBackend()
        assert (backend._extra_args) == (["agent", "stdio"])

    def test_claude_has_no_default_extra_args(self):
        with (
            patch("plugin.framework.config.get_config", side_effect=_config_get(path="/usr/bin/claude-code-acp-rs", args="")),
            patch("os.path.isfile", return_value=True),
            patch("shutil.which", return_value=None),
        ):
            backend = ClaudeBackend()
        assert (backend._extra_args) == ([])
        assert (backend.get_default_extra_args()) == ([])

    def test_default_extra_args_are_immutable_tuples(self):
        assert isinstance(HermesBackend.default_extra_args, tuple)
        assert isinstance(OpenCodeBackend.default_extra_args, tuple)
        assert isinstance(GrokBackend.default_extra_args, tuple)
        assert isinstance(ACPBackend.default_extra_args, tuple)
        copied = HermesBackend.get_default_extra_args(HermesBackend.__new__(HermesBackend))
        copied.append("mutated")
        assert (HermesBackend.default_extra_args) == (("acp",))


class TestBinaryDiscovery:
    """Config path and PATH / home-dir fallback still resolve the binary."""

    def test_config_path_used_when_file_exists(self):
        with (
            patch("plugin.framework.config.get_config", side_effect=_config_get(path="/custom/bin/hermes", args="")),
            patch("os.path.isfile", return_value=True),
            patch("shutil.which", return_value="/usr/bin/hermes"),
        ):
            backend = HermesBackend()
        assert (backend._binary_path) == ("/custom/bin/hermes")
        assert (backend._extra_args) == (["acp"])

    def test_which_used_when_config_path_empty(self):
        with (
            patch("plugin.framework.config.get_config", side_effect=_config_get(path="", args="")),
            patch("os.path.isfile", return_value=True),
            patch("shutil.which", return_value="/usr/bin/opencode"),
        ):
            backend = OpenCodeBackend()
        assert (backend._binary_path) == ("/usr/bin/opencode")
        assert (backend._extra_args) == (["acp"])

    def test_home_local_bin_used_when_not_on_path(self):
        home_bin = os.path.join(os.path.expanduser("~"), ".local", "bin", "hermes")

        def isfile(path):
            return path == home_bin

        def access(path, mode):
            return path == home_bin

        with (
            patch("plugin.framework.config.get_config", side_effect=_config_get(path="", args="")),
            patch("os.path.isfile", side_effect=isfile),
            patch("os.access", side_effect=access),
            patch("shutil.which", return_value=None),
        ):
            backend = HermesBackend()
        assert (backend._binary_path) == (home_bin)
        assert (backend._extra_args) == (["acp"])

    def test_is_available_path_fallback_applies_defaults(self):
        with (
            patch("plugin.framework.config.get_config", side_effect=_config_get(path="", args="")),
            patch("os.path.isfile", return_value=False),
            patch("os.access", return_value=False),
            patch("shutil.which", return_value=None),
        ):
            backend = HermesBackend()
        assert (backend._binary_path) is None
        with (
            patch("plugin.framework.config.get_config", side_effect=_config_get(path="", args="")),
            patch("os.path.isfile", return_value=False),
            patch("shutil.which", return_value="/opt/path/hermes"),
        ):
            assert (backend.is_available(None))
        assert (backend._binary_path) == ("/opt/path/hermes")
        assert (backend._extra_args) == (["acp"])


class TestMergedUpdateHandler:
    """Session and agent updates share one helper for list and dict content."""

    def setup_method(self):
        self.backend = ACPBackend.__new__(ACPBackend)

    def _events(self, handler, update):
        q = queue.Queue()
        handler(update, q)
        events = []
        while not q.empty():
            events.append(q.get_nowait())
        return events

    def test_list_content_queues_chunk_tool_call_tool_result(self):
        tool_call = {"type": "tool_call", "name": "read_file", "id": "tc-1"}
        tool_result = {"type": "tool_result", "content": "ok"}
        update = {
            "content": [
                {"type": "text", "text": "Hello"},
                tool_call,
                tool_result,
            ]
        }
        expected = [
            (StreamQueueKind.CHUNK, "Hello"),
            (StreamQueueKind.TOOL_CALL, tool_call),
            (StreamQueueKind.TOOL_RESULT, tool_result),
        ]
        assert (self._events(self.backend._handle_acp_update, update)) == (expected)

    def test_dict_content_text(self):
        update = {"content": {"type": "text", "text": "Hi"}}
        assert (self._events(self.backend._handle_acp_update, update)) == ([(StreamQueueKind.CHUNK, "Hi")])

    def test_dict_content_tool_call(self):
        item = {"type": "tool_call", "name": "search"}
        update = {"content": item}
        assert (self._events(self.backend._handle_acp_update, update)) == ([(StreamQueueKind.TOOL_CALL, item)])

    def test_dict_content_tool_result(self):
        item = {"type": "tool_result", "content": "done"}
        update = {"content": item}
        assert (self._events(self.backend._handle_acp_update, update)) == ([(StreamQueueKind.TOOL_RESULT, item)])

    def test_missing_or_unknown_content_is_noop(self):
        assert (self._events(self.backend._handle_acp_update, {"keys": "only"})) == ([])
        assert (self._events(self.backend._handle_acp_update, {"content": "plain"})) == ([])
        assert (self._events(self.backend._handle_acp_update, None)) == ([])


class TestPromptResultContentBlocks:
    """Base send() drains prompt-result contentBlocks (Vibe and any ACP agent)."""

    def test_content_blocks_are_queued(self):
        from unittest.mock import MagicMock

        backend = ACPBackend.__new__(ACPBackend)
        backend._stop_requested = False
        backend._prompt_done = __import__("threading").Event()
        backend._permission_lock = threading.Lock()
        backend._pending_permissions = {}
        backend._session_id = "sess-1"
        backend._ensure_connection = MagicMock()
        backend._ensure_session = MagicMock()
        backend.get_display_name = MagicMock(return_value="Test")
        tool_call = {"type": "tool_call", "name": "read"}
        tool_result = {"type": "tool_result", "content": "ok"}
        mock_conn = MagicMock()
        mock_conn.send_request.return_value = {
            "stopReason": "end_turn",
            "contentBlocks": [
                {"type": "text", "text": "Hello from ACP"},
                tool_call,
                tool_result,
            ],
        }
        backend._conn = mock_conn
        q = queue.Queue()
        backend.send(queue=q, user_message="hi", document_context=None, document_url=None)
        events = []
        while not q.empty():
            events.append(q.get_nowait())
        assert ((StreamQueueKind.CHUNK, "Hello from ACP")) in (events)
        assert ((StreamQueueKind.TOOL_CALL, tool_call)) in (events)
        assert ((StreamQueueKind.TOOL_RESULT, tool_result)) in (events)
        assert ((StreamQueueKind.STREAM_DONE, None)) in (events)
        # The turn owns the process. Success still has to stop it.
        mock_conn.stop.assert_called()
        assert (backend._conn) is None
        assert (backend._session_id) is None


def _bare_backend():
    """ACPBackend without config or a live process."""
    backend = ACPBackend.__new__(ACPBackend)
    backend._stop_requested = False
    backend._prompt_done = threading.Event()
    backend._session_id = None
    backend._conn = None
    backend._permission_lock = threading.Lock()
    backend._pending_permissions = {}
    backend.get_display_name = lambda: "Test"
    backend.get_binary_name = lambda: "test-agent"
    return backend


def _drain(q):
    events = []
    while not q.empty():
        events.append(q.get_nowait())
    return events


_PERMISSION_OPTIONS = [
    {"optionId": "allow-always", "name": "Always allow", "kind": "allow_always"},
    {"optionId": "allow-once", "name": "Allow once", "kind": "allow_once"},
    {"optionId": "reject-always", "name": "Reject always", "kind": "reject_always"},
    {"optionId": "reject-once", "name": "Reject", "kind": "reject_once"},
]


class TestSessionUpdateDiscriminator:
    """Spec session/update uses sessionUpdate, not a private content list."""

    def setup_method(self):
        self.backend = ACPBackend.__new__(ACPBackend)

    def _events(self, update):
        q = queue.Queue()
        self.backend._handle_acp_update(update, q)
        return _drain(q)

    def test_agent_message_chunk_is_answer_text(self):
        update = {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "Hello"}}
        assert (self._events(update)) == ([(StreamQueueKind.CHUNK, "Hello")])

    def test_agent_thought_chunk_is_thinking_not_answer(self):
        update = {"sessionUpdate": "agent_thought_chunk", "content": {"type": "text", "text": "Let me think"}}
        events = self._events(update)
        assert (events) == ([(StreamQueueKind.THINKING, "Let me think")])
        assert ((StreamQueueKind.CHUNK, "Let me think")) not in (events)

    def test_user_message_chunk_is_not_saved_as_answer(self):
        update = {"sessionUpdate": "user_message_chunk", "content": {"type": "text", "text": "echo"}}
        assert (self._events(update)) == ([])

    def test_tool_call_is_queued(self):
        update = {"sessionUpdate": "tool_call", "toolCallId": "call_001", "title": "Analyzing Python code", "kind": "other", "status": "pending"}
        events = self._events(update)
        assert (len(events)) == (1)
        assert (events[0][0]) == (StreamQueueKind.TOOL_CALL)
        assert (events[0][1]["id"]) == ("call_001")
        assert (events[0][1]["title"]) == ("Analyzing Python code")
        assert (events[0][1]["status"]) == ("pending")

    def test_tool_call_update_in_progress_is_tool_activity(self):
        update = {"sessionUpdate": "tool_call_update", "toolCallId": "call_001", "status": "in_progress"}
        events = self._events(update)
        assert (events[0][0]) == (StreamQueueKind.TOOL_CALL)
        assert (events[0][1]["status"]) == ("in_progress")

    def test_tool_call_update_completed_is_tool_result(self):
        update = {
            "sessionUpdate": "tool_call_update",
            "toolCallId": "call_001",
            "status": "completed",
            "content": [{"type": "content", "content": {"type": "text", "text": "done"}}],
        }
        events = self._events(update)
        assert (events[0][0]) == (StreamQueueKind.TOOL_RESULT)
        assert (events[0][1]["id"]) == ("call_001")
        assert (events[0][1]["content"][0]["content"]["text"]) == ("done")

    def test_unrelated_session_update_is_not_merged_into_answer(self):
        update = {"sessionUpdate": "usage_update", "used": 10, "size": 100, "content": {"type": "text", "text": "nope"}}
        assert (self._events(update)) == ([])


class TestPermissionResult:
    """HITL Approve/Reject must be an ACP outcome, not {approved: bool}."""

    def _request(self, backend, msg_id=5):
        params = {
            "description": "NOT THIS",
            "sessionId": "sess-1",
            "toolCall": {"toolCallId": "call_9", "title": "Edit main.py", "kind": "edit"},
            "options": _PERMISSION_OPTIONS,
        }
        q = queue.Queue()
        backend._dispatch_notification("session/request_permission", params, msg_id, q)
        return _drain(q)

    def test_description_comes_from_title_and_option_names(self):
        backend = _bare_backend()
        events = self._request(backend)
        assert (len(events)) == (1)
        kind, description, tool_name, tool_call, msg_id = events[0]
        assert (kind) == (StreamQueueKind.APPROVAL_REQUIRED)
        assert (description) == ("Edit main.py\nAlways allow\nAllow once\nReject always\nReject")
        assert ("NOT THIS") not in (description)
        assert (tool_name) == ("edit")
        assert (tool_call["title"]) == ("Edit main.py")
        assert (msg_id) == (5)

    def test_approve_selects_allow_once(self):
        backend = _bare_backend()
        conn = MagicMock()
        conn.is_alive = True
        backend._conn = conn
        self._request(backend, msg_id=5)
        backend.submit_approval(5, True)
        conn.send_response.assert_called_once_with(5, result={"outcome": {"outcome": "selected", "optionId": "allow-once"}})

    def test_reject_selects_reject_once(self):
        backend = _bare_backend()
        conn = MagicMock()
        conn.is_alive = True
        backend._conn = conn
        self._request(backend, msg_id=5)
        backend.submit_approval(5, False)
        conn.send_response.assert_called_once_with(5, result={"outcome": {"outcome": "selected", "optionId": "reject-once"}})

    def test_reject_without_reject_option_is_cancelled(self):
        backend = _bare_backend()
        conn = MagicMock()
        conn.is_alive = True
        backend._conn = conn
        q = queue.Queue()
        backend._dispatch_notification(
            "session/request_permission",
            {"toolCall": {"toolCallId": "c", "title": "Run"}, "options": [{"optionId": "allow-once", "name": "Allow once", "kind": "allow_once"}]},
            3,
            q,
        )
        backend.submit_approval(3, False)
        conn.send_response.assert_called_once_with(3, result={"outcome": {"outcome": "cancelled"}})

    def test_permission_after_stop_is_cancelled_without_dialog(self):
        backend = _bare_backend()
        backend._stop_requested = True
        conn = MagicMock()
        conn.is_alive = True
        backend._conn = conn
        q = queue.Queue()
        backend._dispatch_notification("session/request_permission", {"toolCall": {"title": "Edit main.py", "kind": "edit", "toolCallId": "call_1"}, "options": _PERMISSION_OPTIONS}, 8, q)
        assert (_drain(q)) == ([])
        conn.send_response.assert_called_once_with(8, result={"outcome": {"outcome": "cancelled"}})
        assert (backend._pending_permissions) == ({})

    def test_second_answer_is_not_sent(self):
        backend = _bare_backend()
        conn = MagicMock()
        conn.is_alive = True
        backend._conn = conn
        self._request(backend, msg_id=5)
        backend.submit_approval(5, True)
        backend.submit_approval(5, False)
        assert (conn.send_response.call_count) == (1)


class TestStopAndShutdown:
    """Stop cancels the ACP turn; every send stops the subprocess."""

    def test_stop_cancels_session_permission_and_process(self):
        backend = _bare_backend()
        backend._session_id = "sess-1"
        conn = MagicMock()
        conn.is_alive = True
        backend._conn = conn
        q = queue.Queue()
        backend._dispatch_notification(
            "session/request_permission",
            {"toolCall": {"title": "Edit main.py", "kind": "edit", "toolCallId": "call_1"}, "options": _PERMISSION_OPTIONS},
            7,
            q,
        )
        backend.stop()
        conn.send_notification.assert_called_once_with("session/cancel", {"sessionId": "sess-1"})
        conn.send_response.assert_called_once_with(7, result={"outcome": {"outcome": "cancelled"}})
        assert ("session/interrupt") not in (str(conn.send_notification.call_args_list))
        conn.stop.assert_called()
        assert (backend._conn) is None
        assert (backend._stop_requested) is True
        # The dialog lost the race: Stop already answered.
        backend.submit_approval(7, True)
        assert (conn.send_response.call_count) == (1)

    def test_send_shuts_down_after_success(self):
        backend = _bare_backend()
        backend._session_id = "sess-1"
        conn = MagicMock()
        conn.is_alive = True
        conn.send_request.return_value = {"stopReason": "end_turn"}
        backend._conn = conn
        backend._ensure_connection = lambda: None
        backend._ensure_session = lambda **kwargs: None
        q = queue.Queue()
        backend.send(queue=q, user_message="hi", document_context=None, document_url=None)
        assert ((StreamQueueKind.STREAM_DONE, None)) in (_drain(q))
        conn.stop.assert_called()
        conn.send_notification.assert_not_called()
        assert (backend._conn) is None
        assert (backend._session_id) is None

    def test_send_shuts_down_when_session_creation_fails(self):
        backend = _bare_backend()
        conn = MagicMock()
        conn.is_alive = True

        def ensure_connection():
            backend._conn = conn

        backend._ensure_connection = ensure_connection
        backend._ensure_session = MagicMock(side_effect=RuntimeError("no session"))
        q = queue.Queue()
        backend.send(queue=q, user_message="hi", document_context=None, document_url=None)
        kinds = [event[0] for event in _drain(q)]
        assert (StreamQueueKind.ERROR) in (kinds)
        conn.stop.assert_called()
        assert (backend._conn) is None

    def test_stop_during_prompt_unblocks_and_reports_stopped(self):
        backend = _bare_backend()
        backend._session_id = "sess-9"
        conn = MagicMock()
        conn.is_alive = True
        started = threading.Event()

        def send_request(method, params=None, timeout=120):
            if method == "session/prompt":
                started.set()
                if not backend._prompt_done.wait(timeout=2):
                    raise TimeoutError("prompt still blocked")
                raise RuntimeError("ACP process stopped")
            return {"sessionId": "sess-9"}

        conn.send_request.side_effect = send_request
        backend._conn = conn
        backend._ensure_connection = lambda: None
        backend._ensure_session = lambda **kwargs: None
        backend._pending_permissions[4] = [{"optionId": "allow-once", "name": "Allow once", "kind": "allow_once"}]
        q = queue.Queue()
        worker = threading.Thread(target=lambda: backend.send(queue=q, user_message="hi", document_context=None, document_url=None))
        worker.start()
        assert (started.wait(timeout=2))
        backend.stop()
        worker.join(timeout=2)
        assert (worker.is_alive()) is False
        conn.send_notification.assert_called_with("session/cancel", {"sessionId": "sess-9"})
        conn.send_response.assert_any_call(4, result={"outcome": {"outcome": "cancelled"}})
        conn.stop.assert_called()
        assert ((StreamQueueKind.STOPPED, None)) in (_drain(q))
        assert (backend._conn) is None

    def test_stop_before_send_does_not_start_cli(self):
        backend = _bare_backend()
        backend.stop()
        backend._ensure_connection = MagicMock(side_effect=AssertionError("cli started"))
        backend._ensure_session = MagicMock(side_effect=AssertionError("session started"))
        q = queue.Queue()
        backend.send(queue=q, user_message="hi", document_context=None, document_url=None)
        assert (_drain(q)) == ([(StreamQueueKind.STOPPED, None)])
        backend._ensure_connection.assert_not_called()
        backend._ensure_session.assert_not_called()
        assert (backend._stop_requested) is True

    def test_stop_checker_true_at_send_entry_does_not_start_cli(self):
        backend = _bare_backend()
        backend._ensure_connection = MagicMock(side_effect=AssertionError("cli started"))
        backend._ensure_session = MagicMock(side_effect=AssertionError("session started"))
        q = queue.Queue()
        backend.send(queue=q, user_message="hi", document_context=None, document_url=None, stop_checker=lambda: True)
        assert (_drain(q)) == ([(StreamQueueKind.STOPPED, None)])
        backend._ensure_connection.assert_not_called()
        backend._ensure_session.assert_not_called()
        assert (backend._stop_requested) is True


