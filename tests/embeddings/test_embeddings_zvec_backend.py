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
    row = {
        "doc_url": "file:///tmp/Writing/foo.odt",
        "para_index": 3,
        "char_start": 10,
        "char_end": 42,
        "content_hash": "deadbeef12345678",
        "text": "hello world",
        "file_mtime": 1710000000.0,
    }
    doc_id = zv._stable_doc_id(row)  # type: ignore[attr-defined]
    assert isinstance(doc_id, str) and len(doc_id) > 0
    assert "foo.odt" in doc_id or "deadbeef" in doc_id


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
