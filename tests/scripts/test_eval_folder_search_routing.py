# WriterAgent tests for scripts/eval_folder_search_routing.py
from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

_REPO = Path(__file__).resolve().parents[2]
_SCRIPTS = _REPO / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

from eval_folder_search_routing import (  # noqa: E402
    ALL_LABELED_QUERIES,
    LabeledQuery,
    classify_fts_vec_bucket,
    doc_basename,
    evaluate_query,
    matches_expected,
    run_eval,
    summarize_fts_vec_buckets,
)


def test_doc_basename_from_file_url() -> None:
    assert doc_basename("file:///home/user/part2.odt") == "part2.odt"


def test_matches_expected_substring() -> None:
    assert matches_expected("file:///x/cursor_for_libreoffice_part2.odt", "part2.odt")


def test_classify_fts_vec_bucket() -> None:
    row = {
        "expected": "part2.odt",
        "legs": {
            "fts": {"correct": True},
            "vec": {"correct": False},
        },
    }
    assert classify_fts_vec_bucket(row) == "fts_only"


def test_summarize_fts_vec_buckets() -> None:
    rows = [
        {"expected": "a.odt", "legs": {"fts": {"correct": True}, "vec": {"correct": True}}},
        {"expected": "b.odt", "legs": {"fts": {"correct": True}, "vec": {"correct": False}}},
        {"expected": None, "legs": {"fts": {"correct": False}, "vec": {"correct": False}}},
    ]
    counts = summarize_fts_vec_buckets(rows)
    assert counts["both"] == 1
    assert counts["fts_only"] == 1
    assert counts["neither"] == 0


def test_evaluate_query_mocks_legs(tmp_path: Path) -> None:
    db_path = tmp_path / "corpus.db"
    db_path.write_text("", encoding="utf-8")
    labeled = LabeledQuery("web search", "part2.odt", "short")

    def fake_hybrid(*_args, **_kwargs):
        return {"hits": [{"doc_url": "file:///x/cursor_for_libreoffice_part2.odt", "score": 0.9, "matched_by": ["fts", "vec"]}]}

    def fake_vec(*_args, **_kwargs):
        return {"hits": [{"doc_url": "file:///x/blog_draft.odt", "score": 0.8}]}

    def fake_fts(*_args, **_kwargs):
        return {"hits": [{"doc_url": "file:///x/cursor_for_libreoffice_part2.odt", "score": -1.0}]}

    with (
        patch("eval_folder_search_routing.hybrid_search", side_effect=fake_hybrid),
        patch("eval_folder_search_routing.knn_search", side_effect=fake_vec),
        patch("eval_folder_search_routing.search_folder_fts", side_effect=fake_fts),
    ):
        row = evaluate_query(
            labeled,
            db_path=db_path,
            model_name="test-model",
            k=1,
            near_slop=10,
            use_mmr=True,
            legs=("hybrid", "fts", "vec"),
        )

    assert row["legs"]["hybrid"]["correct"] is True
    assert row["legs"]["fts"]["correct"] is True
    assert row["legs"]["vec"]["correct"] is False
    assert classify_fts_vec_bucket(row) == "fts_only"


def test_run_eval_hybrid_mode(tmp_path: Path) -> None:
    folder = tmp_path / "Writing"
    folder.mkdir()
    cache = folder / "writeragent_embeddings"
    cache.mkdir()
    (cache / "corpus.db").write_text("", encoding="utf-8")
    (cache / "corpus_meta.json").write_text(
        json.dumps({"embedding_model": "m", "chunk_count": "2", "schema_version": "3"}),
        encoding="utf-8",
    )

    def fake_hybrid(_db, query, _k, **kwargs):
        expected = next((q.expected for q in ALL_LABELED_QUERIES if q.query == query), None)
        doc = f"file:///x/{expected or 'miss.odt'}"
        return {"hits": [{"doc_url": doc, "score": 0.5, "matched_by": ["fts"]}]}

    with patch("eval_folder_search_routing.hybrid_search", side_effect=fake_hybrid):
        payload = run_eval(folder, mode="hybrid", k=1, use_mmr=True)

    assert payload["status"] == "ok"
    summary = payload["summary"]["hybrid_routing"]
    assert summary["labeled"] > 0
    assert summary["correct"] == summary["labeled"]


def test_run_eval_requires_cache(tmp_path: Path) -> None:
    folder = tmp_path / "empty"
    folder.mkdir()
    with pytest.raises(Exception, match="No indexed cache"):
        run_eval(folder)


def test_labeled_query_sets_non_empty() -> None:
    assert len(ALL_LABELED_QUERIES) >= 30
