# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Trusted venv LlamaIndex search/indexing backend.

Bridges LlamaIndex with the existing SQLite chunks, vec_chunks, and passages FTS5 tables.
Retrieval is composed from vector + FTS retrievers, RRF fusion, and WriterAgent postprocessors.
"""
from __future__ import annotations

import logging
from typing import Any, Optional, Sequence

log = logging.getLogger(__name__)

_DEFAULT_POOL_K = 30
_MAX_POOL_K = 50
_DEFAULT_RRF_K = 60.0
_DEFAULT_MAX_PER_DOC = 3
_VECTOR_LEG = "vec"
_FTS_LEG = "fts"
_RETRIEVER_LEGS = (_VECTOR_LEG, _FTS_LEG)

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

    class BaseEmbedding:  # type: ignore[no-redef]
        pass

    class BasePydanticVectorStore:  # type: ignore[no-redef]
        pass

    class BaseRetriever:  # type: ignore[no-redef]
        pass

    class QueryFusionRetriever:  # type: ignore[no-redef]
        pass

    class BaseNodePostprocessor:  # type: ignore[no-redef]
        pass

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

    def PrivateAttr(*args: Any, **kwargs: Any) -> Any:  # type: ignore[no-redef]
        pass


def _fetch_pool_k(k: int, *, pool_k: int = _DEFAULT_POOL_K, max_pool: int = _MAX_POOL_K) -> tuple[int, int]:
    """Return (final_k, fetch_k) matching custom hybrid_corpus_search pool sizing."""
    final_k = max(1, min(int(k or 10), 30))
    fetch_k = max(final_k, min(int(pool_k), max_pool))
    return final_k, fetch_k


def _doc_url_from_node(node: Any) -> str:
    metadata = getattr(node, "metadata", None) or {}
    return str(metadata.get("doc_url") or "")


def source_diversity_filter(nodes: list[Any], *, max_per_doc: int) -> list[Any]:
    """Keep rank order but cap how many nodes share the same doc_url."""
    if max_per_doc <= 0:
        return list(nodes)
    counts: dict[str, int] = {}
    kept: list[Any] = []
    for item in nodes:
        doc_url = _doc_url_from_node(getattr(item, "node", item))
        seen = counts.get(doc_url, 0)
        if seen >= max_per_doc:
            continue
        counts[doc_url] = seen + 1
        kept.append(item)
    return kept


def _nodes_to_tool_hits(nodes: list[Any]) -> list[dict[str, Any]]:
    """Convert LlamaIndex nodes to search_nearby_files-compatible hit dicts."""
    from plugin.embeddings.venv.embeddings_search_graph import _public_hit_from_candidate

    hits: list[dict[str, Any]] = []
    for n in nodes:
        metadata = n.node.metadata or {}
        node_id = str(n.node.node_id or "")
        cand = {
            "chunk_id": int(node_id) if node_id.isdigit() else None,
            "doc_url": metadata.get("doc_url", ""),
            "para_index": metadata.get("para_index", 0),
            "snippet": n.node.text,
            "score": n.score or 0.0,
        }
        hit = _public_hit_from_candidate(cand)
        matched_by = metadata.get("matched_by")
        if matched_by:
            if isinstance(matched_by, (list, tuple, set)):
                hit["matched_by"] = sorted({str(x) for x in matched_by})
            else:
                hit["matched_by"] = [str(matched_by)]
        hits.append(hit)
    return hits


def _sqlite_hit_to_node(hit: dict[str, Any], *, leg: str) -> Any:
    """Build a TextNode + NodeWithScore from a corpus.db hit dict."""
    raw_score = hit.get("score")
    metadata = {
        "doc_url": hit.get("doc_url", ""),
        "para_index": hit.get("para_index", 0),
        "chunk_id": hit.get("chunk_id"),
        "raw_score": raw_score,
        "matched_by": [leg],
    }
    node = TextNode(
        text=str(hit.get("snippet") or ""),
        id_=str(hit.get("chunk_id")),
        metadata=metadata,
    )
    score = float(raw_score or 0.0)
    if leg == _FTS_LEG:
        score = -score
    return NodeWithScore(node=node, score=score)


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
            from plugin.embeddings.venv.embeddings_sqlite import _delete_chunk_ids, connect_corpus_db

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
                        metadata={
                            "doc_url": cand["doc_url"],
                            "para_index": cand["para_index"],
                            "chunk_id": cand["chunk_id"],
                            "raw_score": cand["score"],
                            "matched_by": [_VECTOR_LEG],
                        },
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
        _similarity_top_k: int = PrivateAttr()

        def __init__(
            self,
            db_path: str,
            near_slop: int = 10,
            *,
            similarity_top_k: int = _DEFAULT_POOL_K,
            **kwargs: Any,
        ) -> None:
            super().__init__(**kwargs)
            self._db_path = db_path
            self._near_slop = near_slop
            self._similarity_top_k = max(1, int(similarity_top_k))

        def _retrieve(self, query_bundle: QueryBundle) -> list[NodeWithScore]:
            from plugin.embeddings.venv.embeddings_sqlite import connect_corpus_db, fts_corpus_search

            conn = connect_corpus_db(self._db_path)
            try:
                hits = fts_corpus_search(
                    conn,
                    query_bundle.query_str,
                    k=self._similarity_top_k,
                    near_slop=self._near_slop,
                )
                return [_sqlite_hit_to_node(hit, leg=_FTS_LEG) for hit in hits]
            finally:
                conn.close()

    class WriterAgentQueryFusionRetriever(QueryFusionRetriever):  # type: ignore[valid-type, misc]
        """RRF fusion with configurable k and matched_by provenance on fused nodes."""

        _rrf_k: float = PrivateAttr()

        def __init__(self, *args: Any, rrf_k: float = _DEFAULT_RRF_K, **kwargs: Any) -> None:
            super().__init__(*args, **kwargs)
            self._rrf_k = rrf_k

        def _leg_for_retriever(self, retriever_idx: int) -> str:
            if 0 <= retriever_idx < len(_RETRIEVER_LEGS):
                return _RETRIEVER_LEGS[retriever_idx]
            return f"r{retriever_idx}"

        def _reciprocal_rerank_fusion(self, results: dict[tuple[str, int], list[NodeWithScore]]) -> list[NodeWithScore]:
            fused_scores: dict[str, float] = {}
            hash_to_node: dict[str, NodeWithScore] = {}
            hash_matched_by: dict[str, set[str]] = {}

            for key, nodes_with_scores in results.items():
                retriever_idx = key[1] if isinstance(key, tuple) and len(key) > 1 else 0
                leg = self._leg_for_retriever(int(retriever_idx))
                ranked = sorted(nodes_with_scores, key=lambda x: x.score or 0.0, reverse=True)
                for rank, node_with_score in enumerate(ranked):
                    hash_val = node_with_score.node.hash
                    hash_to_node[hash_val] = node_with_score
                    fused_scores[hash_val] = fused_scores.get(hash_val, 0.0) + 1.0 / (rank + self._rrf_k)
                    hash_matched_by.setdefault(hash_val, set()).update(
                        node_with_score.node.metadata.get("matched_by") or [leg]
                    )

            reranked_nodes: list[NodeWithScore] = []
            for hash_val, score in sorted(fused_scores.items(), key=lambda x: x[1], reverse=True):
                node_with_score = hash_to_node[hash_val]
                node_with_score.score = score
                meta = dict(node_with_score.node.metadata or {})
                meta["matched_by"] = sorted(hash_matched_by.get(hash_val, set()))
                node_with_score.node.metadata = meta
                reranked_nodes.append(node_with_score)
            return reranked_nodes

    class WriterAgentWeakHitFilterPostprocessor(BaseNodePostprocessor):  # type: ignore[valid-type, misc]
        """Drop very low RRF scores and nodes with no retrieval-leg metadata."""

        _min_score: float = PrivateAttr()

        def __init__(self, min_score: float = 1e-6, **kwargs: Any) -> None:
            super().__init__(**kwargs)
            self._min_score = float(min_score)

        def _postprocess_nodes(
            self,
            nodes: list[NodeWithScore],
            query_bundle: Optional[QueryBundle] = None,
        ) -> list[NodeWithScore]:
            kept: list[NodeWithScore] = []
            for n in nodes:
                score = float(n.score or 0.0)
                if score < self._min_score:
                    continue
                matched_by = (n.node.metadata or {}).get("matched_by") or []
                if matched_by:
                    kept.append(n)
                    continue
                # vec-only knn path may omit matched_by; keep positive scores
                if score > 0.0:
                    kept.append(n)
            return kept

    class WriterAgentSourceDiversityPostprocessor(BaseNodePostprocessor):  # type: ignore[valid-type, misc]
        """Cap how many hits come from the same doc_url (file-level diversity)."""

        _max_per_doc: int = PrivateAttr()

        def __init__(self, max_per_doc: int = _DEFAULT_MAX_PER_DOC, **kwargs: Any) -> None:
            super().__init__(**kwargs)
            self._max_per_doc = max(1, int(max_per_doc))

        def _postprocess_nodes(
            self,
            nodes: list[NodeWithScore],
            query_bundle: Optional[QueryBundle] = None,
        ) -> list[NodeWithScore]:
            return source_diversity_filter(nodes, max_per_doc=self._max_per_doc)

    class WriterAgentMMRPostprocessor(BaseNodePostprocessor):  # type: ignore[valid-type, misc]
        _query_vec: list[float] = PrivateAttr()
        _db_path: str = PrivateAttr()
        _lambda_mult: float = PrivateAttr()
        _top_k: int = PrivateAttr()

        def __init__(
            self,
            query_vec: list[float],
            db_path: str,
            *,
            top_k: int,
            lambda_mult: float = 0.7,
            **kwargs: Any,
        ) -> None:
            super().__init__(**kwargs)
            self._query_vec = query_vec
            self._db_path = db_path
            self._lambda_mult = lambda_mult
            self._top_k = max(1, int(top_k))

        def _postprocess_nodes(
            self,
            nodes: list[NodeWithScore],
            query_bundle: Optional[QueryBundle] = None,
        ) -> list[NodeWithScore]:
            if not nodes:
                return []

            from plugin.embeddings.venv.embeddings_sqlite import connect_corpus_db

            conn = connect_corpus_db(self._db_path)
            embeddings: dict[str, list[float]] = {}
            try:
                for n in nodes:
                    try:
                        cid = int(n.node.node_id)
                        row = conn.execute("SELECT embedding FROM vec_chunks WHERE chunk_id = ?", (cid,)).fetchone()
                        if row:
                            import numpy as np

                            embeddings[n.node.node_id] = np.frombuffer(row["embedding"], dtype=np.float32).tolist()
                    except (ValueError, TypeError):
                        pass
            finally:
                conn.close()

            from plugin.embeddings.venv.embeddings_search_graph import _max_marginal_relevance

            candidates = []
            for n in nodes:
                candidates.append({
                    "node": n,
                    "chunk_id": int(n.node.node_id) if str(n.node.node_id).isdigit() else None,
                    "embedding": embeddings.get(n.node.node_id),
                })

            with_embeddings = [c for c in candidates if c["embedding"] is not None]
            without = [c for c in candidates if c["embedding"] is None]

            reranked: list[NodeWithScore] = []
            if with_embeddings and len(with_embeddings) >= self._top_k:
                import numpy as np

                fused_mmr = _max_marginal_relevance(
                    np.asarray(self._query_vec, dtype=np.float32),
                    [c["embedding"] for c in with_embeddings],
                    with_embeddings,
                    self._top_k,
                    lambda_mult=self._lambda_mult,
                )
                reranked.extend([c["node"] for c in fused_mmr])

            for c in without:
                if len(reranked) >= self._top_k:
                    break
                if c["node"] not in reranked:
                    reranked.append(c["node"])

            if len(reranked) < self._top_k:
                for c in with_embeddings:
                    if len(reranked) >= self._top_k:
                        break
                    if c["node"] not in reranked:
                        reranked.append(c["node"])

            return reranked[: self._top_k]


def build_writer_agent_hybrid_retriever(
    db_path: str,
    model_name: str,
    *,
    fetch_k: int,
    near_slop: int = 10,
    doc_url_filter: str | None = None,
    rrf_k: float = _DEFAULT_RRF_K,
) -> Any:
    """Compose vector + FTS retrievers with offline RRF fusion (num_queries=1)."""
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

    filters = None
    if doc_url_filter:
        from llama_index.core.vector_stores.types import MetadataFilter, MetadataFilters  # type: ignore

        filters = MetadataFilters(filters=[MetadataFilter(key="doc_url", value=doc_url_filter)])

    vector_retriever = index.as_retriever(similarity_top_k=fetch_k, filters=filters)
    fts_retriever = WriterAgentFTSRetriever(
        db_path=db_path,
        near_slop=near_slop,
        similarity_top_k=fetch_k,
    )
    return WriterAgentQueryFusionRetriever(
        retrievers=[vector_retriever, fts_retriever],
        mode=FUSION_MODES.RECIPROCAL_RANK,
        similarity_top_k=fetch_k,
        num_queries=1,
        use_async=False,
        rrf_k=rrf_k,
    )


def run_hybrid_retrieval_pipeline(
    db_path: str,
    query_text: str,
    k: int,
    *,
    model_name: str,
    near_slop: int = 10,
    doc_url_filter: str | None = None,
    use_mmr: bool = True,
) -> list[dict[str, Any]]:
    """Retrieve via LlamaIndex hybrid stack; return tool-compatible hit dicts."""
    if not HAS_LLAMA_INDEX:
        raise ImportError("llama-index-core is not installed in the configured Python venv.")

    query = str(query_text or "").strip()
    if not query:
        return []

    final_k, fetch_k = _fetch_pool_k(k)
    fusion_retriever = build_writer_agent_hybrid_retriever(
        db_path,
        model_name,
        fetch_k=fetch_k,
        near_slop=near_slop,
        doc_url_filter=doc_url_filter,
    )
    nodes = fusion_retriever.retrieve(query)

    if doc_url_filter:
        allowed = str(doc_url_filter)
        nodes = [n for n in nodes if str((n.node.metadata or {}).get("doc_url") or "") == allowed]

    nodes = WriterAgentWeakHitFilterPostprocessor().postprocess_nodes(nodes)
    nodes = WriterAgentSourceDiversityPostprocessor().postprocess_nodes(nodes)

    if use_mmr and nodes and final_k > 1:
        embed_model = WriterAgentEmbedding(model_name=model_name)
        query_vec = embed_model.get_query_embedding(query)
        nodes = WriterAgentMMRPostprocessor(
            query_vec=query_vec,
            db_path=db_path,
            top_k=final_k,
        ).postprocess_nodes(nodes)
    else:
        nodes = nodes[:final_k]

    return _nodes_to_tool_hits(nodes)


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

        filters = MetadataFilters(filters=[MetadataFilter(key="doc_url", value=doc_url_filter)])

    final_k = max(1, min(int(k or 5), 30))
    retriever = index.as_retriever(similarity_top_k=final_k, filters=filters)
    nodes = retriever.retrieve(query_text)
    nodes = WriterAgentWeakHitFilterPostprocessor(min_score=0.0).postprocess_nodes(nodes)
    nodes = nodes[:final_k]
    return {"hits": _nodes_to_tool_hits(nodes)}


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
    """Hybrid FTS + vector search via LlamaIndex retriever composition and postprocessors."""
    hits = run_hybrid_retrieval_pipeline(
        db_path,
        query_text,
        k,
        model_name=model_name,
        near_slop=near_slop,
        doc_url_filter=doc_url_filter,
        use_mmr=use_mmr,
    )
    return {"hits": hits}


__all__ = [
    "HAS_LLAMA_INDEX",
    "WriterAgentEmbedding",
    "WriterAgentFTSRetriever",
    "WriterAgentMMRPostprocessor",
    "WriterAgentQueryFusionRetriever",
    "WriterAgentSourceDiversityPostprocessor",
    "WriterAgentVectorStore",
    "WriterAgentWeakHitFilterPostprocessor",
    "build_writer_agent_hybrid_retriever",
    "llama_index_hybrid_search",
    "llama_index_ingest",
    "llama_index_knn_search",
    "run_hybrid_retrieval_pipeline",
    "source_diversity_filter",
    "_fetch_pool_k",
    "_nodes_to_tool_hits",
]
