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

from plugin.framework.constants import EMBEDDINGS_SCHEMA_VERSION as SCHEMA_VERSION
from plugin.embeddings.venv.embeddings_index import EMBEDDINGS_VENV_PIP_INSTALL, embed_texts
from plugin.embeddings.venv.embeddings_sqlite import (
    connect_corpus_db,
    corpus_chunk_count,
    delete_by_doc_para,
    delete_paragraph_keys,
    ensure_schema,
    upsert_chunk_with_vector,
    _dim_from_meta_path,
)

log = logging.getLogger(__name__)

CHUNK_SIZE = 512
CHUNK_OVERLAP = 64


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
    vectors: NotRequired[list[list[float]]]
    upserted: NotRequired[int]
    dim: NotRequired[int]


def _import_splitter() -> Any:
    import importlib

    try:
        mod = importlib.import_module("langchain_text_splitters")
    except ImportError as exc:
        raise ImportError(
            "langchain-text-splitters is not installed in the configured Python venv. "
            f"Install with: {EMBEDDINGS_VENV_PIP_INSTALL}"
        ) from exc
    return mod.RecursiveCharacterTextSplitter(
        chunk_size=CHUNK_SIZE,
        chunk_overlap=CHUNK_OVERLAP,
        separators=["\n\n", "\n", " ", ""],
    )


def _import_document() -> Any:
    import importlib

    mod = importlib.import_module("langchain_core.documents")
    return mod.Document


def _split_paragraph_text(text: str, base_meta: dict[str, Any]) -> list[dict[str, Any]]:
    """Split one paragraph into sub-chunks with char offsets relative to paragraph text."""
    splitter = _import_splitter()
    pieces = splitter.split_text(text)
    if not pieces:
        return []

    chunks: list[dict[str, Any]] = []
    search_from = 0
    for chunk_index, piece in enumerate(pieces):
        idx = text.find(piece, search_from)
        if idx < 0:
            idx = search_from
        char_start = idx
        char_end = idx + len(piece)
        search_from = max(0, char_end - CHUNK_OVERLAP)
        meta = dict(base_meta)
        meta.update(
            {
                "char_start": char_start,
                "char_end": char_end,
                "chunk_index": chunk_index,
                "text": piece,
            }
        )
        chunks.append(meta)
    return chunks


def rows_to_documents(state: IngestState) -> dict[str, Any]:
    Document = _import_document()
    documents: list[Any] = []
    for row in state.get("rows") or []:
        text = str(row.get("text") or "").strip()
        if not text:
            continue
        meta = {
            "doc_url": str(row.get("doc_url") or ""),
            "para_index": int(row.get("para_index", 0)),
            "content_hash": str(row.get("content_hash") or ""),
            "file_mtime": float(row.get("file_mtime") or 0.0),
            "embedding_model": str(state.get("model") or ""),
        }
        documents.append(Document(page_content=text, metadata=meta))
    return {"documents": documents}


def split_chunks(state: IngestState) -> dict[str, Any]:
    chunks: list[dict[str, Any]] = []
    for doc in state.get("documents") or []:
        text = str(getattr(doc, "page_content", "") or "").strip()
        if not text:
            continue
        base_meta = dict(getattr(doc, "metadata", {}) or {})
        if len(text) <= CHUNK_SIZE:
            base_meta.update(
                {
                    "char_start": 0,
                    "char_end": len(text),
                    "chunk_index": 0,
                    "text": text,
                }
            )
            chunks.append(base_meta)
        else:
            chunks.extend(_split_paragraph_text(text, base_meta))
    return {"chunks": chunks}


def delete_stale(state: IngestState) -> dict[str, Any]:
    """Remove deleted paragraphs and clear sub-chunks before re-indexing changed paragraphs."""
    build_fts = bool(state.get("build_fts"))
    build_vectors = bool(state.get("build_vectors"))
    dim = _dim_from_meta_path(str(state.get("meta_path") or ""))
    # Cold build: embedding dim is unknown until embed_chunks runs; upsert_corpus creates vec_chunks.
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

        seen: set[tuple[str, int]] = set()
        for chunk in state.get("chunks") or []:
            key = (str(chunk.get("doc_url") or ""), int(chunk.get("para_index", 0)))
            if key in seen:
                continue
            seen.add(key)
            delete_by_doc_para(
                conn,
                key[0],
                key[1],
                with_fts=build_fts,
                with_vec=with_vec,
            )
        conn.commit()
    finally:
        conn.close()
    return {}


def embed_chunks(state: IngestState) -> dict[str, Any]:
    if not state.get("build_vectors"):
        return {"vectors": [], "dim": 0}
    chunks = state.get("chunks") or []
    if not chunks:
        return {"vectors": [], "dim": 0}
    texts = [str(c.get("text") or "") for c in chunks]
    encoded = embed_texts(str(state.get("model") or ""), texts)
    return {"vectors": encoded.get("vectors") or [], "dim": int(encoded.get("dim") or 0)}


def upsert_corpus(state: IngestState) -> dict[str, Any]:
    build_fts = bool(state.get("build_fts"))
    build_vectors = bool(state.get("build_vectors"))
    chunks = state.get("chunks") or []
    vectors = state.get("vectors") or []
    model = str(state.get("model") or "")
    dim = int(state.get("dim") or 0)

    conn = connect_corpus_db(str(state["db_path"]))
    try:
        schema_dim = dim if dim > 0 else _dim_from_meta_path(str(state.get("meta_path") or ""))
        ensure_schema(conn, dim=schema_dim, with_fts=build_fts, with_vec=build_vectors)

        if not chunks or (build_vectors and not vectors):
            count = corpus_chunk_count(conn)
            _write_meta(state, chunk_count_override=count, dim=dim)
            return {"upserted": 0}

        upserted = 0
        for chunk, vec in zip(chunks, vectors if build_vectors else [[] for _ in chunks]):
            if build_vectors and not vec:
                continue
            upsert_chunk_with_vector(
                conn,
                chunk,
                vec if build_vectors else [],
                model=model,
                with_fts=build_fts,
                with_vec=build_vectors,
            )
            upserted += 1
        conn.commit()
        count = corpus_chunk_count(conn)
        _write_meta(state, chunk_count_override=count, dim=dim)
        return {"upserted": upserted}
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
    graph.add_node("rows_to_documents", rows_to_documents)
    graph.add_node("split_chunks", split_chunks)
    graph.add_node("delete_stale", delete_stale)
    graph.add_node("embed_chunks", embed_chunks)
    graph.add_node("upsert_corpus", upsert_corpus)
    graph.add_edge(lg.START, "rows_to_documents")
    graph.add_edge("rows_to_documents", "split_chunks")
    graph.add_edge("split_chunks", "delete_stale")
    graph.add_edge("delete_stale", "embed_chunks")
    graph.add_edge("embed_chunks", "upsert_corpus")
    graph.add_edge("upsert_corpus", lg.END)
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


__all__ = ["CHUNK_SIZE", "CHUNK_OVERLAP", "ingest_paragraphs"]
