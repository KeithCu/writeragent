# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
"""LangGraph ingestion pipeline: split → embed → sqlite-vec upsert (trusted venv)."""
from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any, NotRequired, TypedDict

from plugin.embeddings.embeddings_split import CHUNK_OVERLAP, CHUNK_SIZE
from plugin.framework.constants import EMBEDDINGS_INGEST_BATCH_SIZE, EMBEDDINGS_SCHEMA_VERSION as SCHEMA_VERSION
from plugin.embeddings.venv.embeddings_index import embed_texts
from plugin.embeddings.venv.embeddings_sqlite import (
    connect_corpus_db,
    corpus_chunk_count,
    delete_by_chunk_locator,
    delete_paragraph_keys,
    ensure_schema,
    upsert_chunk_with_vector,
    _dim_from_meta_path,
)

log = logging.getLogger(__name__)


class IngestState(TypedDict):
    db_path: str
    meta_path: str
    model: str
    build_fts: NotRequired[bool]
    build_vectors: NotRequired[bool]
    rows: NotRequired[list[dict[str, Any]]]
    delete_keys: NotRequired[list[dict[str, Any]]]
    documents: NotRequired[list[Any]]
    chunks: NotRequired[list[dict[str, Any]]]
    upserted: NotRequired[int]
    dim: NotRequired[int]


def rows_to_chunks(state: IngestState) -> dict[str, Any]:
    """Map pre-split index rows to chunk dicts (extract already applied 512/64 split)."""
    chunks: list[dict[str, Any]] = []
    for row in state.get("rows") or []:
        text = str(row.get("text") or "").strip()
        if not text:
            continue
        chunks.append(
            {
                "doc_url": str(row.get("doc_url") or ""),
                "para_index": int(row.get("para_index", 0)),
                "char_start": int(row.get("char_start") or 0),
                "char_end": int(row.get("char_end") or len(text)),
                "content_hash": str(row.get("content_hash") or ""),
                "file_mtime": float(row.get("file_mtime") or 0.0),
                "text": text,
            }
        )
    return {"chunks": chunks}


def delete_stale(state: IngestState) -> dict[str, Any]:
    """Remove deleted chunk locators before re-indexing changed rows."""
    build_fts = bool(state.get("build_fts"))
    build_vectors = bool(state.get("build_vectors"))
    dim = _dim_from_meta_path(str(state.get("meta_path") or ""))
    # Cold build: embedding dim is unknown until embed runs; upsert creates vec_chunks.
    with_vec = build_vectors and dim is not None
    conn = connect_corpus_db(str(state["db_path"]))
    try:
        ensure_schema(conn, dim=dim, with_fts=build_fts, with_vec=with_vec)
        delete_paragraph_keys(
            conn,
            list(state.get("delete_keys") or []),
            with_fts=build_fts,
            with_vec=with_vec,
        )

        for chunk in state.get("chunks") or []:
            delete_by_chunk_locator(
                conn,
                str(chunk.get("doc_url") or ""),
                int(chunk.get("para_index") or 0),
                int(chunk.get("char_start") or 0),
                int(chunk.get("char_end") or 0),
                with_fts=build_fts,
                with_vec=with_vec,
            )
        conn.commit()
    finally:
        conn.close()
    return {}


def embed_and_upsert_batches(state: IngestState) -> dict[str, Any]:
    """Embed and upsert sub-chunks in fixed-size windows to bound RAM and CPU spikes."""
    build_fts = bool(state.get("build_fts"))
    build_vectors = bool(state.get("build_vectors"))
    chunks = state.get("chunks") or []
    model = str(state.get("model") or "")
    batch_size = max(1, EMBEDDINGS_INGEST_BATCH_SIZE)

    conn = connect_corpus_db(str(state["db_path"]))
    try:
        schema_dim = _dim_from_meta_path(str(state.get("meta_path") or ""))
        with_vec = build_vectors and schema_dim is not None
        ensure_schema(conn, dim=schema_dim, with_fts=build_fts, with_vec=with_vec)

        if not chunks:
            count = corpus_chunk_count(conn)
            _write_meta(state, chunk_count_override=count, dim=0)
            return {"upserted": 0, "dim": 0}

        upserted = 0
        dim = 0
        total_batches = (len(chunks) + batch_size - 1) // batch_size

        for batch_index, start in enumerate(range(0, len(chunks), batch_size)):
            window = chunks[start : start + batch_size]
            vectors: list[list[float]] = [[] for _ in window]

            if build_vectors:
                texts = [str(c.get("text") or "") for c in window]
                encoded = embed_texts(model, texts, encode_batch_size=batch_size)
                dim = int(encoded.get("dim") or dim)
                vectors = encoded.get("vectors") or []
                if schema_dim is None and dim > 0:
                    ensure_schema(conn, dim=dim, with_fts=build_fts, with_vec=True)
                    schema_dim = dim
                    with_vec = True

            for chunk, vec in zip(window, vectors if build_vectors else [[] for _ in window]):
                if build_vectors and not vec:
                    continue
                upsert_chunk_with_vector(
                    conn,
                    chunk,
                    vec if build_vectors else [],
                    model=model,
                    with_fts=build_fts,
                    with_vec=with_vec if build_vectors else False,
                )
                upserted += 1

            conn.commit()
            count = corpus_chunk_count(conn)
            _write_meta(state, chunk_count_override=count, dim=dim)
            log.debug(
                "ingest batch %s/%s upserted=%s row_count=%s",
                batch_index + 1,
                total_batches,
                len(window),
                count,
            )

        return {"upserted": upserted, "dim": dim}
    finally:
        conn.close()


def _write_meta(state: IngestState, *, chunk_count_override: int, dim: int) -> None:
    meta_path = Path(str(state.get("meta_path") or ""))
    if not meta_path.parent:
        return
    meta_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": SCHEMA_VERSION,
        "storage_backend": "sqlite_vec",
        "embedding_model": str(state.get("model") or ""),
        "dim": str(dim),
        "chunk_count": str(chunk_count_override),
        "updated_at": str(time.time()),
    }
    meta_path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")


def _build_ingest_graph() -> Any:
    import importlib
    from typing import cast

    lg = importlib.import_module("langgraph.graph")
    graph = lg.StateGraph(cast("Any", IngestState))
    graph.add_node("rows_to_chunks", rows_to_chunks)
    graph.add_node("delete_stale", delete_stale)
    graph.add_node("embed_and_upsert_batches", embed_and_upsert_batches)
    graph.add_edge(lg.START, "rows_to_chunks")
    graph.add_edge("rows_to_chunks", "delete_stale")
    graph.add_edge("delete_stale", "embed_and_upsert_batches")
    graph.add_edge("embed_and_upsert_batches", lg.END)
    return graph.compile()


_INGEST_GRAPH: Any | None = None


def _get_ingest_graph() -> Any:
    global _INGEST_GRAPH
    if _INGEST_GRAPH is None:
        _INGEST_GRAPH = _build_ingest_graph()
    return _INGEST_GRAPH


def ingest_paragraphs(
    db_path: str,
    meta_path: str,
    model_name: str,
    rows: list[dict[str, Any]],
    *,
    delete_keys: list[dict[str, Any]] | None = None,
    build_fts: bool = False,
    build_vectors: bool = True,
) -> dict[str, Any]:
    """Run the LangGraph ingest pipeline for changed paragraph rows."""
    model = (model_name or "").strip()
    if not model and build_vectors:
        raise ValueError("embedding model name is required")
    if not rows and not delete_keys:
        return {"indexed": 0, "dim": 0, "storage_backend": "sqlite_vec"}

    initial: IngestState = {
        "db_path": str(db_path),
        "meta_path": str(meta_path),
        "model": model,
        "build_fts": build_fts,
        "build_vectors": build_vectors,
        "rows": list(rows or []),
        "delete_keys": list(delete_keys or []),
    }
    final = _get_ingest_graph().invoke(initial)
    upserted = int(final.get("upserted") or 0)
    dim = int(final.get("dim") or 0)
    return {"indexed": upserted, "dim": dim, "storage_backend": "sqlite_vec"}


__all__ = ["CHUNK_SIZE", "CHUNK_OVERLAP", "embed_and_upsert_batches", "ingest_paragraphs", "rows_to_chunks"]
