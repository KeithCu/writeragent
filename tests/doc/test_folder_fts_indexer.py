# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for plugin.doc.folder_fts_indexer."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from plugin.doc import folder_fts_indexer


def test_enqueue_skipped_when_flag_off():
    with patch("plugin.doc.folder_fts_indexer.document_research_uses_folder_fts", return_value=False):
        with patch("plugin.doc.folder_fts_indexer.run_in_background") as bg_mock:
            folder_fts_indexer.enqueue_folder_fts_index(MagicMock(), MagicMock(), MagicMock())
            bg_mock.assert_not_called()


def test_enqueue_skipped_when_inflight():
    ctx = MagicMock()
    with patch("plugin.doc.folder_fts_indexer.document_research_uses_folder_fts", return_value=True):
        with patch(
            "plugin.doc.folder_fts_indexer.resolve_fts_context",
            return_value=("key", MagicMock(), MagicMock(), "/tmp/folder"),
        ):
            with patch("plugin.doc.folder_fts_indexer._try_enqueue", return_value=False):
                with patch("plugin.doc.folder_fts_indexer.run_in_background") as bg_mock:
                    folder_fts_indexer.enqueue_folder_fts_index(ctx, MagicMock(), MagicMock())
                    bg_mock.assert_not_called()


def test_index_worker_calls_maintain_rpc():
    ctx = MagicMock()
    with patch("plugin.doc.folder_fts_indexer.maintain_folder_fts_rpc") as maintain_mock:
        folder_fts_indexer._index_worker(ctx, "folderkey", "/tmp/listing")
        maintain_mock.assert_called_once_with(ctx, "/tmp/listing", mode="auto")


def test_index_worker_clears_inflight_on_failure():
    ctx = MagicMock()
    folder_fts_indexer._inflight.add("folderkey")
    with patch("plugin.doc.folder_fts_indexer.maintain_folder_fts_rpc", side_effect=RuntimeError("fail")):
        folder_fts_indexer._index_worker(ctx, "folderkey", "/tmp/listing")
    assert "folderkey" not in folder_fts_indexer._inflight
