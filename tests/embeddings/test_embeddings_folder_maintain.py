# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for plugin.embeddings.venv.embeddings_folder_maintain."""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

from plugin.embeddings.embeddings_fs import ParagraphChunk, WriterFileEntry
from plugin.embeddings.venv import embeddings_folder_maintain as maintain


def _write_populated_meta(listing, model: str, *, sqlite: bool = True, collection: str | None = None, chunk_count: str = "4") -> None:
    base = listing / "writeragent_embeddings"
    base.mkdir(parents=True, exist_ok=True)
    if sqlite:
        (base / "corpus.db").write_text("sqlite", encoding="utf-8")
    (base / "corpus_meta.json").write_text(
        json.dumps(
            {
                "schema_version": "6",
                "embedding_model": model,
                "chunk_count": chunk_count,
                "dim": "384",
            }
        ),
        encoding="utf-8",
    )
    if collection:
        store = base / collection
        store.mkdir()
        (store / "segment").write_text("rows", encoding="utf-8")


def _chunk(doc_url: str, para_index: int, text: str) -> ParagraphChunk:
    return ParagraphChunk(
        doc_url=doc_url,
        para_index=para_index,
        char_start=0,
        char_end=len(text),
        text=text,
        content_hash=f"hash-{para_index}",
        file_mtime=1.0,
        doc_path="",
    )


def test_cold_build_ingests_one_file_at_a_time(tmp_path):
    """Cold build should not accumulate the whole folder before a single ingest RPC."""
    listing_root = str(tmp_path)
    files = [
        WriterFileEntry(path="/a.odt", url="file:///a.odt", modified=1.0, name="a.odt"),
        WriterFileEntry(path="/b.odt", url="file:///b.odt", modified=2.0, name="b.odt"),
    ]
    ingest_calls: list[int] = []

    def _fake_ingest_rows(
        root: str,
        model: str,
        rows: list,
        *,
        delete_keys=None,
        build_fts: bool,
        build_vectors: bool,
        **kwargs,
    ) -> dict:
        del root, model, delete_keys, build_fts, build_vectors
        ingest_calls.append(len(rows))
        return {"indexed": len(rows), "upserted": len(rows)}

    db_path = tmp_path / "writeragent_embeddings" / "corpus.db"
    db_path.parent.mkdir(parents=True)

    with (
        patch.object(maintain, "clear_folder_cache"),
        patch.object(maintain, "ensure_corpus_meta"),
        patch.object(maintain, "indexable_chunks_from_path", side_effect=[(1, [_chunk("file:///a.odt", 0, "a")]), (1, [_chunk("file:///b.odt", 0, "b")])]),
        patch.object(maintain, "_ingest_rows", side_effect=_fake_ingest_rows),
        patch.object(maintain, "sync_file_paragraph_state") as sync_mock,
        patch.object(maintain, "corpus_db_path", return_value=db_path),
        patch.object(maintain, "_write_row_count_meta"),
    ):
        result = maintain._cold_build(
            listing_root,
            "test-model",
            files,
            maintain._HeartbeatThrottle(None),
            build_fts=True,
            build_vectors=True,
        )

    assert ingest_calls == [1, 1]
    assert sync_mock.call_count == 2
    assert result["mode"] == "cold"
    assert result["indexed_paragraphs"] == 2
    assert result["upserted"] == 2
    assert result["files"] == 2


def test_cold_build_skips_ingest_for_empty_files(tmp_path):
    listing_root = str(tmp_path)
    files = [WriterFileEntry(path="/empty.odt", url="file:///empty.odt", modified=1.0, name="empty.odt")]

    with (
        patch.object(maintain, "clear_folder_cache"),
        patch.object(maintain, "ensure_corpus_meta"),
        patch.object(maintain, "indexable_chunks_from_path", return_value=(0, [])),
        patch.object(maintain, "_ingest_rows") as ingest_mock,
        patch.object(maintain, "sync_file_paragraph_state") as sync_mock,
        patch.object(maintain, "corpus_db_path", return_value=tmp_path / "writeragent_embeddings" / "corpus.db"),
        patch.object(maintain, "_write_row_count_meta"),
    ):
        result = maintain._cold_build(
            listing_root,
            "test-model",
            files,
            maintain._HeartbeatThrottle(None),
            build_fts=False,
            build_vectors=False,
        )

    ingest_mock.assert_not_called()
    sync_mock.assert_called_once()
    assert result["indexed_paragraphs"] == 0


def test_model_change_forces_cold_even_when_incremental(tmp_path):
    _write_populated_meta(tmp_path, "old-model")
    with (
        patch.object(maintain, "guess_indexable_paths", return_value=[]),
        patch.object(maintain, "_cold_build", return_value={"mode": "cold"}) as cold,
        patch.object(maintain, "_incremental_refresh") as incremental,
    ):
        result = maintain.maintain_folder_corpus(
            str(tmp_path),
            embedding_model="new-model",
            search_mode="embeddings",
            mode="incremental",
        )
    cold.assert_called_once()
    incremental.assert_not_called()
    assert result["mode"] == "cold"


def test_resolve_mode_model_change_and_non_dict_meta(tmp_path):
    _write_populated_meta(tmp_path, "old-model")
    assert maintain._resolve_mode(str(tmp_path), "old-model", "auto", build_vectors=True) == "incremental"
    assert maintain._resolve_mode(str(tmp_path), "new-model", "incremental", build_vectors=True) == "cold"
    assert maintain._resolve_mode(str(tmp_path), "new-model", "auto", build_vectors=False) == "incremental"

    meta = tmp_path / "writeragent_embeddings" / "corpus_meta.json"
    meta.write_text("[]", encoding="utf-8")
    assert maintain._resolve_mode(str(tmp_path), "new-model", "auto", build_vectors=True) == "incremental"


def test_zvec_model_change_clears_collection_before_maintain(tmp_path):
    _write_populated_meta(tmp_path, "old-model")
    with (
        patch(
            "plugin.embeddings.venv.embeddings_zvec.maintain_folder_zvec",
            return_value={"mode": "zvec"},
        ) as zvec,
        patch.object(maintain, "clear_folder_cache") as clear,
    ):
        maintain.maintain_folder_corpus(
            str(tmp_path),
            embedding_model="new-model",
            search_mode="zvec",
            mode="auto",
        )
    clear.assert_called_once_with(str(tmp_path))
    zvec.assert_called_once()


def test_zvec_same_model_does_not_clear_when_corpus_db_missing(tmp_path):
    """A zvec store has no corpus.db. Auto maintain must not cold-wipe it."""
    _write_populated_meta(tmp_path, "old-model", sqlite=False, collection="zvec")
    with (
        patch(
            "plugin.embeddings.venv.embeddings_zvec.maintain_folder_zvec",
            return_value={"mode": "zvec"},
        ) as zvec,
        patch.object(maintain, "clear_folder_cache") as clear,
    ):
        maintain.maintain_folder_corpus(
            str(tmp_path),
            embedding_model="old-model",
            search_mode="zvec",
            mode="auto",
        )
    clear.assert_not_called()
    zvec.assert_called_once()


def test_lancedb_same_model_does_not_clear_when_corpus_db_missing(tmp_path):
    _write_populated_meta(tmp_path, "old-model", sqlite=False, collection="lancedb")
    with (
        patch(
            "plugin.embeddings.venv.embeddings_lancedb.maintain_folder_lancedb",
            return_value={"mode": "lancedb"},
        ) as lance,
        patch.object(maintain, "clear_folder_cache") as clear,
    ):
        maintain.maintain_folder_corpus(
            str(tmp_path),
            embedding_model="old-model",
            search_mode="lancedb",
            mode="auto",
        )
    clear.assert_not_called()
    lance.assert_called_once()


def test_zvec_missing_collection_is_cold(tmp_path):
    _write_populated_meta(tmp_path, "old-model", sqlite=False)
    with (
        patch(
            "plugin.embeddings.venv.embeddings_zvec.maintain_folder_zvec",
            return_value={"mode": "zvec"},
        ),
        patch.object(maintain, "clear_folder_cache") as clear,
    ):
        maintain.maintain_folder_corpus(
            str(tmp_path),
            embedding_model="old-model",
            search_mode="zvec",
            mode="auto",
        )
    clear.assert_called_once_with(str(tmp_path))


def test_resolve_mode_zvec_uses_collection_not_corpus_db(tmp_path):
    _write_populated_meta(tmp_path, "m", sqlite=False, collection="zvec")
    assert maintain._resolve_mode(str(tmp_path), "m", "auto", build_vectors=True, search_mode="zvec") == "incremental"
    (tmp_path / "writeragent_embeddings" / "zvec" / "segment").unlink()
    (tmp_path / "writeragent_embeddings" / "zvec").rmdir()
    assert maintain._resolve_mode(str(tmp_path), "m", "auto", build_vectors=True, search_mode="zvec") == "cold"


def test_incremental_same_model_vector_gap_requests_backfill(tmp_path):
    """has_missing must embed. An empty ingest with no flag returns before the graph."""
    conn = MagicMock()
    conn.execute.return_value.fetchone.return_value = None
    with (
        patch.object(maintain, "connect_corpus_db", return_value=conn),
        patch("plugin.embeddings.venv.embeddings_sqlite._load_vec_extension"),
        patch.object(maintain, "corpus_chunk_count", return_value=2),
        patch.object(maintain, "_write_row_count_meta"),
        patch.object(maintain, "_ingest_rows", return_value={"indexed": 2, "upserted": 2}) as ingest,
    ):
        maintain._incremental_refresh(
            str(tmp_path),
            "all-MiniLM-L6-v2",
            [],
            maintain._HeartbeatThrottle(None),
            build_fts=True,
            build_vectors=True,
            search_mode="embeddings",
        )
    ingest.assert_called_once()
    assert ingest.call_args.args[2] == []
    assert ingest.call_args.kwargs["fill_vector_gaps"] is True
    assert ingest.call_args.kwargs["build_vectors"] is True


def test_incremental_skips_backfill_when_vectors_present(tmp_path):
    conn = MagicMock()
    has_table = MagicMock()
    has_table.fetchone.return_value = (1,)
    no_gap = MagicMock()
    no_gap.fetchone.return_value = None
    conn.execute.side_effect = [has_table, no_gap]
    with (
        patch.object(maintain, "connect_corpus_db", return_value=conn),
        patch("plugin.embeddings.venv.embeddings_sqlite._load_vec_extension"),
        patch.object(maintain, "corpus_chunk_count", return_value=2),
        patch.object(maintain, "_write_row_count_meta"),
        patch.object(maintain, "_ingest_rows") as ingest,
    ):
        maintain._incremental_refresh(
            str(tmp_path),
            "all-MiniLM-L6-v2",
            [],
            maintain._HeartbeatThrottle(None),
            build_fts=False,
            build_vectors=True,
            search_mode="embeddings",
        )
    ingest.assert_not_called()


def test_fill_vector_gaps_uses_sqlite_ingest(tmp_path):
    with (
        patch.object(maintain, "ingest_paragraphs", return_value={"indexed": 1}) as ingest,
        patch("plugin.embeddings.venv.embeddings_llama_index.llama_index_ingest") as llama,
    ):
        maintain._ingest_rows(
            str(tmp_path),
            "model",
            [],
            build_fts=False,
            build_vectors=True,
            search_mode="llama_index",
            fill_vector_gaps=True,
        )
    ingest.assert_called_once()
    assert ingest.call_args.kwargs["fill_vector_gaps"] is True
    llama.assert_not_called()
