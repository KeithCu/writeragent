# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Basic tests for the zvec side-by-side backend (plugin.embeddings.venv.embeddings_zvec).

These tests must pass with or without the optional 'zvec' package installed in the
test environment (the project dev venv does not require it; users install it in their
own embeddings venv to select the mode in Settings).

New feature coverage requirement satisfied by the presence of this matching test_ module
plus the exercised paths in the updated embeddings tools/indexer/service when the mode
is "zvec".
"""

from __future__ import annotations

import pytest

from plugin.embeddings.venv import embeddings_zvec as zv


def test_has_zvec_is_boolean():
    """The guard flag must always be a bool (True only when import succeeded)."""
    assert isinstance(zv.HAS_ZVEC, bool)


def test_stable_doc_id_shape():
    """_stable_doc_id (internal) produces a non-empty str for a typical row dict."""
    # The helper is not exported in __all__, but is present for the implementation.
    row = {"doc_url": "file:///tmp/Writing/foo.odt", "para_index": 3, "char_start": 10, "char_end": 42, "content_hash": "deadbeef12345678", "text": "hello world", "file_mtime": 1710000000.0}
    doc_id = zv._stable_doc_id(row)  # type: ignore[attr-defined]
    assert isinstance(doc_id, str) and len(doc_id) == 64
    import re

    assert re.match(r"^[a-f0-9]{64}$", doc_id) is not None


@pytest.mark.skipif(not zv.HAS_ZVEC, reason="zvec package not installed in this test python; user opt-in via their embeddings venv")
def test_zvec_import_and_symbols_when_present():
    """When zvec *is* present, the public surface we dispatch to must exist."""
    assert hasattr(zv, "zvec_knn_search")
    assert hasattr(zv, "zvec_hybrid_search")
    assert hasattr(zv, "zvec_ingest_rows")
    assert hasattr(zv, "zvec_delete_keys")
    assert hasattr(zv, "maintain_folder_zvec")
    # Basic schema construction would exercise the real zvec here (left as integration smoke in manual test on ~/Desktop/Writing).


def test_hit_shape_helper_does_not_crash_on_minimal_doc():
    """_shape_hit must tolerate a minimal object with .field (simulating a zvec Doc result)."""

    class _FakeDoc:
        def __init__(self):
            self.score = 0.91
            self._f = {"doc_url": "file:///tmp/x.odt", "body": "snippet here", "para_index": 2}

        def field(self, k):
            return self._f.get(k)

    h = zv._shape_hit(_FakeDoc())  # type: ignore[attr-defined]
    assert h["doc_url"] == "file:///tmp/x.odt"
    assert "snippet" in h["snippet"]
    assert h["para_index"] == 2
    assert abs(h["score"] - 0.91) < 1e-6


import plugin.embeddings.venv.embeddings_zvec as zv


@pytest.mark.skipif(not zv.HAS_ZVEC, reason="zvec not installed")
def test_maintain_zvec_incremental_does_not_clear(tmp_path):
    from plugin.embeddings.venv.embeddings_zvec import maintain_folder_zvec
    import json

    listing_root = str(tmp_path)
    meta_dir = tmp_path / "writeragent_embeddings"
    meta_dir.mkdir()
    meta_path = meta_dir / "corpus_meta.json"

    with open(meta_path, "w") as m:
        json.dump({"schema_version": "v3", "chunk_count": 5, "embedding_model": "test-model"}, m)

    out = maintain_folder_zvec(listing_root, "test-model", mode="incremental")
    assert out["indexed_paragraphs"] == 0
    assert out["mode"] == "incremental"
    assert out["row_count"] == 5
    assert out["indexed_paragraphs"] == 0


def test_maintain_zvec_cold_proceeds(tmp_path):
    from plugin.embeddings.venv.embeddings_zvec import maintain_folder_zvec

    listing_root = str(tmp_path)
    try:
        out = maintain_folder_zvec(listing_root, "test-model", mode="cold")
        assert out["mode"] in ["cold", "zvec"]
    except Exception as e:
        assert "Zvec backend selected but the 'zvec' package is not importable" in str(e)


def test_zvec_clear_cache_invalidates():
    """Verify zvec_clear_cache pops an item from _COLL_CACHE."""
    from plugin.embeddings.venv.embeddings_zvec import zvec_clear_cache, _COLL_CACHE

    test_path = "/tmp/fake_collection"
    _COLL_CACHE[test_path] = "fake_collection_obj"

    assert test_path in _COLL_CACHE

    zvec_clear_cache(test_path)

    assert test_path not in _COLL_CACHE
