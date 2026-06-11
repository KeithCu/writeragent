# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for search_nearby_files tool."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from plugin.doc.document_research_fts_tool import SearchNearbyFiles


def _ctx():
    ctx = MagicMock()
    ctx.ctx = MagicMock()
    ctx.doc = MagicMock()
    ctx.services = MagicMock()
    return ctx


def test_search_nearby_files_disabled():
    tool = SearchNearbyFiles()
    with patch("plugin.framework.constants.document_research_uses_folder_fts", return_value=False):
        result = tool.execute(_ctx(), query="web search")
    assert result.get("status") == "error"
    assert result.get("code") == "FOLDER_FTS_DISABLED"


def test_search_nearby_files_indexing_status():
    tool = SearchNearbyFiles()
    with patch("plugin.framework.constants.document_research_uses_folder_fts", return_value=True):
        with patch("plugin.framework.queue_executor.execute_on_main_thread", side_effect=lambda fn: fn()):
            with patch(
                "plugin.doc.folder_fts_cache.resolve_fts_context",
                return_value=("key", MagicMock(), MagicMock(), "/tmp/folder"),
            ):
                with patch("plugin.doc.folder_fts_cache.fts_index_is_empty", return_value=True):
                    with patch("plugin.doc.folder_fts_indexer.ensure_fts_wakeup") as wakeup_mock:
                        result = tool.execute(_ctx(), query="web search")
    assert result.get("status") == "indexing"
    assert result.get("hits") == []
    wakeup_mock.assert_called_once()
