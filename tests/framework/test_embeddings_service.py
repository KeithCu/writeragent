# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for plugin.framework.client.embeddings_service."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from plugin.framework.client import embeddings_service
from plugin.framework.constants import DEFAULT_EMBEDDING_MODEL, WORKER_POOL_EMBEDDINGS
from plugin.framework.errors import ToolExecutionError
from plugin.tests.testing_utils import setup_uno_mocks

setup_uno_mocks()


@pytest.fixture
def ctx():
    return MagicMock()


def test_knn_search_happy_path(ctx):
    worker_payload = {"hits": [{"doc_url": "file:///a.odt", "para_index": 0, "score": 0.9}]}
    with patch("plugin.framework.client.embeddings_service.run_code_in_user_venv", return_value={"status": "ok", "result": worker_payload}) as mock_run:
        with patch("plugin.framework.client.embeddings_service.configured_python_exec_timeout", return_value=30):
            result = embeddings_service.knn_search(ctx, "/tmp/index.db", "query", 5, model=DEFAULT_EMBEDDING_MODEL)
    assert result["hits"][0]["doc_url"] == "file:///a.odt"
    assert mock_run.call_args.kwargs["worker_pool"] == WORKER_POOL_EMBEDDINGS


def test_index_paragraphs_worker_error(ctx):
    with patch("plugin.framework.client.embeddings_service.run_code_in_user_venv", return_value={"status": "error", "message": "boom"}):
        with patch("plugin.framework.client.embeddings_service.configured_python_exec_timeout", return_value=30):
            with pytest.raises(ToolExecutionError, match="boom"):
                embeddings_service.index_paragraphs(ctx, "/tmp/index.db", [], model=DEFAULT_EMBEDDING_MODEL)
