# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
"""ChromaDB storage backend helper for per-folder embeddings & hybrid search."""
from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path
from typing import Any

from plugin.embeddings.venv.embeddings_index import embed_texts
from plugin.embeddings.venv.embeddings_sqlite import (
    connect_corpus_db,
    corpus_chunk_count,
    delete_paragraph_keys,
    ensure_schema,
    fts_corpus_search,
    model_slug,
    upsert_chunk_with_vector,
)
from plugin.embeddings.venv.embeddings_retrieval_pool import hybrid_retrieval_pool
from plugin.embeddings.venv.hybrid_rrf import merge_hybrid_hits
from plugin.embeddings.venv.embeddings_parent_hits import expand_candidates_to_parent_paragraphs
from plugin.embeddings.venv.embeddings_search_graph import _public_hit_from_candidate

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
    from plugin.embeddings.venv.embeddings_index import EMBEDDINGS_VENV_PIP_INSTALL

    try:
        return importlib.import_module("chromadb")
    except ImportError as exc:
        raise ImportError(
            f"chromadb is not installed in the configured Python venv. Install with: {EMBEDDINGS_VENV_PIP_INSTALL}"
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


def chroma_ingest(
    db_path: str,
    meta_path: str,
    model_name: str,
    rows: list[dict[str, Any]],
    *,
    delete_keys: list[dict[str, Any]] | None = None,
    build_fts: bool = False,
    build_vectors: bool = True,
) -> dict[str, Any]:
    """Ingest rows using Chroma for vectors and SQLite corpus.db for metadata + FTS."""
    model = (model_name or "").strip()
    if not model and build_vectors:
        raise ValueError("embedding model name is required")

    cache_dir = Path(db_path).parent
    chroma_dir = cache_dir / "chroma"
    collection_name = model_slug(model)
    collection = get_collection(str(chroma_dir), collection_name)

    # 1. Handle Deletions
    deleted = 0
    if delete_keys:
        # Chroma Delete
        chroma_ids = []
        for k in delete_keys:
            doc_url = str(k.get("doc_url") or "")
            para_index = int(k.get("para_index") or 0)
            # Find matching items in chroma first to delete by id
            where_clause: dict[str, Any] = {"doc_url": doc_url}
            if "para_index" in k:
                where_clause["para_index"] = para_index
            try:
                res = collection.get(where=where_clause, include=[])
                if res and res.get("ids"):
                    chroma_ids.extend(res["ids"])
            except Exception:
                pass
        if chroma_ids:
            collection.delete(ids=chroma_ids)
            deleted = len(chroma_ids)

        # SQLite Delete
        conn = connect_corpus_db(db_path)
        try:
            ensure_schema(conn, with_fts=build_fts, with_vec=False)
            delete_paragraph_keys(conn, delete_keys, with_fts=build_fts, with_vec=False)
            conn.commit()
        finally:
            conn.close()

    # 2. Handle Ingestion
    upserted = 0
    dim = 0
    if rows:
        if build_vectors:
            texts = [str(r.get("text") or "").strip() for r in rows]
            encoded = embed_texts(model, texts)
            vectors = encoded.get("vectors") or []
            dim = int(encoded.get("dim") or 0)
        else:
            vectors = [[] for _ in rows]

        chroma_ids = []
        chroma_embeddings = []
        chroma_metadatas = []
        chroma_documents = []

        conn = connect_corpus_db(db_path)
        try:
            ensure_schema(conn, with_fts=build_fts, with_vec=False)

            for chunk, vec in zip(rows, vectors):
                text = str(chunk.get("text") or "").strip()
                if not text:
                    continue
                doc_url = str(chunk.get("doc_url") or "")
                para_index = int(chunk.get("para_index") or 0)
                char_start = int(chunk.get("char_start") or 0)
                char_end = int(chunk.get("char_end") or len(text))
                content_hash = str(chunk.get("content_hash") or "")
                chunk_index = int(chunk.get("chunk_index") or 0)

                cid = chunk_id_for(doc_url, para_index, char_start, char_end, content_hash)

                if build_vectors and vec:
                    chroma_ids.append(cid)
                    chroma_embeddings.append(vec)
                    chroma_documents.append(text)
                    chroma_metadatas.append(
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

                # Write chunk & FTS to SQLite corpus.db (no vector storage here)
                upsert_chunk_with_vector(
                    conn,
                    chunk,
                    vector=[],
                    model=model,
                    with_fts=build_fts,
                    with_vec=False,
                )
                upserted += 1
            conn.commit()
        finally:
            conn.close()

        if chroma_ids:
            collection.upsert(
                ids=chroma_ids,
                embeddings=chroma_embeddings,
                metadatas=chroma_metadatas,
                documents=chroma_documents,
            )

    # 3. Write metadata file
    conn = connect_corpus_db(db_path)
    try:
        count = corpus_chunk_count(conn)
    finally:
        conn.close()

    meta_file = Path(meta_path)
    meta_file.parent.mkdir(parents=True, exist_ok=True)
    meta_file.write_text(
        json.dumps(
            {
                "schema_version": "3",
                "storage_backend": "chroma",
                "embedding_model": model,
                "dim": str(dim),
                "chunk_count": str(count),
                "updated_at": str(json.loads(meta_file.read_text()).get("updated_at") if meta_file.is_file() else 0.0),
            },
            indent=2,
            sort_keys=True,
        ),
        encoding="utf-8",
    )

    return {"indexed": upserted, "dim": dim, "storage_backend": "chroma", "upserted": upserted}


def chroma_knn_search(
    db_path: str,
    query_text: str,
    k: int,
    *,
    model_name: str,
    doc_url_filter: str | None = None,
    use_mmr: bool = True,
    rerank_model: str | None = None,
) -> dict[str, Any]:
    """ChromaDB vector KNN search."""
    model = (model_name or "").strip()
    if not model:
        raise ValueError("embedding model name is required")
    query = str(query_text or "").strip()
    if not query:
        return {"hits": []}

    encoded = embed_texts(model, [query])
    vectors = encoded.get("vectors") or []
    if not vectors:
        return {"hits": []}
    query_vec = vectors[0]

    cache_dir = Path(db_path).parent
    chroma_dir = cache_dir / "chroma"
    collection_name = model_slug(model)
    collection = get_collection(str(chroma_dir), collection_name)

    try:
        count = int(collection.count())
    except Exception:
        count = 0
    if count == 0:
        return {"hits": []}

    # Retrieve slightly more for MMR or reranking
    n_results = min(max(k * 4, 20), count)

    where_filter: dict[str, Any] = {}
    if doc_url_filter:
        where_filter["doc_url"] = str(doc_url_filter)

    query_args: dict[str, Any] = {
        "query_embeddings": [query_vec],
        "n_results": n_results,
        "include": ["metadatas", "distances", "documents"],
    }
    if where_filter:
        query_args["where"] = where_filter

    result = collection.query(**query_args)
    ids = (result.get("ids") or [[]])[0]
    metadatas = (result.get("metadatas") or [[]])[0]
    distances = (result.get("distances") or [[]])[0]
    documents = (result.get("documents") or [[]])[0]

    candidates: list[dict[str, Any]] = []
    for i, cid in enumerate(ids):
        meta = metadatas[i] if i < len(metadatas) else {}
        dist = float(distances[i]) if i < len(distances) else 1.0
        doc_text = documents[i] if i < len(documents) else ""
        score = max(0.0, 1.0 - dist)
        candidates.append(
            {
                "chunk_id": str(cid),
                "doc_url": str((meta or {}).get("doc_url") or ""),
                "para_index": int((meta or {}).get("para_index") or 0),
                "embedding_model": str((meta or {}).get("embedding_model") or ""),
                "snippet": str(doc_text or ""),
                "score": score,
                "distance": dist,
            }
        )

    # Expand candidates
    fused = expand_candidates_to_parent_paragraphs(str(db_path), candidates)

    from plugin.embeddings.venv.embeddings_cross_encoder_rerank import cross_encoder_rerank_candidates
    rerank_id = str(rerank_model or "").strip()
    if use_mmr and rerank_id and fused and k > 1:
        fused = cross_encoder_rerank_candidates(
            query,
            fused,
            model=rerank_id,
            top_n=k,
        )
    else:
        fused = fused[:k]

    hits = []
    for row in fused:
        hit = _public_hit_from_candidate(row)
        hits.append(hit)

    return {"hits": hits}


def chroma_hybrid_search(
    db_path: str,
    query_text: str,
    k: int,
    *,
    model_name: str,
    near_slop: int = 10,
    doc_url_filter: str | None = None,
    use_mmr: bool = True,
    rerank_model: str | None = None,
) -> dict[str, Any]:
    """Hybrid search: Attempt ChromaDB's native hybrid search (if supported/enabled),
    otherwise fallback to FTS5 (via SQLite corpus.db) + ChromaDB vector search fused with RRF.
    """
    model = (model_name or "").strip()
    if not model:
        raise ValueError("embedding model name is required")
    query = str(query_text or "").strip()
    if not query:
        return {"hits": []}

    final_k, fetch_k = hybrid_retrieval_pool(k)
    cache_dir = Path(db_path).parent
    chroma_dir = cache_dir / "chroma"
    collection_name = model_slug(model)

    # 1. Try Native ChromaDB Hybrid Search
    try:
        from chromadb import Search, Knn, Rrf  # type: ignore[import-not-found]
        from chromadb import K as FilterK  # type: ignore[import-not-found]
        collection = get_collection(str(chroma_dir), collection_name)
        
        dense_rank = Knn(query=query, return_rank=True, limit=fetch_k)
        sparse_rank = Knn(query=query, key="sparse_embedding", return_rank=True, limit=fetch_k)
        hybrid_rank = Rrf(ranks=[dense_rank, sparse_rank], k=60)
        
        search_query = Search().rank(hybrid_rank).limit(final_k)
        if doc_url_filter:
            search_query = search_query.where(FilterK("doc_url") == str(doc_url_filter))
            
        results = collection.search(search_query)
        
        # Parse SearchResults structure if successful
        candidates = []
        if results and hasattr(results, "ids") and results.ids:
            ids = results.ids
            metadatas = getattr(results, "metadatas", []) or []
            documents = getattr(results, "documents", []) or []
            scores = getattr(results, "scores", []) or getattr(results, "distances", []) or []
            
            for i, cid in enumerate(ids):
                meta = metadatas[i] if i < len(metadatas) else {}
                doc_text = documents[i] if i < len(documents) else ""
                score = float(scores[i]) if i < len(scores) else 1.0
                candidates.append(
                    {
                        "chunk_id": str(cid),
                        "doc_url": str((meta or {}).get("doc_url") or ""),
                        "para_index": int((meta or {}).get("para_index") or 0),
                        "embedding_model": str((meta or {}).get("embedding_model") or ""),
                        "snippet": str(doc_text or ""),
                        "score": score,
                    }
                )
            
            fused = expand_candidates_to_parent_paragraphs(str(db_path), candidates)
            rerank_id = str(rerank_model or "").strip()
            if use_mmr and rerank_id and fused and final_k > 1:
                from plugin.embeddings.venv.embeddings_cross_encoder_rerank import cross_encoder_rerank_candidates
                fused = cross_encoder_rerank_candidates(
                    query,
                    fused,
                    model=rerank_id,
                    top_n=final_k,
                )
            else:
                fused = fused[:final_k]
                
            hits = []
            for row in fused:
                hit = _public_hit_from_candidate(row)
                hits.append(hit)
            return {"hits": hits}
            
    except Exception as exc:
        log.debug("Native ChromaDB hybrid search failed or not supported, falling back to SQLite FTS + Chroma: %s", exc)

    # 2. Fallback to FTS5 (via SQLite corpus.db) + Vector search (via ChromaDB) fused with RRF
    # Fetch Vector Hits from Chroma
    encoded = embed_texts(model, [query])
    vectors = encoded.get("vectors") or []
    if not vectors:
        return {"hits": []}
    query_vec = vectors[0]

    collection = get_collection(str(chroma_dir), collection_name)
    try:
        count = int(collection.count())
    except Exception:
        count = 0

    vec_hits = []
    if count > 0:
        where_filter: dict[str, Any] = {}
        if doc_url_filter:
            where_filter["doc_url"] = str(doc_url_filter)

        query_args: dict[str, Any] = {
            "query_embeddings": [query_vec],
            "n_results": min(fetch_k, count),
            "include": ["metadatas", "distances", "documents"],
        }
        if where_filter:
            query_args["where"] = where_filter

        result = collection.query(**query_args)
        ids = (result.get("ids") or [[]])[0]
        metadatas = (result.get("metadatas") or [[]])[0]
        distances = (result.get("distances") or [[]])[0]
        documents = (result.get("documents") or [[]])[0]

        for i, cid in enumerate(ids):
            meta = metadatas[i] if i < len(metadatas) else {}
            dist = float(distances[i]) if i < len(distances) else 1.0
            doc_text = documents[i] if i < len(documents) else ""
            score = max(0.0, 1.0 - dist)
            vec_hits.append(
                {
                    "chunk_id": str(cid),
                    "doc_url": str((meta or {}).get("doc_url") or ""),
                    "para_index": int((meta or {}).get("para_index") or 0),
                    "embedding_model": str((meta or {}).get("embedding_model") or ""),
                    "snippet": str(doc_text or ""),
                    "score": score,
                    "distance": dist,
                }
            )

    # Fetch FTS Hits from SQLite FTS5 passages
    conn = connect_corpus_db(str(db_path))
    try:
        fts_hits = fts_corpus_search(conn, query, k=fetch_k, near_slop=near_slop)
    finally:
        conn.close()

    if doc_url_filter:
        allowed = str(doc_url_filter)
        fts_hits = [h for h in fts_hits if str(h.get("doc_url") or "") == allowed]

    # Fuse results with RRF
    fused = merge_hybrid_hits(fts_hits, vec_hits, k=fetch_k)
    fused = expand_candidates_to_parent_paragraphs(str(db_path), fused)

    # Rerank
    rerank_id = str(rerank_model or "").strip()
    if use_mmr and rerank_id and fused and final_k > 1:
        from plugin.embeddings.venv.embeddings_cross_encoder_rerank import cross_encoder_rerank_candidates
        fused = cross_encoder_rerank_candidates(
            query,
            fused,
            model=rerank_id,
            top_n=final_k,
        )
    else:
        fused = fused[:final_k]

    hits = []
    for row in fused:
        hit = _public_hit_from_candidate(row)
        if row.get("matched_by"):
            hit["matched_by"] = list(row["matched_by"])
        hits.append(hit)

    return {"hits": hits}
