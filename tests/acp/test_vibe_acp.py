# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
"""Tests for the Mistral Vibe ACP backend adapter."""

import queue
from unittest.mock import MagicMock, patch

from plugin.acp.acp_backend import ACPBackend
from plugin.acp.vibe_simple import VibeBackend
from plugin.framework.async_stream import StreamQueueKind


class TestVibeIdentity:
    """Binary / identity hooks used by ACPBackend."""

    def test_binary_name_is_vibe_acp(self):
        assert (VibeBackend().get_binary_name()) == ("vibe-acp")

    def test_display_and_agent_name(self):
        backend = VibeBackend()
        assert (backend.backend_id) == ("vibe")
        assert (backend.get_display_name()) == ("Mistral Vibe (ACP)")
        assert (backend.get_agent_name()) == ("vibe")


class TestVibeIsAvailable:
    """Availability uses the shared PATH lookup for vibe-acp."""

    @patch("os.path.isfile", return_value=True)
    @patch("shutil.which", return_value="/usr/bin/vibe-acp")
    def test_available_when_vibe_acp_in_path(self, mock_which, mock_isfile):
        backend = VibeBackend()
        assert (backend.is_available(None))
        assert (backend._binary_path) == ("/usr/bin/vibe-acp")
        assert (backend._extra_args) == ([])

    @patch("shutil.which", return_value=None)
    @patch("os.path.isfile", return_value=False)
    def test_unavailable_when_no_binary(self, mock_isfile, mock_which):
        backend = VibeBackend()
        assert not (backend.is_available(None))


class TestVibeEnvVars:
    """Forward MISTRAL_API_KEY from general settings when present."""

    @patch("plugin.acp.vibe_simple.get_api_key_for_endpoint", return_value="sk-test")
    @patch("plugin.acp.vibe_simple.get_current_endpoint", return_value="https://api.mistral.ai")
    def test_forwards_mistral_api_key(self, mock_endpoint, mock_key):
        env = VibeBackend().get_env_vars()
        assert (env) == ({"MISTRAL_API_KEY": "sk-test"})

    @patch("plugin.acp.vibe_simple.get_api_key_for_endpoint", return_value="")
    @patch("plugin.acp.vibe_simple.get_current_endpoint", return_value="")
    def test_empty_when_no_key(self, mock_endpoint, mock_key):
        assert (VibeBackend().get_env_vars()) == ({})


class TestVibeUsesBaseSend:
    """contentBlocks drain lives on ACPBackend.send(); Vibe has no override."""

    def test_send_is_the_base_method(self):
        assert (VibeBackend.send) is (ACPBackend.send)

    def test_prompt_result_content_blocks_are_queued(self):
        backend = VibeBackend()
        backend._binary_path = "/usr/bin/vibe-acp"
        backend._session_id = "sess-1"
        backend._ensure_connection = MagicMock()
        backend._ensure_session = MagicMock()
        tool_call = {"type": "tool_call", "name": "read"}
        tool_result = {"type": "tool_result", "content": "ok"}
        mock_conn = MagicMock()
        mock_conn.send_request.return_value = {
            "stopReason": "end_turn",
            "contentBlocks": [
                {"type": "text", "text": "Hello from Vibe"},
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
        kinds = [event[0] for event in events]
        assert (StreamQueueKind.CHUNK) in (kinds)
        assert (StreamQueueKind.TOOL_CALL) in (kinds)
        assert (StreamQueueKind.TOOL_RESULT) in (kinds)
        assert (StreamQueueKind.STREAM_DONE) in (kinds)
        assert ((StreamQueueKind.CHUNK, "Hello from Vibe")) in (events)
        assert ((StreamQueueKind.TOOL_CALL, tool_call)) in (events)
        assert ((StreamQueueKind.TOOL_RESULT, tool_result)) in (events)


