# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
"""Trusted venv module: embeddings encode + sqlite-vec index/search facades.

Invoked from the LO host through fixed RPC stubs — not from LLM-submitted code.
See docs/embeddings.md and docs/enabling_numpy_in_libreoffice.md.
"""
from __future__ import annotations

import importlib
import json
import logging
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

EMBEDDINGS_VENV_PIP_INSTALL = (
    "pip install sentence-transformers numpy sqlite-vec langgraph "
    "langchain-core langchain-text-splitters envwrap odfpy pandas "
    "openpyxl xlrd python-docx"
)

_MODEL_CACHE: dict[str, Any] = {}


def _get_embedder(model_name: str) -> Any:
    cached = _MODEL_CACHE.get(model_name)
    if cached is not None:
        return cached
    try:
        st_mod = importlib.import_module("sentence_transformers")
    except ImportError as exc:
        cause = exc.__cause__
        detail = f"{exc}; caused by: {cause}" if cause else str(exc)
        raise ImportError(
            "sentence-transformers failed to import in the configured Python venv "
            f"({detail}). Install with: {EMBEDDINGS_VENV_PIP_INSTALL}"
        ) from exc
    embedder = st_mod.SentenceTransformer(model_name)
    _MODEL_CACHE[model_name] = embedder
    return embedder


def _l2_normalize_rows(matrix: Any) -> Any:
    import numpy as np

    norms = np.linalg.norm(matrix, axis=1, keepdims=True)
    norms = np.where(norms == 0.0, 1.0, norms)
    return matrix / norms


def embed_texts(model_name: str, texts: list[str], *, normalize: bool = True) -> dict[str, Any]:
    """Batch-encode *texts* with a lazily loaded SentenceTransformer."""
    import numpy as np

    model = (model_name or "").strip()
    if not model:
        raise ValueError("embedding model name is required")

    if not texts:
        return {"model": model, "dim": 0, "vectors": [], "indices": []}

    indices: list[int] = []
    valid_texts: list[str] = []
    for i, text in enumerate(texts):
        if text is None:
            continue
        stripped = str(text).strip()
        if not stripped:
            continue
        indices.append(i)
        valid_texts.append(stripped)

    if not valid_texts:
        return {"model": model, "dim": 0, "vectors": [], "indices": []}

    embedder = _get_embedder(model)
    batch = embedder.encode(valid_texts, convert_to_tensor=False, show_progress_bar=False)
    matrix = np.stack(batch).astype(np.float32)
    if normalize:
        matrix = _l2_normalize_rows(matrix)

    dim = int(matrix.shape[1])
    vectors = matrix.tolist()
    return {"model": model, "dim": dim, "vectors": vectors, "indices": indices}


def index_paragraphs(
    db_path: str,
    meta_path: str,
    model_name: str,
    rows: list[dict[str, Any]],
    *,
    build_fts: bool = False,
    build_vectors: bool = True,
) -> dict[str, Any]:
    """Batch-embed *rows* and persist into corpus.db via the LangGraph ingest pipeline."""
    from plugin.embeddings.venv.embeddings_ingest_graph import ingest_paragraphs

    return ingest_paragraphs(
        db_path,
        meta_path,
        model_name,
        rows,
        delete_keys=[],
        build_fts=build_fts,
        build_vectors=build_vectors,
    )


def delete_paragraphs(
    db_path: str,
    meta_path: str,
    keys: list[dict[str, Any]],
    *,
    model_name: str = "",
    build_fts: bool = False,
    build_vectors: bool = True,
) -> dict[str, Any]:
    """Remove paragraph rows from corpus.db."""
    from plugin.embeddings.venv.embeddings_sqlite import connect_corpus_db, corpus_chunk_count, delete_paragraph_keys, ensure_schema

    if not keys:
        return {"deleted": 0}

    conn = connect_corpus_db(str(db_path))
    try:
        ensure_schema(conn, with_fts=build_fts, with_vec=build_vectors)
        deleted = delete_paragraph_keys(conn, keys, with_fts=build_fts, with_vec=build_vectors)
        conn.commit()
        count = corpus_chunk_count(conn)
    finally:
        conn.close()

    meta_file = Path(str(meta_path))
    if meta_file.is_file():
        try:
            data = json.loads(meta_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            data = {}
    else:
        data = {}
    data["chunk_count"] = str(count)
    meta_file.parent.mkdir(parents=True, exist_ok=True)
    meta_file.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
    return {"deleted": deleted}


def knn_search(
    db_path: str,
    query_text: str,
    k: int,
    *,
    model_name: str,
    doc_url_filter: str | None = None,
) -> dict[str, Any]:
    """Semantic search via the LangGraph search pipeline."""
    from plugin.embeddings.venv.embeddings_search_graph import search_embeddings_graph

    return search_embeddings_graph(
        db_path,
        query_text,
        k,
        model_name=model_name,
        doc_url_filter=doc_url_filter,
    )


def hybrid_search(
    db_path: str,
    query_text: str,
    k: int,
    *,
    model_name: str,
    near_slop: int = 10,
    doc_url_filter: str | None = None,
) -> dict[str, Any]:
    """Hybrid FTS + vector search with reciprocal rank fusion."""
    from plugin.embeddings.venv.embeddings_hybrid_search import hybrid_corpus_search

    return hybrid_corpus_search(
        db_path,
        query_text,
        k,
        model_name=model_name,
        near_slop=near_slop,
        doc_url_filter=doc_url_filter,
    )


def maintain_folder_index(
    listing_root: str,
    embedding_model: str,
    mode: str = "auto",
    *,
    search_mode: str = "hybrid",
    heartbeat_fn: Any | None = None,
) -> dict[str, Any]:
    """Folder corpus maintenance (ODF extract + corpus.db) — trusted RPC entry point."""
    from typing import cast

    from plugin.embeddings.venv.embeddings_folder_maintain import MaintainMode, maintain_folder_corpus

    resolved_mode: MaintainMode = cast("MaintainMode", mode if mode in ("auto", "cold", "incremental") else "auto")
    return maintain_folder_corpus(
        str(listing_root),
        embedding_model=str(embedding_model),
        search_mode=str(search_mode or "embeddings"),
        mode=resolved_mode,
        heartbeat_fn=heartbeat_fn,
    )


def collection_stats(
    db_path: str,
    meta_path: str,
    *,
    model_name: str = "",
) -> dict[str, Any]:
    """Return chunk count and corpus metadata for host empty/stale checks."""
    from plugin.embeddings.venv.embeddings_sqlite import connect_corpus_db, corpus_chunk_count

    meta_file = Path(str(meta_path))
    meta: dict[str, str] = {}
    if meta_file.is_file():
        try:
            raw = json.loads(meta_file.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                meta = {str(k): str(v) for k, v in raw.items()}
        except (OSError, json.JSONDecodeError):
            log.debug("collection_stats could not read %s", meta_path, exc_info=True)

    count = int(meta.get("chunk_count", "0") or 0)
    db = Path(str(db_path))
    if db.is_file():
        conn = connect_corpus_db(db)
        try:
            count = corpus_chunk_count(conn)
        except Exception:
            log.debug("collection_stats corpus.db open failed", exc_info=True)
        finally:
            conn.close()

    return {
        "chunk_count": count,
        "schema_version": meta.get("schema_version", ""),
        "embedding_model": meta.get("embedding_model", ""),
        "storage_backend": meta.get("storage_backend", "sqlite_vec"),
        "dim": int(meta.get("dim", "0") or 0),
    }


__all__ = [
    "collection_stats",
    "delete_paragraphs",
    "embed_texts",
    "hybrid_search",
    "index_paragraphs",
    "knn_search",
    "maintain_folder_index",
]
