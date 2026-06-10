# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
"""Trusted venv module: embeddings encode + Chroma index/search facades.

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

_MODEL_CACHE: dict[str, Any] = {}


def _get_embedder(model_name: str) -> Any:
    cached = _MODEL_CACHE.get(model_name)
    if cached is not None:
        return cached
    try:
        st_mod = importlib.import_module("sentence_transformers")
    except ImportError as exc:
        raise ImportError(
            "sentence-transformers is not installed in the configured Python venv. "
            "Install it with: pip install sentence-transformers numpy chromadb langgraph langchain-core langchain-text-splitters"
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
    persist_dir: str,
    collection_name: str,
    meta_path: str,
    model_name: str,
    rows: list[dict[str, Any]],
) -> dict[str, Any]:
    """Batch-embed *rows* and persist into Chroma via the LangGraph ingest pipeline."""
    from plugin.scripting.embeddings_ingest_graph import ingest_paragraphs

    return ingest_paragraphs(
        persist_dir,
        collection_name,
        meta_path,
        model_name,
        rows,
        delete_keys=[],
    )


def delete_paragraphs(
    persist_dir: str,
    collection_name: str,
    meta_path: str,
    keys: list[dict[str, Any]],
    *,
    model_name: str = "",
) -> dict[str, Any]:
    """Remove paragraph vectors from Chroma."""
    from plugin.scripting.embeddings_chroma import collection_count, delete_paragraph_keys, get_collection

    if not keys:
        return {"deleted": 0}

    collection = get_collection(str(persist_dir), str(collection_name))
    deleted = delete_paragraph_keys(collection, keys)
    count = collection_count(collection)
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
    persist_dir: str,
    collection_name: str,
    query_text: str,
    k: int,
    *,
    model_name: str,
    doc_url_filter: str | None = None,
) -> dict[str, Any]:
    """Semantic search via the LangGraph search pipeline."""
    from plugin.scripting.embeddings_search_graph import search_embeddings_graph

    return search_embeddings_graph(
        persist_dir,
        collection_name,
        query_text,
        k,
        model_name=model_name,
        doc_url_filter=doc_url_filter,
    )


def collection_stats(
    persist_dir: str,
    collection_name: str,
    meta_path: str,
    *,
    model_name: str = "",
) -> dict[str, Any]:
    """Return chunk count and corpus metadata for host empty/stale checks."""
    from plugin.scripting.embeddings_chroma import collection_count, get_collection

    meta_file = Path(str(meta_path))
    meta: dict[str, str] = {}
    if meta_file.is_file():
        try:
            raw = json.loads(meta_file.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                meta = {str(k): str(v) for k, v in raw.items()}
        except (OSError, json.JSONDecodeError):
            log.debug("collection_stats could not read %s", meta_path, exc_info=True)

    try:
        collection = get_collection(str(persist_dir), str(collection_name))
        count = collection_count(collection)
    except Exception:
        log.debug("collection_stats Chroma open failed", exc_info=True)
        count = int(meta.get("chunk_count", "0") or 0)

    return {
        "chunk_count": count,
        "schema_version": meta.get("schema_version", ""),
        "embedding_model": meta.get("embedding_model", ""),
        "storage_backend": meta.get("storage_backend", "chroma"),
        "dim": int(meta.get("dim", "0") or 0),
    }


__all__ = [
    "collection_stats",
    "delete_paragraphs",
    "embed_texts",
    "index_paragraphs",
    "knn_search",
]
