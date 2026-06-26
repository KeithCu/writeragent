# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Trusted venv LlamaIndex search/indexing backend.

Uses idiomatic LlamaIndex retrieval (QueryFusionRetriever + optional SentenceTransformerRerank)
over the existing SQLite corpus.db. Storage and ODF extraction stay WriterAgent-owned; this
module only orchestrates retrieve → rerank → tool hits.
"""
from __future__ import annotations

import logging
from typing import Any, Optional, Sequence

from plugin.embeddings.venv.embeddings_retrieval_pool import _MIN_FETCH_K, hybrid_retrieval_pool

log = logging.getLogger(__name__)

_DEFAULT_RERANK_MODEL = "cross-encoder/ms-marco-MiniLM-L-6-v2"  # English only; see Settings folder_rerank_model
_FTS_LEG = "fts"
_VECTOR_LEG = "vec"

try:
    from llama_index.core import QueryBundle, VectorStoreIndex  # type: ignore
    from llama_index.core.base.base_retriever import BaseRetriever  # type: ignore
    from llama_index.core.embeddings import BaseEmbedding  # type: ignore
    from llama_index.core.postprocessor import SentenceTransformerRerank  # type: ignore
    from llama_index.core.llms.mock import MockLLM  # type: ignore
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
    HAS_SENTENCE_TRANSFORMER_RERANK = True
except ImportError:
    HAS_LLAMA_INDEX = False
    HAS_SENTENCE_TRANSFORMER_RERANK = False

    class BaseEmbedding:  # type: ignore[no-redef]
        pass

    class BasePydanticVectorStore:  # type: ignore[no-redef]
        pass

    class BaseRetriever:  # type: ignore[no-redef]
        pass

    class QueryFusionRetriever:  # type: ignore[no-redef]
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

    class TextNode:  # type: ignore[no-redef]
        embedding: Any = None
        metadata: Any = None
        text: str = ""
        node_id: str = ""

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

    class SentenceTransformerRerank:  # type: ignore[no-redef]
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        def postprocess_nodes(self, nodes: list[Any], query_bundle: Any = None) -> list[Any]:
            return nodes

    class MockLLM:  # type: ignore[no-redef]
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

    def PrivateAttr(*args: Any, **kwargs: Any) -> Any:  # type: ignore[no-redef]
        pass


def _llama_index_retrieval_pool(k: int) -> tuple[int, int]:
    """Return (final_k, fetch_k) for over-retrieval before cross-encoder rerank."""
    return hybrid_retrieval_pool(k)


def _resolve_rerank_model(rerank_model: str | None) -> str:
    model = str(rerank_model or "").strip()
    return model or _DEFAULT_RERANK_MODEL


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
        if metadata.get("parent_expanded"):
            cand["parent_expanded"] = True
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


def _nodes_to_rerank_candidates(nodes: list[Any]) -> list[dict[str, Any]]:
    """Map LlamaIndex NodeWithScore rows to hybrid rerank candidate dicts."""
    candidates: list[dict[str, Any]] = []
    for item in nodes:
        node = item.node
        metadata = node.metadata or {}
        candidates.append(
            {
                "doc_url": metadata.get("doc_url", ""),
                "para_index": metadata.get("para_index", 0),
                "chunk_id": metadata.get("chunk_id"),
                "snippet": str(node.text or ""),
                "score": float(item.score or 0.0),
                "matched_by": metadata.get("matched_by"),
                "parent_expanded": metadata.get("parent_expanded"),
            }
        )
    return candidates


def _rerank_candidates_to_nodes(candidates: list[dict[str, Any]]) -> list[Any]:
    """Rebuild NodeWithScore list after cross-encoder rerank."""
    reranked: list[Any] = []
    for cand in candidates:
        metadata = {
            "doc_url": cand.get("doc_url", ""),
            "para_index": cand.get("para_index", 0),
            "chunk_id": cand.get("chunk_id"),
            "matched_by": cand.get("matched_by"),
        }
        if cand.get("parent_expanded"):
            metadata["parent_expanded"] = True
        node = TextNode(
            text=str(cand.get("snippet") or ""),
            id_=str(cand.get("chunk_id") or ""),
            metadata=metadata,
        )
        reranked.append(NodeWithScore(node=node, score=float(cand.get("score") or 0.0)))
    return reranked


def _apply_llama_index_postprocessors(
    nodes: list[Any],
    query_bundle: Any,
    *,
    final_k: int,
    use_rerank: bool,
    rerank_model: str = _DEFAULT_RERANK_MODEL,
) -> list[Any]:
    """Rerank fused nodes with shared cross-encoder loader; fall back to RRF top-k."""
    if not nodes:
        return []
    if use_rerank and final_k > 1 and HAS_LLAMA_INDEX:
        try:
            from plugin.embeddings.venv.embeddings_cross_encoder_rerank import cross_encoder_rerank_candidates

            query_text = str(getattr(query_bundle, "query_str", None) or "")
            candidates = _nodes_to_rerank_candidates(nodes)
            reranked = cross_encoder_rerank_candidates(
                query_text,
                candidates,
                model=rerank_model,
                top_n=final_k,
            )
            if reranked:
                return _rerank_candidates_to_nodes(reranked)
        except Exception:
            log.exception("CrossEncoder rerank failed; using fused top-k")
    return nodes[:final_k]


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
                ensure_schema(
                    conn,
                    dim=dim,
                    with_fts=self._build_fts,
                    with_vec=self._build_vectors,
                    model=self._embedding_model,
                )
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
            similarity_top_k: int = _MIN_FETCH_K,
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


def build_writer_agent_hybrid_retriever(
    db_path: str,
    model_name: str,
    *,
    fetch_k: int,
    near_slop: int = 10,
    doc_url_filter: str | None = None,
) -> Any:
    """Standard LlamaIndex QueryFusionRetriever over sqlite-vec + FTS5 legs."""
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
    # num_queries=1 never calls the LLM, but QueryFusionRetriever still resolves
    # Settings.llm (OpenAI) when llm is omitted — pass MockLLM for offline use.
    return QueryFusionRetriever(
        retrievers=[vector_retriever, fts_retriever],
        mode=FUSION_MODES.RECIPROCAL_RANK,
        similarity_top_k=fetch_k,
        num_queries=1,
        use_async=False,
        llm=MockLLM(),
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
    rerank_model: str | None = None,
) -> list[dict[str, Any]]:
    """Retrieve with RRF fusion, then optional SentenceTransformerRerank (use_mmr=True)."""
    if not HAS_LLAMA_INDEX:
        raise ImportError("llama-index-core is not installed in the configured Python venv.")

    query = str(query_text or "").strip()
    if not query:
        return []

    final_k, fetch_k = _llama_index_retrieval_pool(k)
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

    from plugin.embeddings.venv.embeddings_parent_hits import expand_nodes_to_parent_paragraphs

    nodes = expand_nodes_to_parent_paragraphs(db_path, nodes)

    nodes = _apply_llama_index_postprocessors(
        nodes,
        QueryBundle(query_str=query),
        final_k=final_k,
        use_rerank=use_mmr,
        rerank_model=_resolve_rerank_model(rerank_model),
    )
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
    use_mmr: bool = True,
    rerank_model: str | None = None,
) -> dict[str, Any]:
    """Semantic search: over-retrieve from VectorStoreIndex, then cross-encoder rerank."""
    if not HAS_LLAMA_INDEX:
        raise ImportError("llama-index-core is not installed in the configured Python venv.")

    query = str(query_text or "").strip()
    if not query:
        return {"hits": []}

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

    final_k, fetch_k = _llama_index_retrieval_pool(k)
    retriever = index.as_retriever(similarity_top_k=fetch_k, filters=filters)
    nodes = retriever.retrieve(query)
    from plugin.embeddings.venv.embeddings_parent_hits import expand_nodes_to_parent_paragraphs

    nodes = expand_nodes_to_parent_paragraphs(db_path, nodes)
    nodes = _apply_llama_index_postprocessors(
        nodes,
        QueryBundle(query_str=query),
        final_k=final_k,
        use_rerank=use_mmr,
        rerank_model=_resolve_rerank_model(rerank_model),
    )
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
    rerank_model: str | None = None,
) -> dict[str, Any]:
    """Hybrid FTS + vector: QueryFusionRetriever RRF, then SentenceTransformerRerank when use_mmr."""
    hits = run_hybrid_retrieval_pipeline(
        db_path,
        query_text,
        k,
        model_name=model_name,
        near_slop=near_slop,
        doc_url_filter=doc_url_filter,
        use_mmr=use_mmr,
        rerank_model=rerank_model,
    )
    return {"hits": hits}


__all__ = [
    "HAS_LLAMA_INDEX",
    "HAS_SENTENCE_TRANSFORMER_RERANK",
    "WriterAgentEmbedding",
    "WriterAgentFTSRetriever",
    "WriterAgentVectorStore",
    "_DEFAULT_RERANK_MODEL",
    "_llama_index_retrieval_pool",
    "_nodes_to_tool_hits",
    "build_writer_agent_hybrid_retriever",
    "llama_index_hybrid_search",
    "llama_index_ingest",
    "llama_index_knn_search",
    "run_hybrid_retrieval_pipeline",
]
