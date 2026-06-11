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
from plugin.tests.testing_utils import setup_uno_mocks

setup_uno_mocks()


@pytest.fixture
def ctx():
    return MagicMock()


def test_search_folder_fts_happy_path(ctx):
    worker_payload = {"hits": [{"doc_url": "file:///a.odt", "para_index": 0, "score": -1.2}], "match": 'NEAR("web" "search", 10)'}
    with patch("plugin.framework.client.folder_fts_service.run_code_in_user_venv", return_value={"status": "ok", "result": worker_payload}) as mock_run:
        with patch("plugin.framework.client.folder_fts_service.embeddings_worker_timeout_sec", return_value=120):
            result = folder_fts_service.search_folder_fts(ctx, "/tmp/fts5.db", "web search", 5, near_slop=10)
    assert result["hits"][0]["doc_url"] == "file:///a.odt"
    assert mock_run.call_args.kwargs["worker_pool"] == WORKER_POOL_EMBEDDINGS
    assert mock_run.call_args.kwargs["session_id"] == "embeddings:folder_fts"
    payload = mock_run.call_args.kwargs["data"]
    assert payload["fts_db_path"] == "/tmp/fts5.db"
    assert payload["query"] == "web search"


def test_maintain_folder_fts_uses_heartbeat(ctx):
    with patch(
        "plugin.framework.client.folder_fts_service.run_code_in_user_venv",
        return_value={"status": "ok", "result": {"mode": "cold", "indexed_paragraphs": 2}},
    ) as mock_run:
        with patch("plugin.framework.client.folder_fts_service.embeddings_worker_timeout_sec", return_value=120):
            result = folder_fts_service.maintain_folder_fts(ctx, "/tmp/folder", mode="auto")
    assert result["mode"] == "cold"
    assert mock_run.call_args.kwargs["allow_heartbeat"] is True
    assert mock_run.call_args.kwargs["worker_pool"] == WORKER_POOL_EMBEDDINGS


def test_search_worker_error(ctx):
    with patch("plugin.framework.client.folder_fts_service.run_code_in_user_venv", return_value={"status": "error", "message": "boom"}):
        with patch("plugin.framework.client.folder_fts_service.embeddings_worker_timeout_sec", return_value=120):
            with pytest.raises(ToolExecutionError, match="boom"):
                folder_fts_service.search_folder_fts(ctx, "/tmp/fts5.db", "q", 5)
