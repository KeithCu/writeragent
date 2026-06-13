# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for plugin.embeddings.embeddings_periodic."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

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
