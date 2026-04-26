"""Shared in-process coordination for LLM concurrency policies."""

from __future__ import annotations

from contextlib import contextmanager
import threading
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Generator

_AGENT_ACTIVE_LOCK = threading.Lock()
_AGENT_ACTIVE_COUNT = 0
_LLM_REQUEST_LOCK = threading.Lock()


@contextmanager
def agent_session() -> Generator[None, None, None]:
    """Mark a chat/agent session as active for its lifespan."""
    global _AGENT_ACTIVE_COUNT
    with _AGENT_ACTIVE_LOCK:
        _AGENT_ACTIVE_COUNT += 1
    try:
        yield
    finally:
        with _AGENT_ACTIVE_LOCK:
            _AGENT_ACTIVE_COUNT = max(0, _AGENT_ACTIVE_COUNT - 1)


def is_agent_active() -> bool:
    with _AGENT_ACTIVE_LOCK:
        return _AGENT_ACTIVE_COUNT > 0


@contextmanager
def llm_request_lane() -> Generator[None, None, None]:
    """Serialize LLM requests when callers choose to opt in."""
    _LLM_REQUEST_LOCK.acquire()
    try:
        yield
    finally:
        _LLM_REQUEST_LOCK.release()
