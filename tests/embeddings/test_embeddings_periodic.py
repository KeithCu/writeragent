# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for plugin.embeddings.embeddings_periodic."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from plugin.tests.testing_utils import setup_uno_mocks
setup_uno_mocks()

import plugin.embeddings.embeddings_periodic as periodic_mod
import pytest
from plugin.embeddings.embeddings_periodic import schedule_periodic_embeddings_indexer_once


@pytest.fixture(autouse=True)
def _reset_periodic_schedule_flag():
    periodic_mod._scheduled = False
    yield
    periodic_mod._scheduled = False


def test_schedule_periodic_indexer_skipped_when_off():
    with patch("plugin.framework.constants.folder_search_enabled", return_value=False):
        with patch("plugin.framework.worker_pool.run_in_background") as run_bg:
            schedule_periodic_embeddings_indexer_once(MagicMock())
            run_bg.assert_not_called()


def test_schedule_periodic_indexer_once_when_on():
    ctx = MagicMock()
    with patch("plugin.framework.constants.folder_search_enabled", return_value=True):
        with patch("plugin.framework.worker_pool.run_in_background") as run_bg:
            schedule_periodic_embeddings_indexer_once(ctx)
            schedule_periodic_embeddings_indexer_once(ctx)
            assert run_bg.call_count == 1
            assert run_bg.call_args[0][1] is ctx


def test_run_periodic_embeddings_indexer_marshals_uno_on_tick():
    from plugin.embeddings.embeddings_periodic import run_periodic_embeddings_indexer

    ctx = MagicMock()

    class StopLoop(Exception):
        pass

    doc_calls_during_marshal: list[bool] = []
    marshal_depth = [0]

    def mock_execute(fn, *args, **kwargs):
        marshal_depth[0] += 1
        try:
            return fn(*args, **kwargs)
        finally:
            marshal_depth[0] -= 1

    def tracking_get_active_document(_ctx):
        doc_calls_during_marshal.append(marshal_depth[0] > 0)
        raise StopLoop()

    with (
        patch("time.sleep", return_value=None),
        patch("plugin.framework.constants.folder_search_enabled", return_value=True),
        patch("plugin.framework.queue_executor.execute_on_main_thread", side_effect=mock_execute),
        patch("plugin.framework.uno_context.get_active_document", side_effect=tracking_get_active_document),
    ):
        with pytest.raises(StopLoop):
            run_periodic_embeddings_indexer(ctx)

    assert doc_calls_during_marshal, "get_active_document should run during periodic tick"
    assert all(doc_calls_during_marshal), "get_active_document must be marshaled to main thread"

