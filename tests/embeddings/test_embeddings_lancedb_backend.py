# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Basic tests for the LanceDB side-by-side backend (plugin.embeddings.venv.embeddings_lancedb)."""

from __future__ import annotations

import pytest

from plugin.embeddings.venv import embeddings_lancedb as ld


def test_has_lancedb_is_boolean():
    """The guard flag must always be a bool (True only when import succeeded)."""
    assert isinstance(ld.HAS_LANCEDB, bool)


def test_stable_doc_id_shape():
    """_stable_doc_id produces a non-empty str for a typical row dict."""
    row = {
        "doc_url": "file:///tmp/Writing/foo.odt",
        "para_index": 3,
        "char_start": 10,
        "char_end": 42,
        "content_hash": "deadbeef12345678",
        "text": "hello world",
        "file_mtime": 1710000000.0,
    }
    doc_id = ld._stable_doc_id(row)
    assert isinstance(doc_id, str) and len(doc_id) > 0
    assert "foo.odt" in doc_id or "deadbeef" in doc_id


@pytest.mark.skipif(not ld.HAS_LANCEDB, reason="lancedb package not installed in this test python")
def test_lancedb_import_and_symbols_when_present():
    """When lancedb is present, the public surface we dispatch to must exist."""
    assert hasattr(ld, "lancedb_knn_search")
    assert hasattr(ld, "lancedb_hybrid_search")
    assert hasattr(ld, "lancedb_ingest_rows")
    assert hasattr(ld, "lancedb_delete_keys")
    assert hasattr(ld, "maintain_folder_lancedb")


def test_hit_shape_helper_does_not_crash_on_minimal_dict():
    """_shape_hit must tolerate a minimal dictionary representing a LanceDB search result."""
    doc = {
        "doc_url": "file:///tmp/x.odt",
        "body": "snippet here",
        "para_index": 2,
        "content_hash": "abc123hash",
        "_score": 0.91,
    }
    h = ld._shape_hit(doc)
    assert h["doc_url"] == "file:///tmp/x.odt"
    assert "snippet" in h["snippet"]
    assert h["para_index"] == 2
    assert abs(h["score"] - 0.91) < 1e-6


def test_hit_shape_helper_with_distance():
    """_shape_hit must tolerate _distance and convert it to score."""
    doc = {
        "doc_url": "file:///tmp/x.odt",
        "body": "snippet here",
        "para_index": 2,
        "content_hash": "abc123hash",
        "_distance": 0.25,
    }
    h = ld._shape_hit(doc)
    assert h["doc_url"] == "file:///tmp/x.odt"
    assert abs(h["score"] - 0.75) < 1e-6
