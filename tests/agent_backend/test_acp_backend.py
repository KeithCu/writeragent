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
import unittest
from unittest.mock import patch

from plugin.agent_backend.acp_backend import ACPBackend
from plugin.agent_backend.claude_simple import ClaudeBackend
from plugin.agent_backend.grok_simple import GrokBackend
from plugin.agent_backend.hermes_simple import HermesBackend
from plugin.agent_backend.opencode_simple import OpenCodeBackend
from plugin.framework.async_stream import StreamQueueKind


def _config_get(path="", args=""):
    def getter(key, default=None):
        values = {
            "agent_backend.path": path,
            "agent_backend.args": args,
        }
        return values.get(key, default)

    return getter


class TestDefaultExtraArgs(unittest.TestCase):
    """Default CLI args apply only when settings args are empty and the basename matches."""

    def test_hermes_defaults_when_settings_args_empty(self):
        with (
            patch("plugin.framework.config.get_config", side_effect=_config_get(path="/usr/bin/hermes", args="")),
            patch("os.path.isfile", return_value=True),
            patch("shutil.which", return_value=None),
        ):
            backend = HermesBackend()
        self.assertEqual(backend._binary_path, "/usr/bin/hermes")
        self.assertEqual(backend._extra_args, ["acp"])

    def test_hermes_settings_args_win(self):
        with (
            patch("plugin.framework.config.get_config", side_effect=_config_get(path="/usr/bin/hermes", args="--keep-going")),
            patch("os.path.isfile", return_value=True),
            patch("shutil.which", return_value=None),
        ):
            backend = HermesBackend()
        self.assertEqual(backend._extra_args, ["--keep-going"])

    def test_hermes_unrelated_basename_skips_defaults(self):
        with (
            patch("plugin.framework.config.get_config", side_effect=_config_get(path="/opt/my-wrapper", args="")),
            patch("os.path.isfile", return_value=True),
            patch("shutil.which", return_value=None),
        ):
            backend = HermesBackend()
        self.assertEqual(backend._binary_path, "/opt/my-wrapper")
        self.assertEqual(backend._extra_args, [])

    def test_opencode_defaults_when_settings_args_empty(self):
        with (
            patch("plugin.framework.config.get_config", side_effect=_config_get(path="/usr/bin/opencode", args="")),
            patch("os.path.isfile", return_value=True),
            patch("shutil.which", return_value=None),
        ):
            backend = OpenCodeBackend()
        self.assertEqual(backend._extra_args, ["acp"])

    def test_opencode_settings_args_win(self):
        with (
            patch("plugin.framework.config.get_config", side_effect=_config_get(path="/usr/bin/opencode", args="serve --port 4096")),
            patch("os.path.isfile", return_value=True),
            patch("shutil.which", return_value=None),
        ):
            backend = OpenCodeBackend()
        self.assertEqual(backend._extra_args, ["serve", "--port", "4096"])

    def test_grok_defaults_when_settings_args_empty(self):
        with (
            patch("plugin.framework.config.get_config", side_effect=_config_get(path="/usr/bin/grok", args="")),
            patch("os.path.isfile", return_value=True),
            patch("shutil.which", return_value=None),
        ):
            backend = GrokBackend()
        self.assertEqual(backend._extra_args, ["--no-auto-update", "agent", "stdio"])

    def test_grok_prefix_basename_gets_defaults(self):
        with (
            patch("plugin.framework.config.get_config", side_effect=_config_get(path="/usr/local/bin/grok-cli", args="")),
            patch("os.path.isfile", return_value=True),
            patch("shutil.which", return_value=None),
        ):
            backend = GrokBackend()
        self.assertEqual(backend._binary_path, "/usr/local/bin/grok-cli")
        self.assertEqual(backend._extra_args, ["--no-auto-update", "agent", "stdio"])

    def test_grok_settings_args_win(self):
        with (
            patch("plugin.framework.config.get_config", side_effect=_config_get(path="/usr/bin/grok", args="agent stdio")),
            patch("os.path.isfile", return_value=True),
            patch("shutil.which", return_value=None),
        ):
            backend = GrokBackend()
        self.assertEqual(backend._extra_args, ["agent", "stdio"])

    def test_claude_has_no_default_extra_args(self):
        with (
            patch("plugin.framework.config.get_config", side_effect=_config_get(path="/usr/bin/claude-code-acp-rs", args="")),
            patch("os.path.isfile", return_value=True),
            patch("shutil.which", return_value=None),
        ):
            backend = ClaudeBackend()
        self.assertEqual(backend._extra_args, [])
        self.assertEqual(backend.get_default_extra_args(), [])


class TestBinaryDiscovery(unittest.TestCase):
    """Config path and PATH / home-dir fallback still resolve the binary."""

    def test_config_path_used_when_file_exists(self):
        with (
            patch("plugin.framework.config.get_config", side_effect=_config_get(path="/custom/bin/hermes", args="")),
            patch("os.path.isfile", return_value=True),
            patch("shutil.which", return_value="/usr/bin/hermes"),
        ):
            backend = HermesBackend()
        self.assertEqual(backend._binary_path, "/custom/bin/hermes")
        self.assertEqual(backend._extra_args, ["acp"])

    def test_which_used_when_config_path_empty(self):
        with (
            patch("plugin.framework.config.get_config", side_effect=_config_get(path="", args="")),
            patch("os.path.isfile", return_value=True),
            patch("shutil.which", return_value="/usr/bin/opencode"),
        ):
            backend = OpenCodeBackend()
        self.assertEqual(backend._binary_path, "/usr/bin/opencode")
        self.assertEqual(backend._extra_args, ["acp"])

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
        self.assertEqual(backend._binary_path, home_bin)
        self.assertEqual(backend._extra_args, ["acp"])

    def test_is_available_path_fallback_applies_defaults(self):
        with (
            patch("plugin.framework.config.get_config", side_effect=_config_get(path="", args="")),
            patch("os.path.isfile", return_value=False),
            patch("os.access", return_value=False),
            patch("shutil.which", return_value=None),
        ):
            backend = HermesBackend()
        self.assertIsNone(backend._binary_path)
        with (
            patch("plugin.framework.config.get_config", side_effect=_config_get(path="", args="")),
            patch("os.path.isfile", return_value=False),
            patch("shutil.which", return_value="/opt/path/hermes"),
        ):
            self.assertTrue(backend.is_available(None))
        self.assertEqual(backend._binary_path, "/opt/path/hermes")
        self.assertEqual(backend._extra_args, ["acp"])


class TestMergedUpdateHandler(unittest.TestCase):
    """Session and agent updates share one helper for list and dict content."""

    def setUp(self):
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
        self.assertEqual(self._events(self.backend._handle_acp_update, update), expected)
        self.assertEqual(self._events(self.backend._handle_session_update, update), expected)
        self.assertEqual(self._events(self.backend._handle_agent_update, update), expected)

    def test_dict_content_text(self):
        update = {"content": {"type": "text", "text": "Hi"}}
        self.assertEqual(
            self._events(self.backend._handle_session_update, update),
            [(StreamQueueKind.CHUNK, "Hi")],
        )
        self.assertEqual(
            self._events(self.backend._handle_agent_update, update),
            [(StreamQueueKind.CHUNK, "Hi")],
        )

    def test_dict_content_tool_call(self):
        item = {"type": "tool_call", "name": "search"}
        update = {"content": item}
        self.assertEqual(
            self._events(self.backend._handle_acp_update, update),
            [(StreamQueueKind.TOOL_CALL, item)],
        )

    def test_dict_content_tool_result(self):
        item = {"type": "tool_result", "content": "done"}
        update = {"content": item}
        self.assertEqual(
            self._events(self.backend._handle_acp_update, update),
            [(StreamQueueKind.TOOL_RESULT, item)],
        )

    def test_missing_or_unknown_content_is_noop(self):
        self.assertEqual(self._events(self.backend._handle_acp_update, {"keys": "only"}), [])
        self.assertEqual(self._events(self.backend._handle_acp_update, {"content": "plain"}), [])
        self.assertEqual(self._events(self.backend._handle_acp_update, None), [])


if __name__ == "__main__":
    unittest.main()
