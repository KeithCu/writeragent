# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Framework test fixtures (Layer B UNO thread-safety enforcement)."""

import pytest

from plugin.framework import queue_executor as qe
from plugin.framework import thread_guard as tg
from tests.framework.thread_safety import UnoThreadSafetySession


@pytest.fixture(autouse=True)
def _reset_thread_safety_hooks():
    """Prevent Layer B hooks from leaking between framework tests."""
    yield
    qe.set_force_marshal_mode(False)
    qe.set_test_poke_handler(None)
    tg.set_designated_main_thread(None)


@pytest.fixture
def uno_thread_safety(monkeypatch):
    """Opt-in Layer B: real cross-thread marshal + thread-affine UNO mocks.

    - Clears WRITERAGENT_TESTING so execute_on_main_thread enqueues work.
    - Starts a synthetic main pump thread (designated UNO main).
    - Use session.make_mock(...) for objects that must only be touched from that thread.
    """
    monkeypatch.delenv("WRITERAGENT_TESTING", raising=False)
    session = UnoThreadSafetySession()
    try:
        yield session
    finally:
        session.close()
