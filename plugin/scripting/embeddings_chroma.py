# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
"""ChromaDB helpers for per-folder embeddings index (trusted venv module)."""
from __future__ import annotations

import hashlib
import logging
from typing import Any

log = logging.getLogger(__name__)

_CLIENT_CACHE: dict[str, Any] = {}
_COLLECTION_CACHE: dict[tuple[str, str], Any] = {}


def chunk_id_for(
    doc_url: str,
    para_index: int,
    char_start: int,
    char_end: int,
    content_hash: str,
) -> str:
    """Deterministic Chroma document id for a sub-chunk."""
    raw = f"{doc_url}|{para_index}|{char_start}|{char_end}|{content_hash}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def _import_chromadb() -> Any:
    import importlib

    try:
        return importlib.import_module("chromadb")
    except ImportError as exc:
        raise ImportError(
            "chromadb is not installed in the configured Python venv. "
            "Install with: pip install chromadb langgraph langchain-core langchain-text-splitters"
        ) from exc


def get_client(persist_dir: str) -> Any:
    """Return a cached PersistentClient for *persist_dir*."""
    key = str(persist_dir)
    cached = _CLIENT_CACHE.get(key)
    if cached is not None:
        return cached
    chromadb = _import_chromadb()
    client = chromadb.PersistentClient(path=key)
    _CLIENT_CACHE[key] = client
    return client


def get_collection(persist_dir: str, collection_name: str) -> Any:
    """Open or create the folder collection (cosine space)."""
    cache_key = (str(persist_dir), str(collection_name))
    cached = _COLLECTION_CACHE.get(cache_key)
    if cached is not None:
        return cached
    client = get_client(persist_dir)
    collection = client.get_or_create_collection(
        name=collection_name,
        metadata={"hnsw:space": "cosine"},
    )
    _COLLECTION_CACHE[cache_key] = collection
    return collection


def collection_count(collection: Any) -> int:
    return int(collection.count())


def delete_by_doc_url(collection: Any, doc_url: str) -> int:
    """Remove all vectors for *doc_url*."""
    doc_url = str(doc_url or "")
    if not doc_url:
        return 0
    try:
        existing = collection.get(where={"doc_url": doc_url}, include=[])
        ids = list(existing.get("ids") or [])
        if ids:
            collection.delete(ids=ids)
        return len(ids)
    except Exception:
        log.debug("delete_by_doc_url failed for %s", doc_url, exc_info=True)
        return 0


def delete_by_doc_para(collection: Any, doc_url: str, para_index: int) -> int:
    """Remove all sub-chunks for one paragraph."""
    doc_url = str(doc_url or "")
    try:
        existing = collection.get(
            where={"$and": [{"doc_url": doc_url}, {"para_index": int(para_index)}]},
            include=[],
        )
        ids = list(existing.get("ids") or [])
        if ids:
            collection.delete(ids=ids)
        return len(ids)
    except Exception:
        log.debug("delete_by_doc_para failed for %s para %s", doc_url, para_index, exc_info=True)
        return 0


def delete_paragraph_keys(collection: Any, keys: list[dict[str, Any]]) -> int:
    """Delete vectors for (doc_url, para_index) pairs from host diff."""
    deleted = 0
    for key in keys or []:
        doc_url = str(key.get("doc_url") or "")
        para_index = int(key.get("para_index", 0))
        deleted += delete_by_doc_para(collection, doc_url, para_index)
    return deleted


def build_chunk_metadata(
    *,
    doc_url: str,
    para_index: int,
    char_start: int,
    char_end: int,
    content_hash: str,
    file_mtime: float,
    embedding_model: str,
    chunk_index: int,
) -> dict[str, Any]:
    """Chroma metadata — scalar types only."""
    return {
        "doc_url": doc_url,
        "para_index": int(para_index),
        "char_start": int(char_start),
        "char_end": int(char_end),
        "content_hash": content_hash,
        "file_mtime": float(file_mtime),
        "embedding_model": embedding_model,
        "chunk_index": int(chunk_index),
    }


__all__ = [
    "build_chunk_metadata",
    "chunk_id_for",
    "collection_count",
    "delete_by_doc_para",
    "delete_by_doc_url",
    "delete_paragraph_keys",
    "get_client",
    "get_collection",
]
