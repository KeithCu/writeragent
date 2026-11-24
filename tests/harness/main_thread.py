# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Reset the shared MainThread executor between unit tests."""

from __future__ import annotations

import queue

from plugin.framework.queue_executor import Policy, main


def reset_main_thread() -> None:
    """Restore ``main`` to a clean production policy with no context, service, or queued posts."""
    # A mock ctx reaches main whenever a test triggers bootstrap.
    # The mock AsyncCallback then makes main.post queue work that nothing runs.
    main.install(Policy.production, designated=None, poke=None)
    with main._init_lock:
        main._ctx = None
        main._initialized = False
        main._async_callback_service = None
        main._callback_instance = None
        main._logged_callback_problem = False
    # execute()'s timeout marks items cancelled without dequeuing them, and a mocked poke may not run process_queue before the next test.
    while not main._work_queue.empty():
        try:
            main._work_queue.get_nowait()
        except queue.Empty:
            break
    # A post with no AsyncCallback waits in _pending_posts; the next test's post() would flush it.
    with main._pending_lock:
        main._pending_posts.clear()
        main._pending_lock.notify_all()
