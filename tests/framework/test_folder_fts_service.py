# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for plugin.framework.client.folder_fts_service."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from plugin.framework.client import folder_fts_service
from plugin.framework.constants import WORKER_POOL_EMBEDDINGS
from plugin.framework.errors import ToolExecutionError


@pytest.fixture
def ctx():
    return MagicMock()


def test_search_folder_fts_happy_path(ctx, tmp_path):
    fts_db = str(tmp_path / "fts5.db")
    worker_payload = {"hits": [{"doc_url": "file:///a.odt", "para_index": 0, "score": -1.2}], "match": 'NEAR("web" "search", 10)'}
    with patch("plugin.framework.client.folder_fts_service.run_trusted_worker_action", return_value=worker_payload) as mock_run:
        with patch("plugin.framework.client.folder_fts_service.embeddings_worker_timeout_sec", return_value=120):
            result = folder_fts_service.search_folder_fts(ctx, fts_db, "web search", 5, near_slop=10)
    assert result["hits"][0]["doc_url"] == "file:///a.odt"
    assert mock_run.call_args.kwargs["worker_pool"] == WORKER_POOL_EMBEDDINGS
    assert mock_run.call_args.kwargs["session_id"] == "embeddings:folder_fts"
    payload = mock_run.call_args.kwargs["params"]
    assert payload["fts_db_path"] == fts_db
    assert payload["query"] == "web search"


def test_maintain_folder_fts_uses_heartbeat(ctx, tmp_path):
    folder = str(tmp_path / "folder")
    with patch(
        "plugin.framework.client.folder_fts_service.run_trusted_worker_action",
        return_value={"mode": "cold", "indexed_paragraphs": 2},
    ) as mock_run:
        with patch("plugin.framework.client.folder_fts_service.embeddings_worker_timeout_sec", return_value=120):
            result = folder_fts_service.maintain_folder_fts(ctx, folder, mode="auto")
    assert result["mode"] == "cold"
    assert mock_run.call_args.kwargs["allow_heartbeat"] is True
    assert mock_run.call_args.kwargs["worker_pool"] == WORKER_POOL_EMBEDDINGS


def test_search_worker_error(ctx, tmp_path):
    fts_db = str(tmp_path / "fts5.db")
    with patch(
        "plugin.framework.client.folder_fts_service.run_trusted_worker_action",
        side_effect=ToolExecutionError("boom", code="FOLDER_FTS_ERROR"),
    ):
        with patch("plugin.framework.client.folder_fts_service.embeddings_worker_timeout_sec", return_value=120):
            with pytest.raises(ToolExecutionError, match="boom"):
                folder_fts_service.search_folder_fts(ctx, fts_db, "q", 5)

