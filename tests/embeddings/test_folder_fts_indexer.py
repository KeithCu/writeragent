# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for plugin.embeddings.folder_fts_indexer delegation."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from plugin.embeddings import folder_fts_indexer


def test_enqueue_delegates_to_corpus_indexer():
    ctx = MagicMock()
    services = MagicMock()
    model = MagicMock()
    with patch("plugin.embeddings.folder_fts_indexer.enqueue_folder_index") as enqueue_mock:
        folder_fts_indexer.enqueue_folder_fts_index(ctx, services, model)
        enqueue_mock.assert_called_once_with(ctx, services, model)


def test_wakeup_delegates_to_corpus_indexer():
    ctx = MagicMock()
    services = MagicMock()
    model = MagicMock()
    with patch("plugin.embeddings.folder_fts_indexer.ensure_index_wakeup") as wakeup_mock:
        folder_fts_indexer.ensure_fts_wakeup(ctx, services, model)
        wakeup_mock.assert_called_once_with(ctx, services, model)
