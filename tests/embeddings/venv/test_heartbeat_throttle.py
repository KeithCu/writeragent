# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for plugin.embeddings.venv.heartbeat_throttle."""

from __future__ import annotations

from unittest.mock import patch

from plugin.embeddings.venv.heartbeat_throttle import HeartbeatThrottle


def test_ping_suppresses_until_interval():
    recorded: list[dict[str, str]] = []
    hb = HeartbeatThrottle(recorded.append)

    with patch(
        "plugin.embeddings.venv.heartbeat_throttle.time.monotonic",
        side_effect=[0.0, 1.0, 5.0],
    ):
        hb.ping({"phase": "a"})
        assert recorded == [{"phase": "a"}]
        hb.ping({"phase": "b"})
        assert recorded == [{"phase": "a"}]
        hb.ping({"phase": "c"})
        assert recorded == [{"phase": "a"}, {"phase": "c"}]


def test_force_bypasses_interval():
    recorded: list[dict[str, str]] = []
    hb = HeartbeatThrottle(recorded.append)

    with patch(
        "plugin.embeddings.venv.heartbeat_throttle.time.monotonic",
        side_effect=[0.0, 0.0],
    ):
        hb.ping({"phase": "a"})
        hb.force({"phase": "forced"})
        assert recorded == [{"phase": "a"}, {"phase": "forced"}]


def test_none_callback_is_noop():
    hb = HeartbeatThrottle(None)
    hb.ping({})
    hb.force({})
