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
import time
from unittest.mock import MagicMock, patch

import pytest

from plugin.acp.acp_backend import ACPBackend, _TurnStopped, _split_cli_args, _tool_stream_kind
from plugin.acp.acp_connection import ACPTimeoutError
from plugin.acp.registry import ClaudeBackend, GrokBackend, HermesBackend, OpenCodeBackend
from plugin.framework.stream_batch import StreamQueueKind
from plugin.version import EXTENSION_VERSION


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

    def test_quoted_settings_arg_stays_one_entry(self):
        with (
            patch("plugin.framework.config.get_config", side_effect=_config_get(path="/usr/bin/hermes", args='--config "/tmp/my file.json"')),
            patch("os.path.isfile", return_value=True),
            patch("shutil.which", return_value=None),
        ):
            backend = HermesBackend()
        assert (len(backend._extra_args)) == (2)
        assert (backend._extra_args[0]) == ("--config")
        assert ("my file.json") in (backend._extra_args[1])

    def test_windows_split_strips_quotes_and_keeps_backslashes(self):
        """posix=False keeps \\ , then one wrapping quote pair is removed.

        posix=True would turn C:\\new\\tmp into C:newtmp. Popen must not see
        the quote characters either.
        """
        assert _split_cli_args('"C:\\Program Files\\x"', posix=False) == ["C:\\Program Files\\x"]
        assert _split_cli_args("C:\\new\\tmp", posix=False) == ["C:\\new\\tmp"]
        assert _split_cli_args('--config "C:\\Program Files\\x"', posix=False) == ["--config", "C:\\Program Files\\x"]
        assert _split_cli_args("C:\\new\\tmp", posix=True) == ["C:newtmp"]
        assert _split_cli_args('--config="C:\\Program Files\\x"', posix=False) == ["--config=C:\\Program Files\\x"]
        assert _split_cli_args('--config="C:\\Program Files\\x" --other', posix=False) == ["--config=C:\\Program Files\\x", "--other"]
        with pytest.raises(ValueError, match="No closing quotation"):
            _split_cli_args('--config="C:\\unmatched', posix=False)

    def test_config_read_error_propagates(self):
        """get_config does not raise in production. A raised error must not search PATH."""
        backend = HermesBackend.__new__(HermesBackend)
        backend._extra_args = ["--stale"]
        backend._binary_path = "/usr/bin/hermes"
        with (
            patch("plugin.framework.config.get_config", side_effect=RuntimeError("bad config")),
            patch("shutil.which", return_value="/usr/bin/from-path"),
            patch("os.path.isfile", return_value=False),
        ):
            with pytest.raises(RuntimeError, match="bad config"):
                backend._load_config()
        assert backend._extra_args == ["--stale"]
        assert backend._binary_path == "/usr/bin/hermes"

    def test_unbalanced_args_keep_path_and_raise(self):
        """An unbalanced quote must not drop the configured path or install default argv."""
        backend = HermesBackend.__new__(HermesBackend)
        backend._extra_args = []
        backend._binary_path = None

        def getter(key, default=None):
            values = {
                "agent_backend.path": "/usr/bin/hermes",
                "agent_backend.args": '--config "unterminated',
            }
            return values.get(key, default)

        with (
            patch("plugin.framework.config.get_config", side_effect=getter),
            patch("os.path.isfile", return_value=True),
            patch("shutil.which", return_value="/usr/bin/from-path") as which,
        ):
            with pytest.raises(ValueError, match="unbalanced quote"):
                backend._load_config()
        which.assert_not_called()
        assert backend._binary_path == "/usr/bin/hermes"
        assert backend._extra_args == []


    @pytest.mark.parametrize(
        "path, expected",
        [
            pytest.param("/opt/my-wrapper", "/opt/my-wrapper", id="test_hermes_unrelated_basename_skips_defaults"),
            pytest.param("/usr/bin/hermes.sh", "/usr/bin/hermes.sh", id="test_non_windows_suffix_skips_defaults"),
        ],
    )
    def test_hermes_unrelated_basename_skips_defaults(self, path, expected):
        with (
            patch("plugin.framework.config.get_config", side_effect=_config_get(path=path, args="")),
            patch("os.path.isfile", return_value=True),
            patch("shutil.which", return_value=None),
        ):
            backend = HermesBackend()
        assert (backend._binary_path) == (expected)
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
        assert backend.default_extra_args == ()

    @pytest.mark.parametrize("backend_cls,binary", [(HermesBackend, "hermes"), (OpenCodeBackend, "opencode")])
    @pytest.mark.parametrize("suffix", [".exe", ".cmd", ".bat", ".EXE"])
    def test_windows_suffix_applies_default_extra_args(self, backend_cls, binary, suffix):
        """shutil.which on Windows returns name.exe / .cmd / .bat, not the bare name."""
        path = f"/usr/bin/{binary}{suffix}"
        with (
            patch("plugin.framework.config.get_config", side_effect=_config_get(path=path, args="")),
            patch("os.path.isfile", return_value=True),
            patch("shutil.which", return_value=None),
        ):
            backend = backend_cls()
        assert (backend._binary_path) == (path)
        assert (backend._extra_args) == (["acp"])


    def test_windows_wrapper_stem_skips_defaults(self):
        with (
            patch("plugin.framework.config.get_config", side_effect=_config_get(path="/opt/hermes-wrapper.exe", args="")),
            patch("os.path.isfile", return_value=True),
            patch("shutil.which", return_value=None),
        ):
            backend = HermesBackend()
        assert (backend._extra_args) == ([])

    @pytest.mark.parametrize("binary", ["grok.exe", "grok-cli.exe", "Grok.CMD"])
    def test_grok_windows_suffix_still_uses_prefix_match(self, binary):
        path = f"/usr/local/bin/{binary}"
        with (
            patch("plugin.framework.config.get_config", side_effect=_config_get(path=path, args="")),
            patch("os.path.isfile", return_value=True),
            patch("shutil.which", return_value=None),
        ):
            backend = GrokBackend()
        assert (backend._binary_path) == (path)
        assert (backend._extra_args) == (["--no-auto-update", "agent", "stdio"])

    def test_claude_windows_suffix_still_has_no_defaults(self):
        with (
            patch("plugin.framework.config.get_config", side_effect=_config_get(path="/usr/bin/claude-code-acp-rs.exe", args="")),
            patch("os.path.isfile", return_value=True),
            patch("shutil.which", return_value=None),
        ):
            backend = ClaudeBackend()
        assert (backend._extra_args) == ([])

    def test_default_extra_args_are_immutable_tuples(self):
        assert isinstance(HermesBackend.default_extra_args, tuple)
        assert isinstance(OpenCodeBackend.default_extra_args, tuple)
        assert isinstance(GrokBackend.default_extra_args, tuple)
        assert isinstance(ACPBackend.default_extra_args, tuple)


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

    def test_missing_config_path_warns_and_falls_back(self, caplog):
        caplog.set_level("WARNING", logger="plugin.acp.acp_backend")
        with (
            patch("plugin.framework.config.get_config", side_effect=_config_get(path="/missing/hermes", args="")),
            patch("os.path.isfile", return_value=False),
            patch("shutil.which", return_value="/usr/bin/hermes"),
        ):
            backend = HermesBackend()
        assert (backend._binary_path) == ("/usr/bin/hermes")
        assert ("/missing/hermes") in (caplog.text)
        assert ("not a file") in (caplog.text)

    def test_empty_config_path_does_not_warn(self, caplog):
        caplog.set_level("WARNING", logger="plugin.acp.acp_backend")
        with (
            patch("plugin.framework.config.get_config", side_effect=_config_get(path="", args="")),
            patch("os.path.isfile", return_value=True),
            patch("shutil.which", return_value="/usr/bin/hermes"),
        ):
            HermesBackend()
        assert ("agent_backend.path") not in (caplog.text)

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

    def test_is_available_caches_path_hit(self):
        with (
            patch("plugin.framework.config.get_config", side_effect=_config_get(path="", args="")),
            patch("os.path.isfile", return_value=True),
            patch("os.access", return_value=True),
            patch("shutil.which", return_value="/usr/bin/hermes") as mock_which,
            patch("glob.glob", return_value=[]) as mock_glob,
        ):
            backend = HermesBackend()
            which_count = mock_which.call_count
            glob_count = mock_glob.call_count
            assert backend.is_available(None) is True
            # Second call with same settings leaves call counts unchanged
            assert backend.is_available(None) is True
            assert mock_which.call_count == which_count
            assert mock_glob.call_count == glob_count

    def test_is_available_cache_recheck_on_changed_settings(self):
        with (
            patch("plugin.framework.config.get_config", side_effect=_config_get(path="", args="")),
            patch("os.path.isfile", return_value=True),
            patch("os.access", return_value=True),
            patch("shutil.which", return_value="/usr/bin/hermes") as mock_which,
        ):
            backend = HermesBackend()
            assert backend.is_available(None) is True
        with (
            patch("plugin.framework.config.get_config", side_effect=_config_get(path="", args="--diff")),
            patch("os.path.isfile", return_value=True),
            patch("os.access", return_value=True),
            patch("shutil.which", return_value="/usr/bin/hermes") as mock_which,
        ):
            assert backend.is_available(None) is True
            mock_which.assert_called()

    def test_is_available_skips_write_when_send_lock_held(self):
        backend = _bare_backend()
        backend._binary_path = "/usr/bin/hermes"
        backend._extra_args = ["acp"]

        lock_acquired = threading.Event()
        release_lock = threading.Event()

        def hold_lock():
            with backend._send_lock:
                lock_acquired.set()
                release_lock.wait(timeout=5)

        t = threading.Thread(target=hold_lock)
        t.start()
        assert lock_acquired.wait(timeout=5)

        with (
            patch("plugin.framework.config.get_config", side_effect=_config_get(path="", args="--new-args")),
            patch("os.path.isfile", return_value=True),
            patch("os.access", return_value=True),
            patch("shutil.which", return_value="/usr/bin/hermes"),
        ):
            avail = backend.is_available(None)
            assert avail is True
            assert backend._extra_args == ["acp"]

            release_lock.set()
            t.join(timeout=5)
            assert not t.is_alive()

            avail2 = backend.is_available(None)
            assert avail2 is True
            assert backend._extra_args == ["--new-args"]

    def test_is_available_unbalanced_quote_keeps_stale_args(self):
        backend = _bare_backend()
        backend._extra_args = ["--stale"]
        with (
            patch("plugin.framework.config.get_config", side_effect=_config_get(path="", args='--config="unclosed')),
            patch("os.path.isfile", return_value=True),
            patch("os.access", return_value=True),
            patch("shutil.which", return_value="/usr/bin/hermes"),
        ):
            with pytest.raises(ValueError, match="unbalanced quote"):
                backend.is_available(None)
        assert backend._extra_args == ["--stale"]

    def test_npm_global_bin_used_when_which_misses(self, tmp_path, monkeypatch):
        home = tmp_path / "home"
        name = "writeragent-acp-test-bin"
        binary = home / ".npm-global" / "bin" / name
        binary.parent.mkdir(parents=True)
        binary.write_text("#!/bin/sh\n", encoding="utf-8")
        binary.chmod(0o755)
        monkeypatch.setattr("plugin.acp.acp_backend.os.path.expanduser", lambda path: str(home) if path == "~" else path)
        monkeypatch.setattr("plugin.acp.acp_backend.shutil.which", lambda binary_name: None)
        backend = _bare_backend()
        backend.get_binary_name = lambda: name
        assert (backend._find_binary()) == (str(binary))


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

        backend = _bare_backend()
        backend._session_id = "sess-1"
        backend._ensure_connection = MagicMock()
        backend._ensure_session = MagicMock()
        backend.display_name = "Test"
        tool_call = {"type": "tool_call", "name": "read"}
        tool_result = {"type": "tool_result", "content": "ok"}
        mock_conn = _mock_conn()
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
        mock_conn.stop.assert_not_called()
        assert (backend._conn) is mock_conn
        assert (backend._session_id) == ("sess-1")


def _arm_rpc(conn):
    def begin_request(method, params=None):
        return (method, params)

    def await_response(handle, timeout=120):
        method, params = handle
        return conn.send_request(method, params, timeout)

    conn.begin_request.side_effect = begin_request
    conn.await_response.side_effect = await_response
    return conn


def _mock_conn(*, alive: bool = True, healthy: bool | None = None):
    conn = MagicMock()
    conn.is_alive = alive
    conn.is_healthy = alive if healthy is None else healthy
    return _arm_rpc(conn)


def _bare_backend():
    """ACPBackend without config or a live process."""
    backend = ACPBackend.__new__(ACPBackend)
    backend._stop_requested = False
    backend._closed = False
    backend._inflight_token = None
    backend._session_id = None
    backend._session_url = None
    backend._session_mcp_url = None
    backend._session_prompted = False
    backend._ready = False
    backend._conn = None
    backend._turn = 0
    backend._turn_pending = False
    backend._stop_turn = 0
    backend._prompt_inflight = False
    backend._prompt_done = threading.Event()
    backend._prompt_done.set()
    backend._state_lock = threading.Lock()
    backend._send_lock = threading.Lock()
    backend._pending_permissions = {}
    backend.display_name = "Test"
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

    def test_tool_call_update_in_progress_with_content_is_activity(self):
        """Streaming content is not a finished result.

        What was wrong: ``content`` on an in_progress update was queued as
        TOOL_RESULT, so one call showed several "[Tool result]" lines.
        """
        update = {
            "sessionUpdate": "tool_call_update",
            "toolCallId": "call_001",
            "status": "in_progress",
            "content": [{"type": "content", "content": {"type": "text", "text": "partial"}}],
        }
        events = self._events(update)
        assert (events[0][0]) == (StreamQueueKind.TOOL_CALL)
        pending = {"sessionUpdate": "tool_call_update", "toolCallId": "call_001", "status": "pending", "rawOutput": {"partial": True}}
        assert (self._events(pending)[0][0]) == (StreamQueueKind.TOOL_CALL)

    def test_tool_call_update_content_without_status_is_a_result(self):
        update = {"sessionUpdate": "tool_call_update", "toolCallId": "call_001", "rawOutput": {"ok": True}}
        events = self._events(update)
        assert (events[0][0]) == (StreamQueueKind.TOOL_RESULT)

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

    def test_tool_call_update_cancelled_without_content_is_tool_result(self):
        update = {
            "sessionUpdate": "tool_call_update",
            "toolCallId": "call_001",
            "status": "cancelled",
        }
        events = self._events(update)
        assert (events[0][0]) == (StreamQueueKind.TOOL_RESULT)
        assert _tool_stream_kind(update) == StreamQueueKind.TOOL_RESULT

    def test_legacy_non_str_text_is_not_queued(self):
        update = {"content": [{"type": "text", "text": {"not": "a string"}}, {"type": "text", "text": "kept"}]}
        assert (self._events(update)) == ([(StreamQueueKind.CHUNK, "kept")])

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
        conn = backend._conn if backend._conn is not None else MagicMock()
        backend._dispatch_notification(conn, "session/request_permission", params, msg_id, q)
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

    @pytest.mark.parametrize(
        "value, value_2",
        [
            pytest.param(True, "allow-once", id="test_approve_selects_allow_once"),
            pytest.param(False, "reject-once", id="test_reject_selects_reject_once"),
        ],
    )
    def test_approve_selects_allow_once(self, value, value_2):
        backend = _bare_backend()
        conn = MagicMock()
        conn.is_alive = True
        backend._conn = conn
        self._request(backend, msg_id=5)
        backend.submit_approval(5, value)
        conn.send_response.assert_called_once_with(5, result={"outcome": {"outcome": "selected", "optionId": value_2}})


    def test_reject_without_reject_option_is_cancelled(self):
        backend = _bare_backend()
        conn = MagicMock()
        conn.is_alive = True
        backend._conn = conn
        q = queue.Queue()
        backend._dispatch_notification(
            conn,
            "session/request_permission",
            {"toolCall": {"toolCallId": "c", "title": "Run"}, "options": [{"optionId": "allow-once", "name": "Allow once", "kind": "allow_once"}]},
            3,
            q,
        )
        backend.submit_approval(3, False)
        conn.send_response.assert_called_once_with(3, result={"outcome": {"outcome": "cancelled"}})


    def test_permission_request_already_queued_when_stop_happens(self):
        backend = _bare_backend()
        conn = MagicMock()
        conn.is_alive = True
        backend._conn = conn

        q = queue.Queue()
        # Request arrives and is enqueued, opening dialog
        backend._dispatch_notification(
            conn,
            "session/request_permission",
            {"toolCall": {"title": "Edit main.py", "kind": "edit", "toolCallId": "call_1"}, "options": _PERMISSION_OPTIONS},
            8,
            q,
        )

        # Stop button is clicked while dialog is open
        backend.stop()

        # We now submit the dialog answer (e.g. they hit Approve)
        backend.submit_approval(8, True)

        # The submission should be ignored or gracefully handled,
        # because the stop cancels all pending permissions.
        # send_response might be called by stop with "cancelled",
        # but the submit_approval shouldn't double-call with "selected".
        call_args = [call.kwargs.get('result', {}).get('outcome', {}).get('outcome')
                    for call in conn.send_response.mock_calls]
        assert call_args == ["cancelled"]

    def test_permission_after_stop_is_cancelled_without_dialog(self):
        backend = _bare_backend()
        backend._stop_requested = True
        conn = MagicMock()
        conn.is_alive = True
        backend._conn = conn
        q = queue.Queue()
        backend._dispatch_notification(conn, "session/request_permission", {"toolCall": {"title": "Edit main.py", "kind": "edit", "toolCallId": "call_1"}, "options": _PERMISSION_OPTIONS}, 8, q)
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

    def test_permission_without_id_is_not_queued(self, caplog):
        caplog.set_level("WARNING", logger="plugin.acp.acp_backend")
        backend = _bare_backend()
        conn = MagicMock()
        conn.is_alive = True
        backend._conn = conn
        q = queue.Queue()
        backend._dispatch_notification(
            conn,
            "session/request_permission",
            {"toolCall": {"title": "Edit main.py", "kind": "edit", "toolCallId": "call_1"}, "options": _PERMISSION_OPTIONS},
            None,
            q,
        )
        assert (_drain(q)) == ([])
        conn.send_response.assert_not_called()
        assert (backend._pending_permissions) == ({})
        assert ("ACP permission request missing id") in (caplog.text)

    def test_permission_without_id_after_stop_is_not_queued(self, caplog):
        caplog.set_level("WARNING", logger="plugin.acp.acp_backend")
        backend = _bare_backend()
        backend._stop_requested = True
        conn = MagicMock()
        conn.is_alive = True
        backend._conn = conn
        q = queue.Queue()
        backend._dispatch_notification(
            conn,
            "session/request_permission",
            {"toolCall": {"title": "Edit main.py", "kind": "edit", "toolCallId": "call_1"}, "options": _PERMISSION_OPTIONS},
            None,
            q,
        )
        assert (_drain(q)) == ([])
        conn.send_response.assert_not_called()
        assert (backend._pending_permissions) == ({})
        assert ("ACP permission request missing id") in (caplog.text)

    def test_permission_with_unhashable_id_is_cancelled(self, caplog):
        caplog.set_level("WARNING", logger="plugin.acp.acp_backend")
        backend = _bare_backend()
        conn = MagicMock()
        conn.is_alive = True
        backend._conn = conn
        q = queue.Queue()
        backend._dispatch_notification(conn, "session/request_permission", {"toolCall": {"title": "Edit"}, "options": []}, [1], q)
        conn.send_response.assert_called_once_with([1], result={"outcome": {"outcome": "cancelled"}})
        assert _drain(q) == []
        assert backend._pending_permissions == {}
        assert "ACP permission request id is unhashable; cancelling without storing it" in caplog.text

    def test_submit_approval_replies_on_the_request_connection(self):
        backend = _bare_backend()
        conn_a = MagicMock()
        conn_a.is_alive = True
        conn_b = MagicMock()
        conn_b.is_alive = True
        backend._conn = conn_a
        q = queue.Queue()
        backend._dispatch_notification(
            conn_a,
            "session/request_permission",
            {"toolCall": {"toolCallId": "c", "title": "Run"}, "options": [{"optionId": "allow-once", "name": "Allow once", "kind": "allow_once"}]},
            5,
            q,
        )
        backend._conn = conn_b
        backend.submit_approval(5, True)
        conn_a.send_response.assert_called_once_with(5, result={"outcome": {"outcome": "selected", "optionId": "allow-once"}})
        conn_b.send_response.assert_not_called()


class TestStopAndShutdown:
    """Stop cancels the in-flight ACP turn. A healthy CLI stays up."""

    def test_stop_cancels_permission_and_keeps_an_idle_process(self):
        backend = _bare_backend()
        backend._session_id = "sess-1"
        conn = MagicMock()
        conn.is_alive = True
        backend._conn = conn
        q = queue.Queue()
        backend._dispatch_notification(
            conn,
            "session/request_permission",
            {"toolCall": {"title": "Edit main.py", "kind": "edit", "toolCallId": "call_1"}, "options": _PERMISSION_OPTIONS},
            7,
            q,
        )
        backend.stop()

        conn.send_notification.assert_not_called()
        conn.send_response.assert_called_once_with(7, result={"outcome": {"outcome": "cancelled"}})
        assert ("session/interrupt") not in (str(conn.send_notification.call_args_list))
        conn.stop.assert_not_called()
        assert (backend._conn) is conn
        assert (backend._session_id) == ("sess-1")
        assert (backend._stop_requested) is True
        # The dialog lost the race: Stop already answered.
        backend.submit_approval(7, True)
        assert (conn.send_response.call_count) == (1)

    def test_stop_cancels_permission_on_the_request_connection(self):
        backend = _bare_backend()
        conn_a = MagicMock()
        conn_a.is_alive = True
        conn_b = MagicMock()
        conn_b.is_alive = True
        backend._conn = conn_a
        q = queue.Queue()
        backend._dispatch_notification(
            conn_a,
            "session/request_permission",
            {"toolCall": {"toolCallId": "c", "title": "Run"}, "options": [{"optionId": "allow-once", "name": "Allow once", "kind": "allow_once"}]},
            5,
            q,
        )
        backend._conn = conn_b
        backend.stop()
        conn_a.send_response.assert_called_once_with(5, result={"outcome": {"outcome": "cancelled"}})
        conn_b.send_response.assert_not_called()

    def test_stop_while_inflight_cancels_the_session_and_keeps_the_process(self):
        backend = _bare_backend()
        backend._session_id = "sess-1"
        backend._prompt_inflight = True
        backend._prompt_done.clear()
        conn = MagicMock()
        conn.is_alive = True
        backend._conn = conn
        with patch.object(backend, "_arm_cancel_grace") as arm:
            backend.stop()
        conn.send_notification.assert_called_once_with("session/cancel", {"sessionId": "sess-1"})
        arm.assert_called_once_with(backend._inflight_token, conn)
        conn.stop.assert_not_called()
        assert (backend._conn) is conn

    def test_stop_cancel_uses_the_inflight_connection_and_session(self):
        backend = _bare_backend()
        backend._turn = 4
        backend._prompt_inflight = True
        backend._prompt_done.clear()
        backend._session_id = "sess-old"

        recorded = []
        replacement = MagicMock()
        replacement.is_alive = True

        class _OldConn:
            @property
            def is_alive(self):
                backend._conn = replacement
                backend._session_id = "sess-new"
                return True

            def send_notification(self, method, params):
                recorded.append((method, params))

        old = _OldConn()
        backend._conn = old

        with patch.object(backend, "_arm_cancel_grace") as arm:
            backend.stop()

        assert recorded == [("session/cancel", {"sessionId": "sess-old"})]
        replacement.send_notification.assert_not_called()
        arm.assert_called_once_with(backend._inflight_token, old)

    def test_send_keeps_the_cli_after_success(self):
        backend = _bare_backend()
        backend._session_id = "sess-1"
        conn = _mock_conn()
        conn.send_request.return_value = {"stopReason": "end_turn"}
        backend._conn = conn
        backend._ensure_connection = lambda stop_checker=None: None
        backend._ensure_session = lambda **kwargs: None
        q = queue.Queue()
        backend.send(queue=q, user_message="hi", document_context=None, document_url=None)
        assert ((StreamQueueKind.STREAM_DONE, None)) in (_drain(q))
        conn.stop.assert_not_called()
        conn.send_notification.assert_not_called()
        assert (backend._conn) is conn
        assert (backend._session_id) == ("sess-1")

    def test_session_creation_failure_keeps_a_live_cli(self):
        backend = _bare_backend()
        conn = MagicMock()
        conn.is_alive = True

        def ensure_connection(stop_checker=None):
            backend._conn = conn

        backend._ensure_connection = ensure_connection
        backend._ensure_session = MagicMock(side_effect=RuntimeError("no session"))
        q = queue.Queue()
        backend.send(queue=q, user_message="hi", document_context=None, document_url=None)
        kinds = [event[0] for event in _drain(q)]
        assert (StreamQueueKind.ERROR) in (kinds)
        conn.stop.assert_not_called()
        assert (backend._conn) is conn

    def test_send_error_when_process_fails(self):
        backend = _bare_backend()
        backend._ensure_connection = MagicMock(side_effect=RuntimeError("spawn failed"))
        q = queue.Queue()
        backend.send(queue=q, user_message="test", document_context=None, document_url=None)
        kinds = [event[0] for event in _drain(q)]
        assert (StreamQueueKind.ERROR) in (kinds)

    def test_stop_during_prompt_keeps_the_cli_when_the_prompt_returns(self):
        backend = _bare_backend()
        backend._session_id = "sess-9"
        conn = _mock_conn()
        started = threading.Event()

        def send_request(method, params=None, timeout=120):
            if method == "session/prompt":
                started.set()
                deadline = time.monotonic() + 2
                while time.monotonic() < deadline and not backend._stop_requested:
                    time.sleep(0.01)
                if not backend._stop_requested:
                    raise TimeoutError("prompt still blocked")
                raise RuntimeError("ACP process stopped")
            return {"sessionId": "sess-9"}

        conn.send_request.side_effect = send_request
        backend._conn = conn
        backend._ensure_connection = lambda stop_checker=None: None
        backend._ensure_session = lambda **kwargs: None
        backend._pending_permissions[4] = (conn, [{"optionId": "allow-once", "name": "Allow once", "kind": "allow_once"}])
        q = queue.Queue()
        worker = threading.Thread(target=lambda: backend.send(queue=q, user_message="hi", document_context=None, document_url=None))
        worker.start()
        assert (started.wait(timeout=2))
        backend.stop()
        worker.join(timeout=2)
        assert (worker.is_alive()) is False
        conn.send_notification.assert_called_with("session/cancel", {"sessionId": "sess-9"})
        conn.send_response.assert_any_call(4, result={"outcome": {"outcome": "cancelled"}})
        conn.stop.assert_not_called()
        assert ((StreamQueueKind.STOPPED, None)) in (_drain(q))
        assert (backend._conn) is conn
        assert (backend._session_id) == ("sess-9")

    def test_stop_grace_kills_when_the_prompt_does_not_return(self):
        backend = _bare_backend()
        backend._session_id = "sess-9"
        conn = _mock_conn()
        started = threading.Event()
        release = threading.Event()

        def send_request(method, params=None, timeout=120):
            if method == "session/prompt":
                started.set()
                if not release.wait(timeout=2):
                    raise TimeoutError("prompt still blocked")
                raise RuntimeError("ACP process stopped")
            return {"sessionId": "sess-9"}

        conn.send_request.side_effect = send_request
        conn.stop.side_effect = lambda: release.set()
        backend._conn = conn
        backend._ensure_connection = lambda stop_checker=None: None
        backend._ensure_session = lambda **kwargs: None
        q = queue.Queue()
        worker = threading.Thread(target=lambda: backend.send(queue=q, user_message="hi", document_context=None, document_url=None))
        worker.start()
        assert (started.wait(timeout=2))
        with patch("plugin.acp.acp_backend._CANCEL_GRACE_SEC", 0.05):
            backend.stop()
            worker.join(timeout=2)
        assert (worker.is_alive()) is False
        conn.stop.assert_called()
        assert ((StreamQueueKind.STOPPED, None)) in (_drain(q))
        assert (backend._conn) is None
        assert (backend._session_id) is None

    def test_request_does_not_send_when_this_turn_is_already_stopped(self):
        backend = _bare_backend()
        backend._turn = 2
        backend._stop_turn = 2
        backend._stop_requested = True
        conn = MagicMock()
        with pytest.raises(_TurnStopped):
            backend._request(conn, "session/prompt", {}, 1)
        conn.send_request.assert_not_called()
        assert (backend._prompt_inflight) is False
        assert (backend._prompt_done.is_set()) is True

    def test_stop_just_before_the_prompt_does_not_send_it(self):
        backend = _bare_backend()
        backend._session_id = "sess-1"
        conn = _mock_conn()
        backend._conn = conn
        backend._ensure_connection = lambda stop_checker=None: None
        backend._ensure_session = lambda **kwargs: None
        real_request = backend._request

        def request(conn_arg, method, params, timeout):
            if method == "session/prompt":
                backend.stop()
            return real_request(conn_arg, method, params, timeout)

        backend._request = request
        q = queue.Queue()
        backend.send(queue=q, user_message="hi", document_context=None, document_url=None)
        kinds = [event[0] for event in _drain(q)]
        assert (StreamQueueKind.STOPPED) in (kinds)
        assert (StreamQueueKind.ERROR) not in (kinds)
        conn.send_request.assert_not_called()
        conn.stop.assert_not_called()
        assert (backend._conn) is conn
        assert (backend._session_id) == ("sess-1")

    def test_stop_before_session_new_is_stopped_not_a_session_error(self):
        backend = _bare_backend()
        conn = _mock_conn()

        def ensure_connection(stop_checker=None):
            backend._conn = conn

        backend._ensure_connection = ensure_connection
        real_request = backend._request

        def request(conn_arg, method, params, timeout):
            if method == "session/new":
                backend.stop()
            return real_request(conn_arg, method, params, timeout)

        backend._request = request
        q = queue.Queue()
        backend.send(queue=q, user_message="hi", document_context=None, document_url=None)
        kinds = [event[0] for event in _drain(q)]
        assert (StreamQueueKind.STOPPED) in (kinds)
        assert (StreamQueueKind.ERROR) not in (kinds)
        conn.send_request.assert_not_called()
        conn.stop.assert_not_called()
        assert (backend._conn) is conn

    def test_grace_kills_only_the_turn_that_is_still_inflight(self):
        backend = _bare_backend()
        token = object()
        backend._inflight_token = token
        backend._turn = 3
        backend._prompt_inflight = True
        backend._session_id = "sess"
        backend._ready = True
        conn = _mock_conn()
        conn.is_alive = True
        conn.is_healthy = True
        backend._conn = conn
        backend._pending_permissions[9] = (conn, [])
        backend._kill_turn_if_still_inflight(token, conn)
        conn.send_response.assert_called_once_with(9, result={"outcome": {"outcome": "cancelled"}})
        conn.stop.assert_called_once()
        assert (backend._conn) is None
        assert (backend._session_id) is None
        assert (backend._ready) is False

    def test_grace_does_not_kill_after_the_turn_moves(self):
        backend = _bare_backend()
        token = object()
        other_token = object()
        backend._inflight_token = token
        backend._turn = 4
        backend._prompt_inflight = True
        backend._session_id = "sess"
        conn = _mock_conn()
        conn.is_alive = True
        conn.is_healthy = True
        backend._conn = conn
        backend._kill_turn_if_still_inflight(other_token, conn)
        conn.stop.assert_not_called()
        assert (backend._conn) is conn
        assert (backend._session_id) == ("sess")

        backend._kill_turn_if_still_inflight(token, conn)
        conn.stop.assert_called_once()
        assert (backend._conn) is None
        assert (backend._session_id) is None

    def test_stop_grace_with_overlapping_second_send(self):
        backend = _bare_backend()
        backend._binary_path = "/usr/bin/test-agent"
        backend._extra_args = []
        backend._session_id = "sess-1"

        conn1 = _mock_conn()
        conn1.is_alive = True
        conn1.is_healthy = True

        started = threading.Event()
        release = threading.Event()

        def send_request1(method, params=None, timeout=120):
            if method == "session/prompt":
                started.set()
                if not release.wait(timeout=2):
                    raise TimeoutError("prompt still blocked")
                conn1.is_alive = False
                conn1.is_healthy = False
                raise RuntimeError("ACP process stopped")
            return {"sessionId": "sess-1"}

        conn1.send_request.side_effect = send_request1
        conn1.stop.side_effect = lambda: release.set()
        backend._conn = conn1
        backend._ready = True

        conn2 = _mock_conn()
        conn2.is_alive = True
        conn2.is_healthy = True

        def send_request2(method, params=None, timeout=120):
            if method == "initialize":
                return {"protocolVersion": 1}
            if method == "session/new":
                return {"sessionId": "sess-new"}
            if method == "session/prompt":
                return {"stopReason": "end_turn"}
            raise AssertionError(f"unexpected method: {method}")

        conn2.send_request.side_effect = send_request2

        q1 = queue.Queue()
        q2 = queue.Queue()

        t1 = threading.Thread(target=lambda: backend.send(queue=q1, user_message="first", document_context=None, document_url=None))
        t1.start()
        assert started.wait(timeout=2)

        def run_second_send():
            backend.prepare_turn()
            backend.send(queue=q2, user_message="second", document_context=None, document_url=None)

        t2 = threading.Thread(target=run_second_send)

        with patch("plugin.acp.acp_backend._CANCEL_GRACE_SEC", 0.05), patch("plugin.acp.acp_backend.ACPConnection", return_value=conn2):
            backend.stop()
            t2.start()
            t1.join(timeout=2)
            t2.join(timeout=2)

        assert not t1.is_alive()
        assert not t2.is_alive()
        conn1.stop.assert_called()
        kinds2 = [item[0] for item in _drain(q2)]
        assert StreamQueueKind.STREAM_DONE in kinds2

    def test_stop_before_send_does_not_start_cli(self):
        backend = _bare_backend()
        backend.prepare_turn()
        backend.stop()
        backend._ensure_connection = MagicMock(side_effect=AssertionError("cli started"))
        backend._ensure_session = MagicMock(side_effect=AssertionError("session started"))
        q = queue.Queue()
        backend.send(queue=q, user_message="hi", document_context=None, document_url=None)
        assert (_drain(q)) == ([(StreamQueueKind.STOPPED, None)])
        backend._ensure_connection.assert_not_called()
        backend._ensure_session.assert_not_called()
        assert (backend._stop_requested) is True

    def test_stop_between_unprepared_sends_does_not_stick(self):
        """A Stop on one unprepared send must not Stop the next one.

        What was wrong: the first send() without prepare_turn left the
        turn looking prepared, so a later Stop matched every send.
        """
        backend = _bare_backend()
        conn = _mock_conn()
        conn.send_request.return_value = {"stopReason": "end_turn"}
        backend._session_id = "sess-1"
        backend._session_url = ""
        backend._session_prompted = True

        def ensure_connection(stop_checker=None):
            backend._conn = conn

        backend._ensure_connection = ensure_connection
        q = queue.Queue()
        backend.send(queue=q, user_message="one", document_context=None, document_url=None)
        assert ((StreamQueueKind.STREAM_DONE, None)) in (_drain(q))
        backend.stop()
        backend.send(queue=q, user_message="two", document_context=None, document_url=None)
        second = _drain(q)
        assert ((StreamQueueKind.STREAM_DONE, None)) in (second)
        assert (StreamQueueKind.STOPPED) not in ([event[0] for event in second])
        assert (conn.send_request.call_count) == (2)
        assert (backend._session_id) == ("sess-1")
        conn.stop.assert_not_called()

    def test_release_of_a_replaced_connection_keeps_the_new_session(self):
        backend = _bare_backend()
        old = MagicMock()
        new = MagicMock()
        backend._conn = new
        backend._ready = True
        backend._session_id = "sess-new"
        backend._session_url = "file:///a.odt"
        backend._session_prompted = True
        backend._release_connection(old)
        old.stop.assert_called_once()
        new.stop.assert_not_called()
        assert (backend._conn) is new
        assert (backend._session_id) == ("sess-new")
        assert (backend._ready) is True
        assert (backend._session_prompted) is True
        assert (backend._session_url) == ("file:///a.odt")

    def test_release_of_the_current_connection_clears_the_session(self):
        backend = _bare_backend()
        conn = MagicMock()
        backend._conn = conn
        backend._ready = True
        backend._session_id = "sess"
        backend._session_url = "file:///a.odt"
        backend._session_prompted = True
        backend._release_connection(conn)
        conn.stop.assert_called_once()
        assert (backend._conn) is None
        assert (backend._session_id) is None
        assert (backend._ready) is False
        assert (backend._session_prompted) is False
        assert (backend._session_url) is None

    def test_previous_stop_does_not_abort_the_next_send(self):
        backend = _bare_backend()
        backend.stop()
        backend.prepare_turn()
        conn = _mock_conn()
        conn.send_request.return_value = {"stopReason": "end_turn"}
        backend._session_id = "sess-1"

        def ensure_connection(stop_checker=None):
            backend._conn = conn

        backend._ensure_connection = ensure_connection
        backend._ensure_session = lambda **kwargs: None
        q = queue.Queue()
        backend.send(queue=q, user_message="hi", document_context=None, document_url=None)
        assert ((StreamQueueKind.STREAM_DONE, None)) in (_drain(q))
        conn.stop.assert_not_called()

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

    def test_notification_callback_is_registered_before_session_new(self):
        """A session/update emitted during session/new must reach the queue.

        What was wrong: send() installed the callback after session/new.
        Notifications during that request saw no callback and were dropped.
        """
        backend = _bare_backend()
        conn = _mock_conn()
        order: list[str] = []
        installed = []

        def set_notification_callback(callback):
            if callback is not None:
                order.append("callback")
                installed.append(callback)

        def send_request(method, params=None, timeout=120):
            order.append(method)
            if method == "session/new":
                assert installed
                callback = installed[0]
                callback(
                    "session/update",
                    {"update": {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "during-new"}}},
                    None,
                )
                return {"sessionId": "sess-early"}
            if method == "session/prompt":
                return {"stopReason": "end_turn"}
            raise AssertionError(method)

        conn.set_notification_callback.side_effect = set_notification_callback
        conn.send_request.side_effect = send_request

        def ensure_connection(stop_checker=None):
            backend._conn = conn

        backend._ensure_connection = ensure_connection
        q = queue.Queue()
        backend.send(queue=q, user_message="hi", document_context=None, document_url=None)
        assert (order.index("callback")) < (order.index("session/new"))
        assert ((StreamQueueKind.CHUNK, "during-new")) in (_drain(q))

    def test_overlapping_send_waits_for_first_send_finally(self):
        backend = _bare_backend()
        conn = _mock_conn()
        backend._conn = conn
        backend._ready = True
        backend._session_id = "sess-1"
        backend._session_prompted = True
        backend._ensure_connection = lambda stop_checker=None: None
        backend._ensure_session = lambda **kwargs: None

        order = []
        current_callback = [None]

        def set_notification_callback(cb):
            order.append(("set", cb is not None))
            current_callback[0] = cb

        conn.set_notification_callback.side_effect = set_notification_callback

        prompt_1_started = threading.Event()
        prompt_1_release = threading.Event()
        prompt_count = 0

        def send_request(method, params=None, timeout=120):
            nonlocal prompt_count
            if method == "session/prompt":
                prompt_count += 1
                if prompt_count == 1:
                    order.append("first-enter")
                    prompt_1_started.set()
                    if not prompt_1_release.wait(timeout=5):
                        raise TimeoutError("prompt 1 timed out")
                    order.append("first-leave")
                    return {"stopReason": "end_turn"}
                if prompt_count == 2:
                    assert current_callback[0] is not None
                    current_callback[0](
                        "session/update",
                        {"update": {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "second"}}},
                        None,
                    )
                    assert backend._prompt_inflight is True
                    return {"stopReason": "end_turn"}
            raise AssertionError(f"Unexpected send_request: {method}")

        conn.send_request.side_effect = send_request

        real_send_lock = backend._send_lock
        thread2_entered_acquire = threading.Event()

        class _LockWrapper:
            def acquire(self, *args, **kwargs):
                if threading.current_thread().name == "second-send":
                    thread2_entered_acquire.set()
                return real_send_lock.acquire(*args, **kwargs)

            def release(self):
                return real_send_lock.release()

            def __enter__(self):
                if threading.current_thread().name == "second-send":
                    thread2_entered_acquire.set()
                return real_send_lock.__enter__()

            def __exit__(self, exc_type, exc_val, exc_tb):
                return real_send_lock.__exit__(exc_type, exc_val, exc_tb)

        backend._send_lock = _LockWrapper()

        q1 = queue.Queue()
        q2 = queue.Queue()

        t1 = threading.Thread(
            target=lambda: backend.send(queue=q1, user_message="one", document_context=None, document_url=None),
            name="first-send",
        )
        t2 = threading.Thread(
            target=lambda: (backend.prepare_turn(), backend.send(queue=q2, user_message="two", document_context=None, document_url=None)),
            name="second-send",
        )

        t1.start()
        assert prompt_1_started.wait(timeout=5)
        t2.start()
        assert thread2_entered_acquire.wait(timeout=5)
        time.sleep(0.05)
        prompt_1_release.set()

        t1.join(timeout=5)
        t2.join(timeout=5)
        assert not t1.is_alive()
        assert not t2.is_alive()

        first_enter_idx = order.index("first-enter")
        first_leave_idx = order.index("first-leave")
        between = order[first_enter_idx + 1 : first_leave_idx]
        assert ("set", True) not in between
        after = order[first_leave_idx + 1 :]
        assert ("set", True) in after

        assert (StreamQueueKind.CHUNK, "second") in _drain(q2)

    def test_stop_does_not_wait_for_send_lock(self):
        backend = _bare_backend()
        conn = _mock_conn()
        backend._conn = conn
        backend._ready = True
        backend._session_id = "sess-1"
        backend._session_prompted = True
        backend._ensure_connection = lambda stop_checker=None: None
        backend._ensure_session = lambda **kwargs: None

        prompt_started = threading.Event()
        prompt_release = threading.Event()

        def send_request(method, params=None, timeout=120):
            if method == "session/prompt":
                prompt_started.set()
                if not prompt_release.wait(timeout=5):
                    raise TimeoutError("prompt timed out")
                return {"stopReason": "end_turn"}
            raise AssertionError(f"Unexpected send_request: {method}")

        conn.send_request.side_effect = send_request

        q = queue.Queue()
        t_send = threading.Thread(
            target=lambda: backend.send(queue=q, user_message="one", document_context=None, document_url=None),
            name="send-thread",
        )
        t_send.start()
        assert prompt_started.wait(timeout=5)

        t_stop = threading.Thread(target=backend.stop, name="stop-thread")
        t_stop.start()
        t_stop.join(timeout=0.5)
        assert not t_stop.is_alive()

        prompt_release.set()
        t_send.join(timeout=5)
        assert not t_send.is_alive()


def _startup_conn(*, alive: bool = True):
    conn = MagicMock()
    conn.is_alive = alive
    conn.is_healthy = alive
    conn.stderr_text.return_value = ""
    conn.send_request.return_value = {"protocolVersion": 1}
    return _arm_rpc(conn)


class TestStartupWait:
    """Stop during spawn skips initialize. A dead child is reported at once."""

    def _backend(self):
        backend = _bare_backend()
        backend._binary_path = "/usr/bin/test-agent"
        backend._extra_args = []
        return backend

    def test_shutdown_during_start_kills_child_and_stops_send(self):
        backend = self._backend()
        conn = _startup_conn()
        spawned = False
        stopped_with_spawned = False

        def start():
            nonlocal spawned
            backend.shutdown()
            spawned = True

        def stop():
            nonlocal stopped_with_spawned
            if spawned:
                stopped_with_spawned = True

        conn.start.side_effect = start
        conn.stop.side_effect = stop

        q = queue.Queue()
        with patch("plugin.acp.acp_backend.ACPConnection", return_value=conn):
            backend.send(queue=q, user_message="hi", document_context=None, document_url=None)

        kinds = [event[0] for event in _drain(q)]
        assert StreamQueueKind.STOPPED in kinds
        assert StreamQueueKind.ERROR not in kinds
        assert stopped_with_spawned is True
        assert backend._conn is None
        assert backend._closed is True
        # A second shutdown does not raise
        backend.shutdown()

    def test_shutdown_during_initialize_kills_child_and_stops_send(self):
        backend = self._backend()
        conn = _startup_conn()

        def send_request(method, params=None, timeout=120):
            if method == "initialize":
                backend.shutdown()
                raise RuntimeError("gone")
            return {"sessionId": "s"}

        conn.send_request.side_effect = send_request

        q = queue.Queue()
        with patch("plugin.acp.acp_backend.ACPConnection", return_value=conn):
            backend.send(queue=q, user_message="hi", document_context=None, document_url=None)

        kinds = [event[0] for event in _drain(q)]
        assert StreamQueueKind.STOPPED in kinds
        assert StreamQueueKind.ERROR not in kinds
        conn.stop.assert_called()
        assert backend._conn is None

    def test_stop_during_spawn_does_not_initialize_or_prompt(self):
        backend = self._backend()
        conn = _startup_conn()
        conn.start.side_effect = lambda: backend.stop()
        q = queue.Queue()
        with patch("plugin.acp.acp_backend.ACPConnection", return_value=conn):
            backend.send(queue=q, user_message="hi", document_context=None, document_url=None)
        kinds = [event[0] for event in _drain(q)]
        assert (StreamQueueKind.STOPPED) in (kinds)
        assert (StreamQueueKind.ERROR) not in (kinds)
        conn.send_request.assert_not_called()
        # Stop during spawn leaves the child. The next send continues
        # initialize because the process is not ready yet.
        conn.stop.assert_not_called()
        assert (backend._conn) is conn
        assert (backend._ready) is False

    def test_stop_checker_during_spawn_does_not_prompt(self):
        backend = self._backend()
        conn = _startup_conn()
        armed = {"stop": False}

        def start() -> None:
            armed["stop"] = True

        conn.start.side_effect = start
        q = queue.Queue()
        with patch("plugin.acp.acp_backend.ACPConnection", return_value=conn):
            backend.send(queue=q, user_message="hi", document_context=None, document_url=None, stop_checker=lambda: armed["stop"])
        kinds = [event[0] for event in _drain(q)]
        assert (StreamQueueKind.STOPPED) in (kinds)
        assert (StreamQueueKind.ERROR) not in (kinds)
        assert (backend._stop_requested) is True
        conn.send_request.assert_not_called()

    def test_turn_stopped_during_initialize_keeps_the_child(self):
        backend = self._backend()
        conn = _startup_conn()
        real_request = backend._request

        def request(conn_arg, method, params, timeout):
            backend.stop()
            return real_request(conn_arg, method, params, timeout)

        backend._request = request
        q = queue.Queue()
        with patch("plugin.acp.acp_backend.ACPConnection", return_value=conn):
            backend.send(queue=q, user_message="hi", document_context=None, document_url=None)
        kinds = [event[0] for event in _drain(q)]
        assert (StreamQueueKind.STOPPED) in (kinds)
        assert (StreamQueueKind.ERROR) not in (kinds)
        conn.send_request.assert_not_called()
        conn.stop.assert_not_called()
        assert (backend._conn) is conn
        assert (backend._ready) is False

    def test_cold_start_initializes_without_sleeping(self):
        backend = self._backend()
        conn = _startup_conn()
        with patch("plugin.acp.acp_backend.ACPConnection", return_value=conn):
            backend._ensure_connection()
        conn.send_request.assert_called_once()
        assert (conn.send_request.call_args.args[0]) == ("initialize")
        params = conn.send_request.call_args.args[1]
        assert (params["clientCapabilities"]["fs"]) == ({"readTextFile": False, "writeTextFile": False})
        assert (params["clientInfo"]["version"]) == (EXTENSION_VERSION)
        assert (backend._ready) is True

    def test_initialize_warns_on_protocol_version_mismatch(self, caplog):
        backend = self._backend()
        conn = _startup_conn()
        conn.send_request.return_value = {"protocolVersion": 99}
        with patch("plugin.acp.acp_backend.ACPConnection", return_value=conn):
            backend._ensure_connection()
        assert backend._ready is True
        assert "99" in caplog.text
        assert "1" in caplog.text

    def test_dead_process_still_raises_before_initialize(self):
        backend = self._backend()
        conn = _startup_conn(alive=False)
        with patch("plugin.acp.acp_backend.ACPConnection", return_value=conn):
            with pytest.raises(RuntimeError, match="failed to start"):
                backend._ensure_connection()
        conn.send_request.assert_not_called()

    def test_alive_but_not_ready_continues_initialize(self):
        backend = self._backend()
        conn = _startup_conn()
        backend._conn = conn
        backend._ready = False
        with patch("plugin.acp.acp_backend.ACPConnection", side_effect=AssertionError("spawned")):
            backend._ensure_connection()
        conn.start.assert_not_called()
        assert (conn.send_request.call_args.args[0]) == ("initialize")
        assert (backend._ready) is True
        assert (backend._conn) is conn

    def test_dead_child_is_dropped_and_replaced(self):
        backend = self._backend()
        dead = _startup_conn(alive=False)
        backend._conn = dead
        backend._ready = True
        backend._session_id = "old"
        fresh = _startup_conn()
        with patch("plugin.acp.acp_backend.ACPConnection", return_value=fresh):
            backend._ensure_connection()
        dead.stop.assert_called()
        assert (backend._conn) is fresh
        assert (backend._session_id) is None
        assert (backend._ready) is True

    def test_dead_process_includes_stderr_tail(self):
        backend = self._backend()
        conn = _startup_conn(alive=False)
        conn.stderr_text.return_value = "cli: not found\n"
        with patch("plugin.acp.acp_backend.ACPConnection", return_value=conn):
            with pytest.raises(RuntimeError, match="cli: not found"):
                backend._ensure_connection()
        conn.send_request.assert_not_called()

    def test_initialize_failure_on_a_dead_child_includes_stderr(self):
        backend = self._backend()
        conn = _startup_conn()

        def send_request(method, params=None, timeout=120):
            conn.is_alive = False
            conn.is_healthy = False
            raise RuntimeError("ACP error: ACP process terminated")

        conn.send_request.side_effect = send_request
        conn.stderr_text.return_value = "cli: not found\n"
        with patch("plugin.acp.acp_backend.ACPConnection", return_value=conn):
            with pytest.raises(RuntimeError, match="cli: not found"):
                backend._ensure_connection()
        conn.stop.assert_called()


class TestUnknownAcpRequest:
    """Agent requests this client does not implement must still get a JSON-RPC error."""

    def test_unknown_method_with_id_is_rejected(self):
        backend = _bare_backend()
        conn = MagicMock()
        backend._conn = conn
        q = queue.Queue()
        backend._dispatch_notification(conn, "fs/read_text_file", {"path": "a"}, 7, q)
        conn.send_response.assert_called_once_with(7, error={"code": -32601, "message": "Method not found: fs/read_text_file"})
        assert (_drain(q)) == ([])

    def test_notification_is_not_rejected(self):
        backend = _bare_backend()
        conn = MagicMock()
        backend._conn = conn
        q = queue.Queue()
        backend._dispatch_notification(
            conn,
            "session/update",
            {"update": {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "hi"}}},
            None,
            q,
        )
        conn.send_response.assert_not_called()
        assert (_drain(q)) == ([(StreamQueueKind.CHUNK, "hi")])

    def test_session_update_with_id_is_acknowledged(self):
        """A request-shaped session/update must be answered or the agent blocks."""
        backend = _bare_backend()
        conn = MagicMock()
        backend._conn = conn
        q = queue.Queue()
        backend._dispatch_notification(
            conn,
            "session/update",
            {"update": {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "hi"}}},
            9,
            q,
        )
        conn.send_response.assert_called_once_with(9, result={})
        assert ((StreamQueueKind.CHUNK, "hi")) in (_drain(q))

    def test_unknown_method_while_stopped_is_still_rejected(self):
        backend = _bare_backend()
        backend._stop_requested = True
        conn = MagicMock()
        backend._conn = conn
        q = queue.Queue()
        backend._dispatch_notification(conn, "fs/read_text_file", {}, 3, q)
        conn.send_response.assert_called_once_with(3, error={"code": -32601, "message": "Method not found: fs/read_text_file"})
        assert (_drain(q)) == ([])

    @pytest.mark.parametrize(
        "method",
        ["session/update", "notifications/session", "agent/update", "notifications/agent"],
    )
    def test_update_request_while_stopped_is_acked_without_queue(self, method):
        backend = _bare_backend()
        backend._stop_requested = True
        conn = MagicMock()
        backend._conn = conn
        q = queue.Queue()
        params = {"update": {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "hi"}}}
        backend._dispatch_notification(conn, method, params, 9, q)
        conn.send_response.assert_called_once_with(9, result={})
        assert _drain(q) == []

    def test_session_update_notification_while_stopped_is_not_queued(self):
        backend = _bare_backend()
        backend._stop_requested = True
        conn = MagicMock()
        backend._conn = conn
        q = queue.Queue()
        params = {"update": {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "hi"}}}
        backend._dispatch_notification(conn, "session/update", params, None, q)
        conn.send_response.assert_not_called()
        assert _drain(q) == []

    def test_ack_replies_on_the_dispatch_connection(self):
        backend = _bare_backend()
        conn_a = MagicMock()
        conn_b = MagicMock()
        backend._conn = conn_b
        q = queue.Queue()
        backend._dispatch_notification(
            conn_a,
            "session/update",
            {"update": {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "hi"}}},
            9,
            q,
        )
        conn_a.send_response.assert_called_once_with(9, result={})
        conn_b.send_response.assert_not_called()


class TestSessionId:
    def test_session_prompted_latched_only_when_manual_included(self):
        backend = _bare_backend()
        conn = _mock_conn()
        calls = []

        def send_request(method, params=None, timeout=120):
            calls.append((method, params))
            if method == "session/new":
                return {"sessionId": "sess-1"}
            if method == "session/prompt":
                return {"stopReason": "end_turn"}
            raise AssertionError(method)

        conn.send_request.side_effect = send_request

        def ensure_connection(stop_checker=None):
            backend._conn = conn
            backend._ready = True

        backend._ensure_connection = ensure_connection
        q = queue.Queue()
        with patch("plugin.framework.config.get_config", return_value="false"):
            # Slash-first send puts only command text, does not latch _session_prompted
            backend.send(queue=q, user_message="/compact", document_context="doc", document_url="file:///a.odt", system_prompt="MANUAL")
            assert backend._session_prompted is False
            prompts = [params["prompt"] for method, params in calls if method == "session/prompt"]
            assert len(prompts) == 1
            assert prompts[0] == [{"type": "text", "text": "/compact"}]

            # Second send with normal prompt now includes MANUAL
            backend.send(queue=q, user_message="hello", document_context="doc", document_url="file:///a.odt", system_prompt="MANUAL")
            assert backend._session_prompted is True
            prompts = [params["prompt"] for method, params in calls if method == "session/prompt"]
            assert len(prompts) == 2
            second_texts = [block["text"] for block in prompts[1]]
            assert "MANUAL" in second_texts

            # Slash command after that normal send leaves flag True
            backend.send(queue=q, user_message="/compact", document_context="doc", document_url="file:///a.odt", system_prompt="MANUAL")
            assert backend._session_prompted is True

        # Also: first send with system_prompt=None leaves flag False; next send with system_prompt="MANUAL" includes MANUAL
        backend2 = _bare_backend()
        calls2 = []

        def send_request2(method, params=None, timeout=120):
            calls2.append((method, params))
            if method == "session/new":
                return {"sessionId": "sess-2"}
            if method == "session/prompt":
                return {"stopReason": "end_turn"}
            raise AssertionError(method)

        conn2 = _mock_conn()
        conn2.send_request.side_effect = send_request2

        def ensure_connection2(stop_checker=None):
            backend2._conn = conn2
            backend2._ready = True

        backend2._ensure_connection = ensure_connection2
        q2 = queue.Queue()
        with patch("plugin.framework.config.get_config", return_value="false"):
            backend2.send(queue=q2, user_message="hello", document_context="doc", document_url="file:///a.odt", system_prompt=None)
            assert backend2._session_prompted is False

            backend2.send(queue=q2, user_message="hello again", document_context="doc", document_url="file:///a.odt", system_prompt="MANUAL")
            assert backend2._session_prompted is True
            prompts2 = [params["prompt"] for method, params in calls2 if method == "session/prompt"]
            assert len(prompts2) == 2
            assert "MANUAL" in [b["text"] for b in prompts2[1]]

    def test_same_url_reuses_the_session_and_omits_the_manual(self):
        backend = _bare_backend()
        conn = _mock_conn()
        calls = []

        def send_request(method, params=None, timeout=120):
            calls.append((method, params))
            if method == "session/new":
                return {"sessionId": "sess-1"}
            if method == "session/prompt":
                return {"stopReason": "end_turn"}
            raise AssertionError(method)

        conn.send_request.side_effect = send_request

        def ensure_connection(stop_checker=None):
            backend._conn = conn
            backend._ready = True

        backend._ensure_connection = ensure_connection
        q = queue.Queue()
        with patch("plugin.framework.config.get_config", return_value="false"):
            backend.send(queue=q, user_message="one", document_context="doc-v1", document_url="file:///a.odt", system_prompt="MANUAL")
            backend.send(queue=q, user_message="two", document_context="doc-v2", document_url="file:///a.odt", system_prompt="MANUAL")
        new_calls = [params for method, params in calls if method == "session/new"]
        prompts = [params["prompt"] for method, params in calls if method == "session/prompt"]
        assert (len(new_calls)) == (1)
        assert (len(prompts)) == (2)
        first = [block["text"] for block in prompts[0]]
        second = [block["text"] for block in prompts[1]]
        assert ("MANUAL") in (first)
        assert ("MANUAL") not in (second)
        assert any("doc-v1" in text for text in first)
        assert any("doc-v2" in text for text in second)
        conn.stop.assert_not_called()
        assert (backend._session_id) == ("sess-1")

    def test_different_document_url_starts_a_new_session(self):
        backend = _bare_backend()
        conn = _mock_conn()
        calls = []

        def send_request(method, params=None, timeout=120):
            calls.append((method, params))
            if method == "session/new":
                return {"sessionId": f"sess-{len(calls)}"}
            if method == "session/prompt":
                return {"stopReason": "end_turn"}
            raise AssertionError(method)

        conn.send_request.side_effect = send_request

        def ensure_connection(stop_checker=None):
            backend._conn = conn
            backend._ready = True

        backend._ensure_connection = ensure_connection
        q = queue.Queue()
        with patch("plugin.framework.config.get_config", return_value="false"):
            backend.send(queue=q, user_message="one", document_context="doc", document_url="file:///a.odt", system_prompt="MANUAL")
            backend.send(queue=q, user_message="two", document_context="doc", document_url="file:///b.odt", system_prompt="MANUAL")
        new_urls = [params["cwd"] for method, params in calls if method == "session/new"]
        prompts = [params["prompt"] for method, params in calls if method == "session/prompt"]
        assert (len(new_urls)) == (2)
        assert (len(prompts)) == (2)
        assert ("MANUAL") in ([block["text"] for block in prompts[1]])
        conn.stop.assert_not_called()

    def test_empty_and_missing_url_share_one_session(self):
        backend = _bare_backend()
        conn = _mock_conn()
        conn.send_request.return_value = {"sessionId": "sess-blank"}
        backend._conn = conn
        with patch("plugin.framework.config.get_config", return_value="false"):
            backend._ensure_session(document_url=None)
            backend._ensure_session(document_url="")
        conn.send_request.assert_called_once()
        assert (backend._session_url) == ("")

    def test_session_new_failure_leaves_a_live_process(self):
        backend = _bare_backend()
        conn = _mock_conn()
        conn.send_request.side_effect = RuntimeError("nope")
        backend._conn = conn
        with patch("plugin.framework.config.get_config", return_value="false"):
            with pytest.raises(RuntimeError, match="nope"):
                backend._ensure_session(document_url="file:///a.odt")
        conn.stop.assert_not_called()
        assert (backend._conn) is conn

    def test_session_new_failure_drops_a_dead_process(self):
        backend = _bare_backend()
        conn = _mock_conn(alive=False, healthy=False)
        conn.send_request.side_effect = RuntimeError("died")
        backend._conn = conn
        with patch("plugin.framework.config.get_config", return_value="false"):
            with pytest.raises(RuntimeError, match="died"):
                backend._ensure_session(document_url="file:///a.odt")
        conn.stop.assert_called()
        assert (backend._conn) is None

    def test_missing_process_without_stop_is_an_error(self):
        backend = _bare_backend()
        backend._ensure_connection = lambda stop_checker=None: None
        backend._ensure_session = lambda **kwargs: None
        q = queue.Queue()
        backend.send(queue=q, user_message="hi", document_context=None, document_url=None)
        kinds = [event[0] for event in _drain(q)]
        assert (StreamQueueKind.ERROR) in (kinds)
        assert (StreamQueueKind.STOPPED) not in (kinds)

    def test_ensure_session_without_process_raises(self):
        backend = _bare_backend()
        with pytest.raises(RuntimeError, match="not running"):
            backend._ensure_session()

    def test_blank_session_id_does_not_prompt(self):
        backend = _bare_backend()
        conn = _mock_conn()
        methods: list[str] = []

        def send_request(method, params=None, timeout=120):
            methods.append(method)
            if method == "session/new":
                return {"sessionId": "  "}
            return {"stopReason": "end_turn"}

        conn.send_request.side_effect = send_request
        backend._conn = conn
        backend._ensure_connection = lambda stop_checker=None: None
        q = queue.Queue()
        backend.send(queue=q, user_message="hi", document_context=None, document_url=None)
        assert ("session/prompt") not in (methods)
        assert (StreamQueueKind.ERROR) in ([event[0] for event in _drain(q)])

    def test_missing_session_id_raises(self):
        backend = _bare_backend()
        backend._conn = _mock_conn()
        backend._conn.send_request.return_value = {}
        with pytest.raises(RuntimeError, match="sessionId"):
            backend._ensure_session()

    def test_non_dict_session_result_raises(self):
        backend = _bare_backend()
        backend._conn = _mock_conn()
        backend._conn.send_request.return_value = "nope"
        with pytest.raises(RuntimeError, match="sessionId"):
            backend._ensure_session()


class TestEnsureSessionMcp:
    def test_ensure_session_omits_mcp_when_disabled(self):
        backend = HermesBackend()
        backend._conn = _mock_conn()
        backend._conn.send_request.return_value = {"sessionId": "s-123"}
        with patch("plugin.framework.config.get_config", side_effect=lambda k, d=None: "false" if k == "mcp.mcp_enabled" else d):
            backend._ensure_session(mcp_url="http://localhost:8765/mcp")
        backend._conn.send_request.assert_called_once()
        params = backend._conn.send_request.call_args[0][1]
        assert params["mcpServers"] == []

    def test_ensure_session_attaches_mcp_when_enabled(self):
        backend = HermesBackend()
        backend._conn = _mock_conn()
        backend._conn.send_request.return_value = {"sessionId": "s-123"}
        with patch("plugin.framework.config.get_config", side_effect=lambda k, d=None: "true" if k == "mcp.mcp_enabled" else d):
            backend._ensure_session(mcp_url="http://localhost:8765/mcp")
        backend._conn.send_request.assert_called_once()
        params = backend._conn.send_request.call_args[0][1]
        assert len(params["mcpServers"]) == 1
        assert params["mcpServers"][0]["url"] == "http://localhost:8765/mcp"

    def test_same_url_and_same_effective_mcp_reuses_session(self):
        backend = HermesBackend()
        conn = _mock_conn()
        conn.send_request.return_value = {"sessionId": "s-123"}
        backend._conn = conn
        with patch("plugin.framework.config.get_config", side_effect=lambda k, d=None: "true" if k == "mcp.mcp_enabled" else d):
            backend._ensure_session(mcp_url="http://127.0.0.1:1/mcp", document_url="file:///doc.odt")
            backend._ensure_session(mcp_url="http://127.0.0.1:1/mcp", document_url="file:///doc.odt")
        conn.send_request.assert_called_once()

    def test_same_url_different_effective_mcp_starts_new_session(self):
        backend = HermesBackend()
        conn = _mock_conn()
        conn.send_request.side_effect = [{"sessionId": "s-1"}, {"sessionId": "s-2"}]
        backend._conn = conn
        with patch("plugin.framework.config.get_config", side_effect=lambda k, d=None: "true" if k == "mcp.mcp_enabled" else d):
            backend._ensure_session(mcp_url="http://127.0.0.1:1/mcp", document_url="file:///doc.odt")
            backend._ensure_session(mcp_url="http://127.0.0.1:2/mcp", document_url="file:///doc.odt")
        assert conn.send_request.call_count == 2
        second_params = conn.send_request.call_args_list[1][0][1]
        assert second_params["mcpServers"][0]["url"] == "http://127.0.0.1:2/mcp"
        conn.stop.assert_not_called()

    def test_disabled_then_enabled_mcp_starts_new_session(self):
        backend = HermesBackend()
        conn = _mock_conn()
        conn.send_request.side_effect = [{"sessionId": "s-1"}, {"sessionId": "s-2"}]
        backend._conn = conn
        with patch("plugin.framework.config.get_config", side_effect=lambda k, d=None: "false" if k == "mcp.mcp_enabled" else d):
            backend._ensure_session(mcp_url="http://127.0.0.1:1/mcp", document_url="file:///doc.odt")
        with patch("plugin.framework.config.get_config", side_effect=lambda k, d=None: "true" if k == "mcp.mcp_enabled" else d):
            backend._ensure_session(mcp_url="http://127.0.0.1:1/mcp", document_url="file:///doc.odt")
        assert conn.send_request.call_count == 2
        conn.stop.assert_not_called()


def _prompt_events(result: object):
    backend = _bare_backend()
    backend._session_id = "sess-1"
    conn = _mock_conn()
    conn.send_request.return_value = result
    backend._conn = conn
    backend._ensure_connection = lambda stop_checker=None: None
    backend._ensure_session = lambda **kwargs: None
    q = queue.Queue()
    backend.send(queue=q, user_message="hi", document_context=None, document_url=None)
    return _drain(q)


class TestStopReason:
    def test_refusal_is_an_error(self):
        events = _prompt_events({"stopReason": "refusal"})
        assert (StreamQueueKind.ERROR) in ([event[0] for event in events])
        assert (StreamQueueKind.STREAM_DONE) not in ([event[0] for event in events])

    def test_max_tokens_keeps_the_partial_answer(self):
        events = _prompt_events({"stopReason": "max_tokens"})
        kinds = [event[0] for event in events]
        assert (StreamQueueKind.STATUS) in (kinds)
        assert (StreamQueueKind.STREAM_DONE) in (kinds)
        assert (StreamQueueKind.ERROR) not in (kinds)
        status = next(event[1] for event in events if event[0] == StreamQueueKind.STATUS and "token" in str(event[1]).lower())
        assert "token" in status.lower()

    def test_max_turn_requests_keeps_the_partial_answer(self):
        events = _prompt_events({"stopReason": "max_turn_requests"})
        kinds = [event[0] for event in events]
        assert any(event[0] == StreamQueueKind.STATUS and "request" in str(event[1]).lower() for event in events)
        assert (StreamQueueKind.STREAM_DONE) in (kinds)
        assert (StreamQueueKind.ERROR) not in (kinds)

    def test_end_turn_is_stream_done(self):
        kinds = [event[0] for event in _prompt_events({"stopReason": "end_turn"})]
        assert (StreamQueueKind.STREAM_DONE) in (kinds)
        assert (StreamQueueKind.ERROR) not in (kinds)

    def test_cancelled_is_stopped(self):
        kinds = [event[0] for event in _prompt_events({"stopReason": "cancelled"})]
        assert (StreamQueueKind.STOPPED) in (kinds)
        assert (StreamQueueKind.STREAM_DONE) not in (kinds)

    def test_non_dict_result_is_an_error(self):
        kinds = [event[0] for event in _prompt_events(["not-a-result"])]
        assert (StreamQueueKind.ERROR) in (kinds)
        assert (StreamQueueKind.STREAM_DONE) not in (kinds)

    def test_missing_result_is_stream_done(self):
        kinds = [event[0] for event in _prompt_events(None)]
        assert (StreamQueueKind.STREAM_DONE) in (kinds)


class TestSessionCwd:
    def test_file_url_uses_the_document_directory(self, tmp_path):
        doc = tmp_path / "note.odt"
        doc.write_text("x", encoding="utf-8")
        backend = _bare_backend()
        conn = _mock_conn()
        conn.send_request.return_value = {"sessionId": "s"}
        backend._conn = conn
        backend._ensure_session(document_url=doc.as_uri())
        params = conn.send_request.call_args[0][1]
        assert params["cwd"] == str(tmp_path)

    def test_unsaved_document_uses_home(self, monkeypatch):
        monkeypatch.setattr("plugin.acp.acp_backend.os.path.expanduser", lambda path: "/home/sentinel" if path == "~" else path)
        monkeypatch.setattr("plugin.acp.acp_backend.os.getcwd", lambda: "/launched/from/here")
        backend = _bare_backend()
        conn = _mock_conn()
        conn.send_request.return_value = {"sessionId": "s"}
        backend._conn = conn
        backend._ensure_session(document_url="private:factory/swriter")
        params = conn.send_request.call_args[0][1]
        assert params["cwd"] == "/home/sentinel"

    def test_localhost_file_url_uses_the_document_directory(self, tmp_path):
        from plugin.acp.acp_backend import _directory_from_document_url

        doc = tmp_path / "note.odt"
        doc.write_text("x", encoding="utf-8")
        url = "file://localhost" + doc.as_uri()[len("file://") :]
        assert (_directory_from_document_url(url)) == (str(tmp_path))

    def test_remote_file_host_is_not_a_local_directory(self, monkeypatch):
        from plugin.acp.acp_backend import _directory_from_document_url

        monkeypatch.setattr("plugin.acp.acp_backend.os.name", "posix")
        monkeypatch.setattr("plugin.acp.acp_backend.os.path.isdir", lambda path: True)
        assert (_directory_from_document_url("file://server/share/doc.odt")) is None

    def test_unc_file_url_on_windows(self, monkeypatch):
        import ntpath
        from plugin.acp.acp_backend import _directory_from_document_url

        monkeypatch.setattr("plugin.acp.acp_backend.os.name", "nt")
        monkeypatch.setattr("plugin.acp.acp_backend.os.path.dirname", ntpath.dirname)
        unc_dir = ntpath.dirname(r"\\server\share\doc.odt")
        monkeypatch.setattr("plugin.acp.acp_backend.os.path.isdir", lambda path: path == unc_dir)
        assert (_directory_from_document_url("file://server/share/doc.odt")) == (unc_dir)

    def test_unc_missing_directory_is_none(self, monkeypatch):
        import ntpath
        from plugin.acp.acp_backend import _directory_from_document_url

        monkeypatch.setattr("plugin.acp.acp_backend.os.name", "nt")
        monkeypatch.setattr("plugin.acp.acp_backend.os.path.dirname", ntpath.dirname)
        monkeypatch.setattr("plugin.acp.acp_backend.os.path.isdir", lambda path: False)
        assert (_directory_from_document_url("file://server/share/doc.odt")) is None



class TestReplyAndCancelDoNotSwallow:
    def test_reject_propagates_send_response_error(self):
        backend = _bare_backend()
        conn = MagicMock()
        conn.send_response.side_effect = TypeError("not json")
        with pytest.raises(TypeError, match="not json"):
            backend._dispatch_notification(conn, "fs/read_text_file", {}, 1, queue.Queue())

    def test_stop_propagates_cancel_send_error(self):
        backend = _bare_backend()
        conn = MagicMock()
        conn.is_alive = True
        backend._conn = conn
        backend._session_id = "sess-1"
        backend._prompt_inflight = True
        conn.send_notification.side_effect = TypeError("not json")
        with pytest.raises(TypeError, match="not json"):
            backend.stop()


def test_stop_during_begin_request_cannot_queue_cancel_first():
    backend = _bare_backend()
    backend._session_id = "sess-1"
    backend._ensure_connection = lambda stop_checker=None: None
    backend._ensure_session = lambda **kwargs: None

    entered_begin = threading.Event()
    release_begin = threading.Event()
    events_log = []

    class FakeConn:
        is_alive = True
        is_healthy = True

        def begin_request(self, method, params=None):
            events_log.append(("begin", method))
            entered_begin.set()
            release_begin.wait(timeout=5)
            return (method, params)

        def await_response(self, handle, timeout=120):
            return {"stopReason": "end_turn"}

        def send_notification(self, method, params=None):
            events_log.append(("notify", method))

        def set_notification_callback(self, callback):
            pass

        def stop(self):
            pass

    conn = FakeConn()
    backend._conn = conn

    q = queue.Queue()
    t_send = threading.Thread(
        target=backend.send,
        kwargs={"queue": q, "user_message": "hi", "document_context": None, "document_url": None},
    )
    t_send.start()

    assert entered_begin.wait(timeout=5)
    t_stop = threading.Thread(target=backend.stop)
    t_stop.start()

    time.sleep(0.05)
    assert ("notify", "session/cancel") not in events_log

    release_begin.set()
    t_stop.join(timeout=5)
    t_send.join(timeout=5)

    assert not t_stop.is_alive()
    assert not t_send.is_alive()
    assert ("begin", "session/prompt") in events_log
    assert ("notify", "session/cancel") in events_log
    idx_begin = events_log.index(("begin", "session/prompt"))
    idx_cancel = events_log.index(("notify", "session/cancel"))
    assert idx_begin < idx_cancel


def test_send_prompt_timeout_shuts_down_connection():
    backend = _bare_backend()
    backend._session_id = "sess-1"
    backend._ensure_connection = lambda stop_checker=None: None
    backend._ensure_session = lambda **kwargs: None

    conn = _mock_conn()
    conn.send_request.side_effect = ACPTimeoutError("ACP request session/prompt timed out after 600s")
    backend._conn = conn

    q = queue.Queue()
    backend.send(queue=q, user_message="hi", document_context=None, document_url=None)

    kinds = [event[0] for event in _drain(q)]
    assert StreamQueueKind.ERROR in kinds
    conn.stop.assert_called_once()
    assert backend._conn is None


def test_finished_send_cancels_an_unanswered_permission():
    backend = _bare_backend()
    backend._session_id = "sess-1"
    backend._ensure_connection = lambda stop_checker=None: None
    backend._ensure_session = lambda **kwargs: None

    conn = _mock_conn()
    conn.send_request.return_value = {"stopReason": "end_turn"}
    backend._conn = conn
    backend._pending_permissions[4] = (conn, [])

    q = queue.Queue()
    backend.send(queue=q, user_message="hi", document_context=None, document_url=None)

    assert not backend._pending_permissions
    conn.send_response.assert_called_once_with(4, result={"outcome": {"outcome": "cancelled"}})
    conn.stop.assert_not_called()
    assert backend._session_id == "sess-1"


def test_unhealthy_connection_is_dropped_and_replaced():
    backend = _bare_backend()
    backend._binary_path = "/usr/bin/test-agent"
    backend._extra_args = []

    old_conn = _mock_conn(alive=True, healthy=False)
    backend._conn = old_conn
    backend._ready = True

    new_conn = _mock_conn(alive=True, healthy=True)
    with patch("plugin.acp.acp_backend.ACPConnection", return_value=new_conn):
        backend._ensure_connection()

    old_conn.stop.assert_called_once()
    assert backend._conn is new_conn


def test_prompt_runtime_error_on_unhealthy_conn_shuts_down():
    backend = _bare_backend()
    backend._session_id = "sess-1"
    backend._ensure_connection = lambda stop_checker=None: None
    backend._ensure_session = lambda **kwargs: None

    conn = _mock_conn(alive=True, healthy=True)

    def fail_prompt(*args, **kwargs):
        conn.is_healthy = False
        raise RuntimeError("crashed")

    conn.send_request.side_effect = fail_prompt
    backend._conn = conn

    q = queue.Queue()
    backend.send(queue=q, user_message="hi", document_context=None, document_url=None)

    conn.stop.assert_called_once()
    assert backend._conn is None



