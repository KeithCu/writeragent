# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Trusted venv backend for zvec (Alibaba in-process vector DB) as side-by-side store.

Selected via Settings → Embeddings → Cross-file search = "Zvec (experimental)".
Requires the user to have `pip install zvec` (and its numpy etc) in the embeddings Python venv.
The OXT itself does not ship or depend on the zvec native wheel.

This module implements the same facades used by embeddings_index (ingest, delete, knn/hybrid search)
and a maintain_folder_zvec entry used by embeddings_folder_maintain when search_mode=="zvec".

Result shapes for hits match the existing contract:
  {"doc_url": str, "score": float, "snippet": str, "para_index": int|None, ...}

Storage layout (per listing folder):
  writeragent_embeddings/
    zvec/                 # the path passed to zvec.create_and_open / open
    corpus_meta.json      # shared meta (chunk_count, embedding_model, etc.) updated by this path too
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any, Callable

log = logging.getLogger(__name__)

# We intentionally do NOT list zvec in the OXT pyproject dev deps.
# Users opt-in by installing it into their configured "Python Test" / embeddings venv.
# The import is guarded exactly like the llama_index path.

try:
    import zvec as _zvec  # type: ignore[import-not-found]
    HAS_ZVEC = True
except Exception:
    _zvec = None  # type: ignore[assignment]
    HAS_ZVEC = False

# Re-exports / aliases for code that wants the module object (guarded by HAS_ZVEC).
zvec = _zvec  # type: ignore[assignment]


# Reuse the project's embedder (sentence-transformers) already present for embeddings work.
# This keeps model choice, batching, and normalize behavior consistent with other backends.
def _embed_texts(model_name: str, texts: list[str], *, normalize: bool = True) -> list[list[float]]:
    from plugin.embeddings.venv.embeddings_index import embed_texts as _et

    out = _et(model_name, texts, normalize=normalize)
    return out.get("vectors") or []


def _stable_doc_id(row: dict[str, Any]) -> str:
    """Stable unique id for a paragraph chunk. Used as zvec Doc id for upsert/delete by id."""
    # content_hash is already a good content identity; combine with locator for global uniqueness.
    doc_url = str(row.get("doc_url") or "")
    para = row.get("para_index")
    ch = str(row.get("content_hash") or "")[:16]
    return f"{doc_url}#{para}#{ch}"


def _get_or_create_collection(coll_path: str, dim: int) -> Any:
    """Open existing zvec collection or create with a schema that supports hybrid FTS + vector + provenance."""
    if not HAS_ZVEC or zvec is None:
        raise RuntimeError("zvec package is not importable in this Python venv. Install with: pip install zvec")

    p = Path(coll_path)
    p.parent.mkdir(parents=True, exist_ok=True)

    schema = _zvec.CollectionSchema(  # type: ignore[attr-defined]
        name="wa_folder_corpus",
        fields=[
            _zvec.FieldSchema("doc_url", _zvec.DataType.STRING, nullable=False),  # type: ignore[attr-defined]
            _zvec.FieldSchema(  # type: ignore[attr-defined]
                "body",
                _zvec.DataType.STRING,  # type: ignore[attr-defined]
                nullable=False,
                index_param=_zvec.FtsIndexParam(tokenizer_name="standard", filters=["lowercase"]),  # type: ignore[attr-defined]
            ),
            _zvec.FieldSchema("para_index", _zvec.DataType.INT32, nullable=True),  # type: ignore[attr-defined]
            _zvec.FieldSchema("content_hash", _zvec.DataType.STRING, nullable=True),  # type: ignore[attr-defined]
            _zvec.FieldSchema("file_mtime", _zvec.DataType.DOUBLE, nullable=True),  # type: ignore[attr-defined]
        ],
        vectors=[
            _zvec.VectorSchema(  # type: ignore[attr-defined]
                "embedding",
                _zvec.DataType.VECTOR_FP32,  # type: ignore[attr-defined]
                dimension=int(dim),
                # HNSW gives good default recall/latency; users can optimize() later if desired.
                index_param=None,  # VectorSchema defaults to Flat if None; pass HnswIndexParam() for ANN
            ),
        ],
    )

    try:
        coll = zvec.open(str(p))  # type: ignore[attr-defined]
        # If dim changed (model switch), the caller (maintain) should have cleared or we could destroy here.
        # For v1 we let the query path use whatever dim the collection has; embedder must match.
        return coll
    except Exception:
        # Does not exist or failed to open (e.g. first run) — create fresh.
        coll = zvec.create_and_open(str(p), schema=schema)  # type: ignore[attr-defined]
        return coll


def _open_for_search(coll_path: str) -> Any:
    if not HAS_ZVEC or zvec is None:
        raise RuntimeError("zvec package is not importable in this Python venv.")
    p = Path(coll_path)
    return zvec.open(str(p))  # type: ignore[attr-defined]


def zvec_ingest_rows(
    collection_path: str,
    meta_path: str,
    model_name: str,
    rows: list[dict[str, Any]],
    *,
    build_fts: bool = True,
    build_vectors: bool = True,
) -> dict[str, Any]:
    """Ingest/upsert paragraph rows into a zvec collection.

    For zvec the build_* flags are mostly advisory (we store body for FTS and vectors when asked).
    The 'collection_path' here is the path string the caller resolved for zvec mode (see tools/service).
    """
    if not rows:
        return {"indexed": 0, "upserted": 0, "dim": 0, "storage_backend": "zvec"}

    if build_vectors and not (model_name or "").strip():
        raise ValueError("embedding model name is required for zvec vector ingestion")

    # Compute embeddings for the bodies (map "text" -> embed input; we store as "body").
    bodies = [str(r.get("text") or r.get("body") or "") for r in rows]
    vectors: list[list[float]] = []
    dim = 0
    if build_vectors:
        vectors = _embed_texts(model_name, bodies, normalize=True)
        if vectors:
            dim = len(vectors[0])

    coll = _get_or_create_collection(collection_path, dim or (len(vectors[0]) if vectors else 0))

    docs: list[Any] = []
    for i, row in enumerate(rows):
        body = bodies[i]
        vec: list[float] | None = vectors[i] if i < len(vectors) else None
        doc = zvec.Doc(  # type: ignore[attr-defined]
            id=_stable_doc_id(row),
            fields={
                "doc_url": str(row.get("doc_url") or ""),
                "body": body,
                "para_index": int(row.get("para_index") or 0),
                "content_hash": str(row.get("content_hash") or ""),
                "file_mtime": float(row.get("file_mtime") or 0.0),
            },
            vectors={"embedding": vec} if vec is not None else None,
        )
        docs.append(doc)

    # Upsert by our stable id (insert or replace). This gives reasonable per-chunk update behavior.
    statuses = coll.upsert(docs)
    upserted = len([s for s in (statuses if isinstance(statuses, list) else [statuses]) if getattr(s, "ok", True)])

    # Best-effort meta update (chunk count via stats if available, else len(docs) delta).
    try:
        st = getattr(coll, "stats", None)
        count = int(getattr(st, "doc_count", 0) or 0) if st is not None else 0
    except Exception:
        count = 0
    if count == 0:
        count = len(docs)

    # Write/refresh the shared corpus_meta.json so host empty checks and UI see progress.
    from plugin.embeddings.embeddings_cache import ensure_corpus_meta, write_corpus_meta

    meta_p = Path(str(meta_path))
    ensure_corpus_meta(
        meta_p,
        embedding_model=model_name,
        dim=dim or None,
        chunk_count=count,
    )
    # Override storage backend for visibility
    write_corpus_meta(meta_p, storage_backend="zvec", updated_at=str(time.time()))

    try:
        coll.flush()
    except Exception:
        pass

    return {
        "indexed": len(docs),
        "upserted": upserted,
        "dim": dim,
        "storage_backend": "zvec",
    }


def zvec_delete_keys(collection_path: str, keys: list[dict[str, Any]]) -> int:
    """Delete docs by the stable ids derived from the same row key dicts used for ingest."""
    if not keys:
        return 0
    if not HAS_ZVEC or zvec is None:
        return 0
    coll = zvec.open(str(collection_path))  # type: ignore[attr-defined]
    ids = [_stable_doc_id(k) for k in keys]
    # zvec delete accepts str or list[str]
    res = coll.delete(ids)
    try:
        coll.flush()
    except Exception:
        pass
    if isinstance(res, list):
        return sum(1 for r in res if getattr(r, "ok", True))
    return 1 if getattr(res, "ok", True) else 0


def _embed_query(model_name: str, query_text: str) -> list[float]:
    vecs = _embed_texts(model_name, [query_text], normalize=True)
    return vecs[0] if vecs else []


def _shape_hit(d: Any) -> dict[str, Any]:
    """Map a zvec Doc result to the WA hit contract used by tools and UI."""
    return {
        "doc_url": d.field("doc_url") if hasattr(d, "field") else (d.fields or {}).get("doc_url"),
        "score": float(getattr(d, "score", 0.0) or 0.0),
        "snippet": (d.field("body") if hasattr(d, "field") else (d.fields or {}).get("body") or "").strip(),
        "para_index": d.field("para_index") if hasattr(d, "field") else (d.fields or {}).get("para_index"),
        "content_hash": d.field("content_hash") if hasattr(d, "field") else (d.fields or {}).get("content_hash"),
    }


def zvec_knn_search(
    collection_path: str,
    query_text: str,
    k: int,
    *,
    model_name: str,
    doc_url_filter: str | None = None,
    use_mmr: bool = True,
    rerank_model: str | None = None,
) -> dict[str, Any]:
    """Semantic-only search over the zvec collection (vector leg)."""
    if not HAS_ZVEC or zvec is None:
        return {"hits": [], "error": "zvec not available in venv", "backend": "zvec"}
    coll = _open_for_search(collection_path)
    qvec = _embed_query(model_name, query_text)
    if not qvec:
        return {"hits": [], "backend": "zvec"}

    q = zvec.Query(field_name="embedding", vector=qvec)  # type: ignore[attr-defined]
    # output_fields restricts what comes back in Doc.fields
    out_fields = ["doc_url", "body", "para_index", "content_hash"]
    try:
        docs = coll.query(
            queries=[q],
            topk=int(k or 5),
            include_vector=False,
            output_fields=out_fields,
            filter=f'doc_url == "{doc_url_filter}"' if doc_url_filter else None,
        )
    except Exception:
        # Some zvec builds may be strict on filter syntax or empty filter; retry without.
        docs = coll.query(queries=[q], topk=int(k or 5), include_vector=False, output_fields=out_fields)

    hits = [_shape_hit(d) for d in (docs or [])]
    # Optional client-side MMR or rerank could be applied here using rerank_model, but native zvec
    # RRF is preferred when doing hybrid. For pure knn we return as-is (use_mmr is advisory).
    return {"hits": hits, "backend": "zvec"}


def zvec_hybrid_search(
    collection_path: str,
    query_text: str,
    k: int,
    *,
    model_name: str,
    near_slop: int = 10,
    doc_url_filter: str | None = None,
    use_mmr: bool = True,
    rerank_model: str | None = None,
) -> dict[str, Any]:
    """Hybrid FTS + vector via zvec multi-query + native RRF (or Weighted if reranker provided)."""
    if not HAS_ZVEC or zvec is None:
        return {"hits": [], "error": "zvec not available in venv", "backend": "zvec"}
    coll = _open_for_search(collection_path)
    qvec: list[float] | None = _embed_query(model_name, query_text)
    if not qvec:
        # Fall back to FTS-only leg
        qvec = None

    qs: list[Any] = []
    if qvec:
        qs.append(zvec.Query(field_name="embedding", vector=qvec))  # type: ignore[attr-defined]
    # FTS leg: use match_string for natural language (tokenized). near_slop is not directly mapped;
    # advanced users can use query_string with NEAR if desired. We keep it simple and effective.
    qs.append(zvec.Query(field_name="body", fts=zvec.Fts(match_string=query_text)))  # type: ignore[attr-defined]

    out_fields = ["doc_url", "body", "para_index", "content_hash"]
    flt = f'doc_url == "{doc_url_filter}"' if doc_url_filter else None

    reranker = None
    try:
        # Prefer built-in RRF for hybrid when no custom rerank_model requested.
        if rerank_model:
            # Could load a cross-encoder via zvec rerank extensions or fall back; for v1 use RRF anyway.
            reranker = zvec.extension.RrfReRanker(rank_constant=60)  # type: ignore[attr-defined]
        else:
            reranker = zvec.extension.RrfReRanker(rank_constant=60)  # type: ignore[attr-defined]
    except Exception:
        reranker = None

    try:
        docs = coll.query(
            queries=qs,
            topk=int(k or 10),
            filter=flt,
            include_vector=False,
            output_fields=out_fields,
            reranker=reranker,
        )
    except Exception:
        # Retry without reranker if the installed zvec build has limitations on this query mix.
        docs = coll.query(
            queries=qs,
            topk=int(k or 10),
            filter=flt,
            include_vector=False,
            output_fields=out_fields,
        )

    hits = [_shape_hit(d) for d in (docs or [])]
    return {"hits": hits, "backend": "zvec"}


def maintain_folder_zvec(
    listing_root: str,
    embedding_model: str,
    *,
    mode: str = "auto",
    heartbeat_fn: Callable[[dict[str, Any]], None] | None = None,
    hb: Any | None = None,
) -> dict[str, Any]:
    """Zvec-specific folder maintain: extract current paragraphs for files, (re)upsert into zvec collection.

    For the first implementation we do a straightforward "current state" build:
    - For each indexable file: delete previous entries for its doc_url (simple + correct), extract fresh,
      embed, upsert Docs, heartbeat progress.
    - At end flush and update meta (so host UI and empty checks see the count and model).
    This is intentionally simple (no dependency on sqlite indexed_* state) so zvec works side-by-side
    and can be selected even on a folder that has never used the sqlite backend.
    """
    if not HAS_ZVEC or zvec is None:
        raise RuntimeError(
            "Zvec backend selected but the 'zvec' package is not importable in the configured Python venv. "
            "Install it with: pip install zvec  (then restart LibreOffice or re-trigger the worker)."
        )

    from plugin.embeddings.embeddings_cache import ensure_corpus_meta, write_corpus_meta, zvec_collection_path
    from plugin.embeddings.embeddings_fs import guess_indexable_paths, indexable_chunks_from_path
    from plugin.framework.constants import EMBEDDINGS_HEARTBEAT_INTERVAL_S

    root = str(listing_root or "").strip()
    if not root:
        raise ValueError("listing_root is required")

    coll_path = str(zvec_collection_path(root, create_parent=True))
    meta_path = Path(root) / "writeragent_embeddings" / "corpus_meta.json"

    # Heartbeat helper (reuse the one from the caller if provided)
    class _HB:
        def __init__(self, fn: Callable[[dict[str, Any]], None] | None) -> None:
            self._fn = fn
            self._last = 0.0

        def ping(self, p: dict[str, Any]) -> None:
            if not self._fn:
                return
            now = time.monotonic()
            if now - self._last < EMBEDDINGS_HEARTBEAT_INTERVAL_S:
                return
            self._last = now
            self._fn(p)

        def force(self, p: dict[str, Any]) -> None:
            if not self._fn:
                return
            self._last = time.monotonic()
            self._fn(p)

    _hb = hb or _HB(heartbeat_fn)

    files = guess_indexable_paths(root)
    total = len(files)
    indexed = 0
    upserted_total = 0

    _hb.force({"phase": "start", "mode": "zvec", "listing_root": root, "files": total})

    # Probe dim once by embedding a tiny text (or first real body).
    dim = 0
    for f in files:
        try:
            _, chunks = indexable_chunks_from_path(f.path, doc_url=f.url, file_mtime=f.modified)
            if chunks:
                sample = str(getattr(chunks[0], "text", "") or "")
                if sample:
                    v = _embed_texts(embedding_model, [sample], normalize=True)
                    if v and v[0]:
                        dim = len(v[0])
                    break
        except Exception:
            continue
    if dim <= 0:
        # Fallback common dim for many MiniLM models; will be corrected on first real embed batch.
        dim = 384

    # Ensure meta reflects we are using zvec for this model (even if count is still 0).
    ensure_corpus_meta(meta_path, embedding_model=embedding_model, dim=dim)
    write_corpus_meta(meta_path, storage_backend="zvec")

    for idx, entry in enumerate(files):
        _hb.force({"phase": "extract", "file": entry.name, "index": idx, "total": total, "mode": "zvec"})

        try:
            paragraph_count, chunks = indexable_chunks_from_path(entry.path, doc_url=entry.url, file_mtime=entry.modified)
        except Exception as e:
            log.debug("zvec extract failed for %s: %s", entry.name, e)
            continue

        rows = [{"doc_url": c.doc_url, "para_index": c.para_index, "char_start": c.char_start, "char_end": c.char_end,
                 "content_hash": c.content_hash, "text": c.text, "file_mtime": c.file_mtime} for c in chunks]

        _hb.force(
            {
                "phase": "extract",
                "file": entry.name,
                "paragraphs": paragraph_count,
                "chunks": len(rows),
                "mode": "zvec",
            }
        )

        if not rows:
            # Nothing to index for this file; still "touch" it in meta sense by ensuring collection exists.
            continue

        # For clean per-file refresh, remove any previous docs for this doc_url.
        try:
            coll = zvec.open(coll_path)  # type: ignore[attr-defined]
            coll.delete_by_filter(f'doc_url == "{entry.url}"')
        except Exception:
            # Collection may not exist yet; the upsert below will create on open/create path.
            pass

        res = zvec_ingest_rows(
            coll_path,
            str(meta_path),
            embedding_model,
            rows,
            build_fts=True,
            build_vectors=True,
        )
        up = int(res.get("upserted") or res.get("indexed") or 0)
        upserted_total += up
        indexed += len(rows)

        _hb.force(
            {
                "phase": "index",
                "file": entry.name,
                "paragraphs": paragraph_count,
                "chunks": up,
                "upserted": up,
                "mode": "zvec",
            }
        )

    # Final meta + flush
    try:
        coll = zvec.open(coll_path)  # type: ignore[attr-defined]
        try:
            coll.flush()
        except Exception:
            pass
        try:
            st = getattr(coll, "stats", None)
            final_count = int(getattr(st, "doc_count", 0) or 0) if st is not None else indexed
        except Exception:
            final_count = indexed
    except Exception:
        final_count = indexed

    ensure_corpus_meta(meta_path, embedding_model=embedding_model, dim=dim, chunk_count=final_count)
    write_corpus_meta(meta_path, storage_backend="zvec", updated_at=str(time.time()))

    _hb.force({"phase": "done", "mode": "zvec", "indexed_paragraphs": indexed, "upserted": upserted_total})

    return {
        "mode": "zvec",
        "indexed_paragraphs": indexed,
        "files": total,
        "upserted": upserted_total,
        "row_count": final_count,
        "storage_backend": "zvec",
    }


__all__ = [
    "HAS_ZVEC",
    "maintain_folder_zvec",
    "zvec_delete_keys",
    "zvec_hybrid_search",
    "zvec_ingest_rows",
    "zvec_knn_search",
]