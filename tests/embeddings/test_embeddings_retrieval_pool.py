# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for shared hybrid retrieval pool sizing."""

from __future__ import annotations

import pytest

from plugin.embeddings.venv.embeddings_retrieval_pool import hybrid_retrieval_pool
from plugin.embeddings.venv.embeddings_llama_index import _llama_index_retrieval_pool


@pytest.mark.parametrize(
    ("k", "expected_final_k", "expected_fetch_k"),
    [
        (1, 1, 20),
        (5, 5, 20),
        (10, 10, 40),
        (20, 20, 50),
        (30, 30, 50),
        (0, 10, 40),
    ],
)
def test_hybrid_retrieval_pool(k: int, expected_final_k: int, expected_fetch_k: int) -> None:
    final_k, fetch_k = hybrid_retrieval_pool(k)
    assert final_k == expected_final_k
    assert fetch_k == expected_fetch_k


def test_llama_index_retrieval_pool_delegates_to_shared_helper() -> None:
    assert _llama_index_retrieval_pool(5) == hybrid_retrieval_pool(5)
