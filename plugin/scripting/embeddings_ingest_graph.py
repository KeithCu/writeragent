# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
"""LangGraph ingestion pipeline: split → embed → Chroma upsert (trusted venv)."""
from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any, TypedDict

from plugin.doc.embeddings_cache import SCHEMA_VERSION
from plugin.scripting.embeddings_chroma import (
    build_chunk_metadata,
    chunk_id_for,
    delete_paragraph_keys,
    get_collection,
)
from plugin.scripting.embeddings_index import embed_texts

log = logging.getLogger(__name__)

CHUNK_SIZE = 512
CHUNK_OVERLAP = 64


class IngestState(TypedDict, total=False):
    persist_dir: str
    collection_name: str
    meta_path: str
    model: str
    rows: list[dict[str, Any]]
    delete_keys: list[dict[str, Any]]
    documents: list[Any]
    chunks: list[dict[str, Any]]
    vectors: list[list[float]]
    upserted: int
    dim: int


def _import_splitter() -> Any:
    import importlib

    try:
        mod = importlib.import_module("langchain_text_splitters")
    except ImportError as exc:
        raise ImportError(
            "langchain-text-splitters is not installed in the configured Python venv. "
            "Install with: pip install langchain-text-splitters langgraph langchain-core chromadb"
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
    collection = get_collection(str(state["persist_dir"]), str(state["collection_name"]))
    deleted = delete_paragraph_keys(collection, list(state.get("delete_keys") or []))

    seen: set[tuple[str, int]] = set()
    for chunk in state.get("chunks") or []:
        key = (str(chunk.get("doc_url") or ""), int(chunk.get("para_index", 0)))
        if key in seen:
            continue
        seen.add(key)
        from plugin.scripting.embeddings_chroma import delete_by_doc_para

        deleted += delete_by_doc_para(collection, key[0], key[1])
    return {}


def embed_chunks(state: IngestState) -> dict[str, Any]:
    chunks = state.get("chunks") or []
    if not chunks:
        return {"vectors": [], "dim": 0}
    texts = [str(c.get("text") or "") for c in chunks]
    encoded = embed_texts(str(state.get("model") or ""), texts)
    return {"vectors": encoded.get("vectors") or [], "dim": int(encoded.get("dim") or 0)}


def upsert_chroma(state: IngestState) -> dict[str, Any]:
    from plugin.scripting.embeddings_chroma import collection_count

    chunks = state.get("chunks") or []
    vectors = state.get("vectors") or []
    collection = get_collection(str(state["persist_dir"]), str(state["collection_name"]))

    if not chunks or not vectors:
        count = collection_count(collection)
        dim = int(state.get("dim") or 0)
        _write_meta(state, chunk_count_override=count, dim=dim)
        return {"upserted": 0}

    model = str(state.get("model") or "")
    ids: list[str] = []
    metadatas: list[dict[str, Any]] = []
    documents: list[str] = []
    embeddings: list[list[float]] = []

    for chunk, vec in zip(chunks, vectors):
        text = str(chunk.get("text") or "").strip()
        if not text:
            continue
        doc_url = str(chunk.get("doc_url") or "")
        para_index = int(chunk.get("para_index", 0))
        char_start = int(chunk.get("char_start") or 0)
        char_end = int(chunk.get("char_end") or len(text))
        content_hash = str(chunk.get("content_hash") or "")
        chunk_index = int(chunk.get("chunk_index") or 0)
        cid = chunk_id_for(doc_url, para_index, char_start, char_end, content_hash)
        ids.append(cid)
        documents.append(text)
        embeddings.append(vec)
        metadatas.append(
            build_chunk_metadata(
                doc_url=doc_url,
                para_index=para_index,
                char_start=char_start,
                char_end=char_end,
                content_hash=content_hash,
                file_mtime=float(chunk.get("file_mtime") or 0.0),
                embedding_model=model,
                chunk_index=chunk_index,
            )
        )

    if ids:
        collection.upsert(ids=ids, embeddings=embeddings, metadatas=metadatas, documents=documents)

    count = collection_count(collection)
    dim = int(state.get("dim") or 0)
    _write_meta(state, chunk_count_override=count, dim=dim)
    return {"upserted": len(ids)}


def _write_meta(state: IngestState, *, chunk_count_override: int, dim: int) -> None:
    meta_path = Path(str(state.get("meta_path") or ""))
    if not meta_path.parent:
        return
    meta_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": SCHEMA_VERSION,
        "storage_backend": "chroma",
        "embedding_model": str(state.get("model") or ""),
        "dim": str(dim),
        "chunk_count": str(chunk_count_override),
        "updated_at": str(time.time()),
    }
    meta_path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")


def _build_ingest_graph() -> Any:
    import importlib

    lg = importlib.import_module("langgraph.graph")
    graph = lg.StateGraph(IngestState)
    graph.add_node("rows_to_documents", rows_to_documents)
    graph.add_node("split_chunks", split_chunks)
    graph.add_node("delete_stale", delete_stale)
    graph.add_node("embed_chunks", embed_chunks)
    graph.add_node("upsert_chroma", upsert_chroma)
    graph.add_edge(lg.START, "rows_to_documents")
    graph.add_edge("rows_to_documents", "split_chunks")
    graph.add_edge("split_chunks", "delete_stale")
    graph.add_edge("delete_stale", "embed_chunks")
    graph.add_edge("embed_chunks", "upsert_chroma")
    graph.add_edge("upsert_chroma", lg.END)
    return graph.compile()


_INGEST_GRAPH: Any | None = None


def _get_ingest_graph() -> Any:
    global _INGEST_GRAPH
    if _INGEST_GRAPH is None:
        _INGEST_GRAPH = _build_ingest_graph()
    return _INGEST_GRAPH


def ingest_paragraphs(
    persist_dir: str,
    collection_name: str,
    meta_path: str,
    model_name: str,
    rows: list[dict[str, Any]],
    *,
    delete_keys: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Run the LangGraph ingest pipeline for changed paragraph rows."""
    model = (model_name or "").strip()
    if not model:
        raise ValueError("embedding model name is required")
    if not rows and not delete_keys:
        return {"indexed": 0, "dim": 0, "storage_backend": "chroma"}

    initial: IngestState = {
        "persist_dir": str(persist_dir),
        "collection_name": str(collection_name),
        "meta_path": str(meta_path),
        "model": model,
        "rows": list(rows or []),
        "delete_keys": list(delete_keys or []),
    }
    final = _get_ingest_graph().invoke(initial)
    upserted = int(final.get("upserted") or 0)
    dim = int(final.get("dim") or 0)
    return {"indexed": upserted, "dim": dim, "storage_backend": "chroma"}


__all__ = ["ingest_paragraphs"]
