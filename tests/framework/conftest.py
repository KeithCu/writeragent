# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Framework test fixtures (Layer B UNO thread-safety enforcement)."""

import pytest

from tests.framework.thread_safety import UnoThreadSafetySession


@pytest.fixture
def uno_thread_safety(monkeypatch):
    """Opt-in Layer B: real cross-thread marshal + thread-affine UNO mocks.

    - Clears WRITERAGENT_TESTING so main.call enqueues work.
    - Starts a synthetic main pump thread (designated UNO main).
    - Use session.make_mock(...) for objects that must only be touched from that thread.
    """
    monkeypatch.delenv("WRITERAGENT_TESTING", raising=False)
    session = UnoThreadSafetySession()
    try:
        yield session
    finally:
        session.close()
