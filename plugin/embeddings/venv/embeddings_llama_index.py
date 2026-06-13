# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Trusted venv LlamaIndex search/indexing backend.

Bridges LlamaIndex with the existing SQLite chunks, vec_chunks, and passages FTS5 tables,
providing reciprocal rank fusion and MMR reranking.
"""
from __future__ import annotations

import logging
import sqlite3
from typing import Any, List, Optional, Sequence

log = logging.getLogger(__name__)

try:
    from llama_index.core import QueryBundle, VectorStoreIndex  # type: ignore
    from llama_index.core.base.base_retriever import BaseRetriever  # type: ignore
    from llama_index.core.embeddings import BaseEmbedding  # type: ignore
    from llama_index.core.postprocessor.types import BaseNodePostprocessor  # type: ignore
    from llama_index.core.retrievers import QueryFusionRetriever  # type: ignore
    from llama_index.core.retrievers.fusion_retriever import FUSION_MODES  # type: ignore
    from llama_index.core.schema import NodeWithScore, TextNode  # type: ignore
    from llama_index.core.vector_stores.types import (  # type: ignore
        BasePydanticVectorStore,
        VectorStoreQuery,
        VectorStoreQueryResult,
    )
    from pydantic import PrivateAttr
    HAS_LLAMA_INDEX = True
except ImportError:
    HAS_LLAMA_INDEX = False
    # Fallback dummy definitions to prevent import-time failures when not installed
    class BaseEmbedding: pass  # type: ignore[no-redef]
    class BasePydanticVectorStore: pass  # type: ignore[no-redef]
    class BaseRetriever: pass  # type: ignore[no-redef]
    class QueryFusionRetriever: pass  # type: ignore[no-redef]
    class BaseNodePostprocessor: pass  # type: ignore[no-redef]
    
    class TextNode:  # type: ignore[no-redef]
        embedding: Any = None
        metadata: Any = None
        text: str = ""
        node_id: str = ""
        hash: str = ""
        def __init__(self, text: str = "", id_: str = "", metadata: Any = None, **kwargs: Any) -> None:
            self.text = text
            self.node_id = id_
            self.metadata = metadata or {}

    class NodeWithScore:  # type: ignore[no-redef]
        node: Any = None
        score: float = 0.0
        def __init__(self, node: Any = None, score: float = 0.0, **kwargs: Any) -> None:
            self.node = node
            self.score = score

    class VectorStoreQueryResult:  # type: ignore[no-redef]
        nodes: Any = None
        similarities: Any = None
        ids: Any = None
        def __init__(self, nodes: Any = None, similarities: Any = None, ids: Any = None, **kwargs: Any) -> None:
            self.nodes = nodes
            self.similarities = similarities
            self.ids = ids
        
    class QueryBundle:  # type: ignore[no-redef]
        query_str: str = ""
        def __init__(self, query_str: str = "", **kwargs: Any) -> None:
            self.query_str = query_str

    class VectorStoreIndex:  # type: ignore[no-redef]
        @classmethod
        def from_vector_store(cls, *args: Any, **kwargs: Any) -> Any:
            pass
        
    class VectorStoreQuery:  # type: ignore[no-redef]
        query_embedding: Any = None
        similarity_top_k: int = 1
        doc_ids: Any = None
        node_ids: Any = None
        query_str: Any = None
        filters: Any = None
        
    class FUSION_MODES:  # type: ignore[no-redef]
        RECIPROCAL_RANK: Any = "reciprocal_rerank"

    # Pydantic dummy
    def PrivateAttr(*args: Any, **kwargs: Any) -> Any: pass  # type: ignore[no-redef]


if HAS_LLAMA_INDEX:
    class WriterAgentEmbedding(BaseEmbedding):  # type: ignore[valid-type, misc]
        _model_name: str = PrivateAttr()

        def __init__(self, model_name: str, **kwargs: Any) -> None:
            super().__init__(**kwargs)
            self._model_name = model_name

        def _get_query_embedding(self, query: str) -> list[float]:
            from plugin.embeddings.venv.embeddings_index import embed_texts
            return embed_texts(self._model_name, [query])["vectors"][0]

        def _get_text_embedding(self, text: str) -> list[float]:
            from plugin.embeddings.venv.embeddings_index import embed_texts
            return embed_texts(self._model_name, [text])["vectors"][0]

        def _get_text_embeddings(self, texts: list[str]) -> list[list[float]]:
            from plugin.embeddings.venv.embeddings_index import embed_texts
            return embed_texts(self._model_name, texts)["vectors"]

        async def _aget_query_embedding(self, query: str) -> list[float]:
            return self._get_query_embedding(query)

        async def _aget_text_embedding(self, text: str) -> list[float]:
            return self._get_text_embedding(text)

        async def _aget_text_embeddings(self, texts: list[str]) -> list[list[float]]:
            return self._get_text_embeddings(texts)


    class WriterAgentVectorStore(BasePydanticVectorStore):  # type: ignore[valid-type, misc]
        stores_text: bool = True

        _db_path: str = PrivateAttr()
        _embedding_model: str = PrivateAttr()
        _build_fts: bool = PrivateAttr()
        _build_vectors: bool = PrivateAttr()

        def __init__(
            self,
            db_path: str,
            embedding_model: str,
            build_fts: bool = False,
            build_vectors: bool = True,
            **kwargs: Any,
        ) -> None:
            super().__init__(**kwargs)
            self._db_path = db_path
            self._embedding_model = embedding_model
            self._build_fts = build_fts
            self._build_vectors = build_vectors

        @property
        def client(self) -> Any:
            return None

        def add(self, nodes: Sequence[TextNode], **kwargs: Any) -> list[str]:
            from plugin.embeddings.venv.embeddings_sqlite import (
                connect_corpus_db,
                ensure_schema,
                upsert_chunk_with_vector,
            )

            conn = connect_corpus_db(self._db_path)
            try:
                dim = len(nodes[0].embedding) if nodes and nodes[0].embedding else None
                ensure_schema(conn, dim=dim, with_fts=self._build_fts, with_vec=self._build_vectors)
                for node in nodes:
                    meta = node.metadata or {}
                    chunk = {
                        "doc_url": meta.get("doc_url", ""),
                        "para_index": meta.get("para_index", 0),
                        "char_start": meta.get("char_start", 0),
                        "char_end": meta.get("char_end", len(node.text)),
                        "content_hash": meta.get("content_hash", ""),
                        "file_mtime": meta.get("file_mtime", 0.0),
                        "text": node.text,
                    }
                    upsert_chunk_with_vector(
                        conn,
                        chunk,
                        node.embedding or [],
                        model=self._embedding_model,
                        with_fts=self._build_fts,
                        with_vec=self._build_vectors and bool(node.embedding),
                    )
                conn.commit()
            finally:
                conn.close()
            return [node.node_id for node in nodes]

        def delete(self, ref_doc_id: str, **delete_kwargs: Any) -> None:
            from plugin.embeddings.venv.embeddings_sqlite import connect_corpus_db, _delete_chunk_ids
            conn = connect_corpus_db(self._db_path)
            try:
                rows = conn.execute("SELECT chunk_id FROM chunks WHERE doc_url = ?", (ref_doc_id,)).fetchall()
                chunk_ids = [int(row["chunk_id"]) for row in rows]
                _delete_chunk_ids(conn, chunk_ids, with_fts=self._build_fts, with_vec=self._build_vectors)
                conn.commit()
            finally:
                conn.close()

        def query(self, query: VectorStoreQuery, **kwargs: Any) -> VectorStoreQueryResult:
            from plugin.embeddings.venv.embeddings_sqlite import connect_corpus_db, vec0_search
            conn = connect_corpus_db(self._db_path)
            try:
                doc_filter = None
                if query.doc_ids:
                    doc_filter = query.doc_ids[0]
                elif query.filters:
                    for f in query.filters.filters:
                        if getattr(f, "key", None) == "doc_url":
                            doc_filter = f.value
                            break

                candidates = vec0_search(
                    conn,
                    query.query_embedding or [],
                    k=query.similarity_top_k,
                    model=self._embedding_model,
                    doc_url_filter=doc_filter,
                )
                nodes, similarities, ids = [], [], []
                for cand in candidates:
                    node = TextNode(
                        text=cand["snippet"],
                        id_=str(cand["chunk_id"]),
                        metadata={"doc_url": cand["doc_url"], "para_index": cand["para_index"]},
                    )
                    nodes.append(node)
                    similarities.append(cand["score"])
                    ids.append(str(cand["chunk_id"]))
                return VectorStoreQueryResult(nodes=nodes, similarities=similarities, ids=ids)
            finally:
                conn.close()


    class WriterAgentFTSRetriever(BaseRetriever):  # type: ignore[valid-type, misc]
        _db_path: str = PrivateAttr()
        _near_slop: int = PrivateAttr()

        def __init__(self, db_path: str, near_slop: int = 10, **kwargs: Any) -> None:
            super().__init__(**kwargs)
            self._db_path = db_path
            self._near_slop = near_slop

        def _retrieve(self, query_bundle: QueryBundle) -> list[NodeWithScore]:
            from plugin.embeddings.venv.embeddings_sqlite import connect_corpus_db, fts_corpus_search
            conn = connect_corpus_db(self._db_path)
            try:
                hits = fts_corpus_search(conn, query_bundle.query_str, k=50, near_slop=self._near_slop)
                nodes = []
                for hit in hits:
                    node = TextNode(
                        text=hit["snippet"],
                        id_=str(hit["chunk_id"]),
                        metadata={
                            "doc_url": hit["doc_url"],
                            "para_index": hit["para_index"],
                            "raw_score": hit["score"],
                        },
                    )
                    # Invert negative FTS BM25 score so that higher scores correspond to higher relevance
                    nodes.append(NodeWithScore(node=node, score=-float(hit["score"] or 0.0)))
                return nodes
            finally:
                conn.close()


    class WriterAgentQueryFusionRetriever(QueryFusionRetriever):  # type: ignore[valid-type, misc]
        _rrf_k: float = PrivateAttr()

        def __init__(self, *args: Any, rrf_k: float = 60.0, **kwargs: Any) -> None:
            super().__init__(*args, **kwargs)
            self._rrf_k = rrf_k

        def _reciprocal_rerank_fusion(self, results: dict[tuple[str, int], list[NodeWithScore]]) -> list[NodeWithScore]:
            fused_scores = {}
            hash_to_node = {}
            for nodes_with_scores in results.values():
                for rank, node_with_score in enumerate(
                    sorted(nodes_with_scores, key=lambda x: x.score or 0.0, reverse=True)
                ):
                    hash_val = node_with_score.node.hash
                    hash_to_node[hash_val] = node_with_score
                    if hash_val not in fused_scores:
                        fused_scores[hash_val] = 0.0
                    fused_scores[hash_val] += 1.0 / (rank + self._rrf_k)

            reranked_results = dict(sorted(fused_scores.items(), key=lambda x: x[1], reverse=True))
            reranked_nodes = []
            for hash_val, score in reranked_results.items():
                reranked_nodes.append(hash_to_node[hash_val])
                reranked_nodes[-1].score = score
            return reranked_nodes


    class WriterAgentMMRPostprocessor(BaseNodePostprocessor):  # type: ignore[valid-type, misc]
        _query_vec: list[float] = PrivateAttr()
        _db_path: str = PrivateAttr()
        _lambda_mult: float = PrivateAttr()

        def __init__(self, query_vec: list[float], db_path: str, lambda_mult: float = 0.7, **kwargs: Any) -> None:
            super().__init__(**kwargs)
            self._query_vec = query_vec
            self._db_path = db_path
            self._lambda_mult = lambda_mult

        def _postprocess_nodes(self, nodes: list[NodeWithScore], query_bundle: Optional[QueryBundle] = None) -> list[NodeWithScore]:
            if not nodes:
                return []

            from plugin.embeddings.venv.embeddings_sqlite import connect_corpus_db
            conn = connect_corpus_db(self._db_path)
            embeddings = {}
            try:
                for n in nodes:
                    try:
                        cid = int(n.node.node_id)
                        row = conn.execute("SELECT embedding FROM vec_chunks WHERE chunk_id = ?", (cid,)).fetchone()
                        if row:
                            import numpy as np
                            emb = np.frombuffer(row["embedding"], dtype=np.float32).tolist()
                            embeddings[n.node.node_id] = emb
                    except (ValueError, TypeError):
                        pass
            finally:
                conn.close()

            from plugin.embeddings.venv.embeddings_search_graph import _max_marginal_relevance
            candidates = []
            for n in nodes:
                candidates.append({
                    "node": n,
                    "chunk_id": int(n.node.node_id) if n.node.node_id.isdigit() else None,
                    "embedding": embeddings.get(n.node.node_id),
                })

            with_embeddings = [c for c in candidates if c["embedding"] is not None]
            without = [c for c in candidates if c["embedding"] is None]

            k = len(nodes)
            reranked = []
            if with_embeddings:
                import numpy as np
                fused_mmr = _max_marginal_relevance(
                    np.asarray(self._query_vec, dtype=np.float32),
                    [c["embedding"] for c in with_embeddings],
                    with_embeddings,
                    k,
                    lambda_mult=self._lambda_mult,
                )
                reranked.extend([c["node"] for c in fused_mmr])

            for c in without:
                if c["node"] not in reranked:
                    reranked.append(c["node"])

            return reranked


def llama_index_ingest(
    db_path: str,
    meta_path: str,
    model_name: str,
    rows: list[dict[str, Any]],
    *,
    delete_keys: list[dict[str, Any]] | None = None,
    build_fts: bool = False,
    build_vectors: bool = True,
) -> dict[str, Any]:
    """Index paragraphs/chunks using LlamaIndex VectorStoreIndex."""
    if not HAS_LLAMA_INDEX:
        raise ImportError("llama-index-core is not installed in the configured Python venv.")

    import json
    import time
    from pathlib import Path

    from plugin.embeddings.venv.embeddings_sqlite import (
        _dim_from_meta_path,
        connect_corpus_db,
        corpus_chunk_count,
        delete_by_chunk_locator,
        delete_paragraph_keys,
        ensure_schema,
    )

    conn = connect_corpus_db(db_path)
    try:
        dim = _dim_from_meta_path(meta_path)
        ensure_schema(conn, dim=dim, with_fts=build_fts, with_vec=build_vectors and dim is not None)

        if delete_keys:
            delete_paragraph_keys(conn, delete_keys, with_fts=build_fts, with_vec=build_vectors)

        for row in rows:
            delete_by_chunk_locator(
                conn,
                str(row.get("doc_url") or ""),
                int(row.get("para_index") or 0),
                int(row.get("char_start") or 0),
                int(row.get("char_end") or 0),
                with_fts=build_fts,
                with_vec=build_vectors and dim is not None,
            )
        conn.commit()
    finally:
        conn.close()

    if not rows:
        conn = connect_corpus_db(db_path)
        try:
            count = corpus_chunk_count(conn)
        finally:
            conn.close()

        meta_file = Path(meta_path)
        meta_file.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "schema_version": "3",
            "storage_backend": "llama_index",
            "embedding_model": model_name,
            "dim": str(dim or 0),
            "chunk_count": str(count),
            "updated_at": str(time.time()),
        }
        meta_file.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
        return {"indexed": 0, "dim": dim or 0, "storage_backend": "llama_index"}

    nodes = []
    for row in rows:
        text = str(row.get("text") or "").strip()
        if not text:
            continue
        node = TextNode(
            text=text,
            metadata={
                "doc_url": str(row.get("doc_url") or ""),
                "para_index": int(row.get("para_index") or 0),
                "char_start": int(row.get("char_start") or 0),
                "char_end": int(row.get("char_end") or len(text)),
                "content_hash": str(row.get("content_hash") or ""),
                "file_mtime": float(row.get("file_mtime") or 0.0),
            },
        )
        nodes.append(node)

    from plugin.framework.constants import EMBEDDINGS_INGEST_BATCH_SIZE

    vector_store = WriterAgentVectorStore(
        db_path=db_path,
        embedding_model=model_name,
        build_fts=build_fts,
        build_vectors=build_vectors,
    )
    embed_model = WriterAgentEmbedding(
        model_name=model_name,
        embed_batch_size=EMBEDDINGS_INGEST_BATCH_SIZE,
    )

    index = VectorStoreIndex.from_vector_store(vector_store, embed_model=embed_model)
    index.insert_nodes(nodes)

    conn = connect_corpus_db(db_path)
    try:
        count = corpus_chunk_count(conn)
    finally:
        conn.close()

    final_dim = len(nodes[0].embedding) if nodes and nodes[0].embedding else (dim or 0)

    meta_file = Path(meta_path)
    meta_file.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": "3",
        "storage_backend": "llama_index",
        "embedding_model": model_name,
        "dim": str(final_dim),
        "chunk_count": str(count),
        "updated_at": str(time.time()),
    }
    meta_file.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")

    return {"indexed": len(nodes), "dim": final_dim, "storage_backend": "llama_index"}


def llama_index_knn_search(
    db_path: str,
    query_text: str,
    k: int,
    *,
    model_name: str,
    doc_url_filter: str | None = None,
) -> dict[str, Any]:
    """Semantic vector search using LlamaIndex VectorStoreIndex."""
    if not HAS_LLAMA_INDEX:
        raise ImportError("llama-index-core is not installed in the configured Python venv.")

    vector_store = WriterAgentVectorStore(
        db_path=db_path,
        embedding_model=model_name,
        build_fts=False,
        build_vectors=True,
    )
    embed_model = WriterAgentEmbedding(model_name=model_name)
    index = VectorStoreIndex.from_vector_store(vector_store, embed_model=embed_model)

    filters = None
    if doc_url_filter:
        from llama_index.core.vector_stores.types import MetadataFilter, MetadataFilters  # type: ignore
        filters = MetadataFilters(filters=[
            MetadataFilter(key="doc_url", value=doc_url_filter),
        ])

    retriever = index.as_retriever(similarity_top_k=k, filters=filters)
    nodes = retriever.retrieve(query_text)

    from plugin.embeddings.venv.embeddings_search_graph import _hit_snippet

    hits = []
    for n in nodes:
        metadata = n.node.metadata or {}
        hits.append({
            "chunk_id": int(n.node.node_id) if n.node.node_id.isdigit() else None,
            "doc_url": metadata.get("doc_url", ""),
            "para_index": metadata.get("para_index", 0),
            "snippet": _hit_snippet(n.node.text),
            "score": n.score or 0.0,
        })
    return {"hits": hits}


def llama_index_hybrid_search(
    db_path: str,
    query_text: str,
    k: int,
    *,
    model_name: str,
    near_slop: int = 10,
    doc_url_filter: str | None = None,
    use_mmr: bool = True,
) -> dict[str, Any]:
    """Hybrid FTS + vector search fused using RRF and postprocessed with MMR via LlamaIndex."""
    if not HAS_LLAMA_INDEX:
        raise ImportError("llama-index-core is not installed in the configured Python venv.")

    vector_store = WriterAgentVectorStore(
        db_path=db_path,
        embedding_model=model_name,
        build_fts=True,
        build_vectors=True,
    )
    embed_model = WriterAgentEmbedding(model_name=model_name)
    index = VectorStoreIndex.from_vector_store(vector_store, embed_model=embed_model)

    # Fetch intermediate candidates pool
    fetch_k = max(k, min(30, 50))

    filters = None
    if doc_url_filter:
        from llama_index.core.vector_stores.types import MetadataFilter, MetadataFilters  # type: ignore
        filters = MetadataFilters(filters=[
            MetadataFilter(key="doc_url", value=doc_url_filter),
        ])

    vector_retriever = index.as_retriever(similarity_top_k=fetch_k, filters=filters)
    fts_retriever = WriterAgentFTSRetriever(db_path=db_path, near_slop=near_slop)

    from llama_index.core.retrievers.fusion_retriever import QueryFusionRetriever  # type: ignore
    fusion_retriever = WriterAgentQueryFusionRetriever(
        retrievers=[vector_retriever, fts_retriever],
        mode=FUSION_MODES.RECIPROCAL_RANK,
        similarity_top_k=fetch_k,
        num_queries=1,
        use_async=False,
    )

    nodes = fusion_retriever.retrieve(query_text)

    if doc_url_filter:
        allowed = str(doc_url_filter)
        nodes = [n for n in nodes if str(n.node.metadata.get("doc_url") or "") == allowed]

    if use_mmr and nodes and k > 1:
        query_vec = embed_model.get_query_embedding(query_text)
        postprocessor = WriterAgentMMRPostprocessor(query_vec=query_vec, db_path=db_path)
        nodes = postprocessor.postprocess_nodes(nodes)

    nodes = nodes[:k]

    from plugin.embeddings.venv.embeddings_search_graph import _public_hit_from_candidate

    hits = []
    for n in nodes:
        metadata = n.node.metadata or {}
        cand = {
            "chunk_id": int(n.node.node_id) if n.node.node_id.isdigit() else None,
            "doc_url": metadata.get("doc_url", ""),
            "para_index": metadata.get("para_index", 0),
            "snippet": n.node.text,
            "score": n.score or 0.0,
        }
        hits.append(_public_hit_from_candidate(cand))

    return {"hits": hits}
