# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""In-process mock-LLM helpers for native sidebar tests (not shipped)."""

from __future__ import annotations

import os
import threading
import time
import unittest
from http.server import ThreadingHTTPServer
from typing import Any

from scripts.mock_llm_server import MOCK_MODEL_ID, MockLLMConfig, make_handler_class


def mock_config(config: Any, **flags: Any) -> Any:
    """Mutate a ``MockLLMConfig`` (or similar) in place and return it."""
    for key, value in flags.items():
        setattr(config, key, value)
    return config


class MockSidebarSession:
    """Running mock LLM + saved WriterAgent endpoint/model to restore on close."""

    def __init__(self, httpd: ThreadingHTTPServer, thread: threading.Thread, base_url: str, saved: dict[str, Any]) -> None:
        self.httpd = httpd
        self.thread = thread
        self.base_url = base_url
        self.saved = saved


def start_mock_sidebar_session(*, delay_ms: int = 20, offline: bool = True, **flags: Any) -> MockSidebarSession:
    """Bind an ephemeral mock and point config at ``writeragent-mock``."""
    from plugin.framework.client.model_fetcher import get_text_model, set_text_model
    from plugin.framework.config import (
        get_api_key_for_endpoint,
        get_current_endpoint,
        set_api_key_for_endpoint,
        set_config,
    )

    config = MockLLMConfig(delay_ms=delay_ms, offline=offline)
    mock_config(config, **flags)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler_class(config))
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    host, port = httpd.server_address[:2]
    base_url = "http://%s:%s" % (host, port)

    saved = {
        "endpoint": get_current_endpoint(),
        "text_model": get_text_model(),
        "api_key": get_api_key_for_endpoint(base_url),
    }
    set_config("endpoint", base_url)
    set_text_model(MOCK_MODEL_ID, update_lru=False)
    if not get_api_key_for_endpoint(base_url):
        set_api_key_for_endpoint(base_url, "mock-key")
    # Live sidebar is the OXT process. get_config caches mtime checks for 2s.
    if os.environ.get("WRITERAGENT_UNO_USER_PROFILE") == "1":
        time.sleep(2.1)
    return MockSidebarSession(httpd, thread, base_url, saved)


def stop_mock_sidebar_session(session: MockSidebarSession | None) -> None:
    if session is None:
        return
    from plugin.framework.client.model_fetcher import set_text_model
    from plugin.framework.config import set_api_key_for_endpoint, set_config

    try:
        session.httpd.shutdown()
        session.thread.join(timeout=2)
    except Exception:
        pass
    saved = session.saved
    if saved.get("endpoint") is not None:
        set_config("endpoint", saved["endpoint"])
    if saved.get("text_model"):
        set_text_model(saved["text_model"], update_lru=False)
    set_api_key_for_endpoint(session.base_url, saved.get("api_key") or "")


def require_send_listener(*, skip_if_missing: bool = True):
    """Live ``SendButtonListener``. Skip or fail if the chat deck is not wired."""
    from plugin.chatbot.sidebar_test_hooks import send_listener

    sl = send_listener()
    if sl is None:
        msg = "WriterAgent chat sidebar not wired (make test-mock-sidebar uses your LO profile)"
        if skip_if_missing:
            raise unittest.SkipTest(msg)
        raise AssertionError(msg)
    return sl
