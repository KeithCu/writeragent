# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Throttle for child-side embedding maintain heartbeats."""
from __future__ import annotations

import time
from typing import Any, Callable

from plugin.framework.constants import EMBEDDINGS_HEARTBEAT_INTERVAL_S

__all__ = ["HeartbeatThrottle"]


class HeartbeatThrottle:
    _fn: Callable[[dict[str, Any]], None] | None
    _last: float

    def __init__(self, heartbeat_fn: Callable[[dict[str, Any]], None] | None) -> None:
        self._fn = heartbeat_fn
        self._last = float("-inf")

    def ping(self, payload: dict[str, Any]) -> None:
        if self._fn is None:
            return
        now = time.monotonic()
        if now - self._last < EMBEDDINGS_HEARTBEAT_INTERVAL_S:
            return
        self._last = now
        self._fn(payload)

    def force(self, payload: dict[str, Any]) -> None:
        if self._fn is None:
            return
        self._last = time.monotonic()
        self._fn(payload)
