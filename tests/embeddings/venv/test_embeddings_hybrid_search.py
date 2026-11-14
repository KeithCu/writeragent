# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for hybrid_corpus_search cross-encoder rerank after RRF."""

from __future__ import annotations

from contextlib import ExitStack, contextmanager
from unittest.mock import MagicMock, patch

import numpy as np
import pytest

from plugin.embeddings.venv.embeddings_hybrid_search import hybrid_corpus_search  # noqa: E402

M = "plugin.embeddings.venv.embeddings_hybrid_search."


def _fused_candidates() -> list[dict]:
    return [
        {
            "chunk_id": 1,
            "doc_url": "file:///a.odt",
            "para_index": 0,
            "snippet": "streaming sidebar alpha",
            "score": 0.03,
            "matched_by": ["fts", "vec"],
            "embedding": np.array([1.0, 0.0, 0.0, 0.0], dtype=np.float32),
        },
        {
            "chunk_id": 2,
            "doc_url": "file:///b.odt",
            "para_index": 1,
            "snippet": "streaming sidebar beta",
            "score": 0.025,
            "matched_by": ["vec"],
            "embedding": np.array([0.99, 0.01, 0.0, 0.0], dtype=np.float32),
        },
        {
            "chunk_id": 3,
            "doc_url": "file:///c.odt",
            "para_index": 2,
            "snippet": "budget figures",
            "score": 0.02,
            "matched_by": ["fts"],
            "embedding": np.array([0.0, 1.0, 0.0, 0.0], dtype=np.float32),
        },
    ]


@contextmanager
def _patch_hybrid_search(fused: list[dict]):
    conn = MagicMock()
    with ExitStack() as stack:
        stack.enter_context(patch(f"{M}connect_corpus_db", return_value=conn))
        stack.enter_context(patch(f"{M}embed_texts", return_value={"vectors": [[1.0, 0.0, 0.0, 0.0]]}))
        stack.enter_context(patch(f"{M}fts_corpus_search", return_value=[]))
        stack.enter_context(patch(f"{M}vec0_search", return_value=[]))
        stack.enter_context(patch(f"{M}load_embeddings_for_candidates"))
        stack.enter_context(patch(f"{M}merge_hybrid_hits", return_value=list(fused)))
        stack.enter_context(
            patch(
                f"{M}expand_candidates_to_parent_paragraphs",
                side_effect=lambda _db, rows: rows,
            )
        )
        yield


@pytest.mark.parametrize(
    "n_fused,k,rerank_model,use_mmr,rerank_called,expected_urls",
    [
        pytest.param(
            3,
            2,
            "cross-encoder/ms-marco-MiniLM-L-6-v2",
            True,
            True,
            ["file:///c.odt", "file:///a.odt"],
            id="rerank-when-model-set",
        ),
        pytest.param(
            3,
            1,
            "cross-encoder/ms-marco-MiniLM-L-6-v2",
            True,
            False,
            ["file:///a.odt"],
            id="skip-when-k-is-one",
        ),
        pytest.param(
            2,
            2,
            None,
            False,
            False,
            ["file:///a.odt", "file:///b.odt"],
            id="skip-when-disabled",
        ),
    ],
)
def test_hybrid_corpus_search_rerank(
    n_fused: int,
    k: int,
    rerank_model: str | None,
    use_mmr: bool,
    rerank_called: bool,
    expected_urls: list[str],
) -> None:
    fused = _fused_candidates()[:n_fused]
    rerank_return = [fused[2], fused[0]] if n_fused == 3 else []

    with (
        _patch_hybrid_search(fused),
        patch(f"{M}cross_encoder_rerank_candidates", return_value=rerank_return) as rerank,
    ):
        result = hybrid_corpus_search(
            "/tmp/corpus.db",
            "streaming sidebar",
            k,
            model_name="test-model",
            use_mmr=use_mmr,
            rerank_model=rerank_model,
        )

    assert rerank.call_count == (1 if rerank_called else 0)
    assert [h["doc_url"] for h in result["hits"]] == expected_urls
