# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for trusted embeddings RPC stubs in venv_sandbox."""

from __future__ import annotations

from unittest.mock import patch

from plugin.scripting.venv.venv_sandbox import run_sandboxed_code

_HYBRID_SEARCH_STUB = """\
from plugin.embeddings.venv.embeddings_index import hybrid_search as _search
result = _search(
    data["db_path"],
    data["query"],
    data["k"],
    model_name=data["model"],
    near_slop=data.get("near_slop", 10),
    doc_url_filter=data.get("doc_url_filter"),
)
"""


def test_trusted_hybrid_search_stub_dispatches():
    payload = {
        "db_path": "/tmp/corpus.db",
        "query": "dspy",
        "k": 20,
        "model": "all-MiniLM-L6-v2",
        "near_slop": 10,
    }
    fake_hits = {"hits": [{"doc_url": "file:///a.odt", "score": 0.5, "snippet": "dspy"}]}
    with patch("plugin.embeddings.venv.embeddings_index.hybrid_search", return_value=fake_hits) as mock_search:
        response = run_sandboxed_code(_HYBRID_SEARCH_STUB, data=payload)

    assert response["status"] == "ok"
    assert response["result"]["hits"][0]["doc_url"] == "file:///a.odt"
    mock_search.assert_called_once_with(
        "/tmp/corpus.db",
        "dspy",
        20,
        model_name="all-MiniLM-L6-v2",
        near_slop=10,
        doc_url_filter=None,
        use_mmr=True,
        rerank_model=None,
        search_mode="hybrid",
    )
