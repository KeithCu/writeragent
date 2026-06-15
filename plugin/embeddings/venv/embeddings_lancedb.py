# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Trusted venv backend for LanceDB as a side-by-side store.

Selected via Settings → Embeddings → Cross-file search = "LanceDB (experimental)".
Requires the user to have `pip install lancedb` in their Python venv.
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any, Callable

log = logging.getLogger(__name__)

try:
    import lancedb as _lancedb  # type: ignore[import-not-found]
    import pyarrow as _pa  # type: ignore[import-not-found]
    HAS_LANCEDB = True
except Exception:
    _lancedb = None  # type: ignore[assignment]
    _pa = None  # type: ignore[assignment]
    HAS_LANCEDB = False

lancedb = _lancedb  # type: ignore[assignment]
pa = _pa  # type: ignore[assignment]


def _embed_texts(model_name: str, texts: list[str], *, normalize: bool = True) -> list[list[float]]:
    from plugin.embeddings.venv.embeddings_index import embed_texts as _et
    out = _et(model_name, texts, normalize=normalize)
    return out.get("vectors") or []


def _stable_doc_id(row: dict[str, Any]) -> str:
    """Stable unique id for a paragraph chunk."""
    doc_url = str(row.get("doc_url") or "")
    para = row.get("para_index")
    ch = str(row.get("content_hash") or "")[:16]
    return f"{doc_url}#{para}#{ch}"


def _get_or_create_table(db_path: str, dim: int) -> Any:
    """Open existing LanceDB table or create it with schema."""
    if not HAS_LANCEDB or lancedb is None or pa is None:
        raise RuntimeError("lancedb is not installed in the configured Python venv. Install with: pip install lancedb")

    db = lancedb.connect(db_path)  # type: ignore[attr-defined]
    table_name = "wa_folder_corpus"

    schema = pa.schema([
        pa.field("id", pa.string()),
        pa.field("doc_url", pa.string()),
        pa.field("body", pa.string()),
        pa.field("para_index", pa.int32()),
        pa.field("content_hash", pa.string()),
        pa.field("file_mtime", pa.float64()),
        pa.field("vector", pa.list_(pa.float32(), int(dim))),
    ])

    try:
        tbl = db.open_table(table_name)
        # Check if schema dimensions match. If not, recreate.
        tbl_dim = len(tbl.schema.field("vector").type.value_type)
        if tbl_dim != int(dim):
            log.info("LanceDB table dimension mismatch (%d vs %d), recreating table", tbl_dim, dim)
            tbl = db.create_table(table_name, schema=schema, mode="overwrite")
        return tbl
    except Exception:
        # Create fresh table
        tbl = db.create_table(table_name, schema=schema, mode="overwrite")
        return tbl


def _open_for_search(db_path: str) -> Any:
    if not HAS_LANCEDB or lancedb is None:
        raise RuntimeError("lancedb is not installed in the configured Python venv.")
    db = lancedb.connect(db_path)  # type: ignore[attr-defined]
    return db.open_table("wa_folder_corpus")


def lancedb_ingest_rows(
    db_path: str,
    meta_path: str,
    model_name: str,
    rows: list[dict[str, Any]],
    *,
    build_fts: bool = True,
    build_vectors: bool = True,
) -> dict[str, Any]:
    """Ingest paragraph rows into a LanceDB table."""
    if not rows:
        return {"indexed": 0, "upserted": 0, "dim": 0, "storage_backend": "lancedb"}

    if build_vectors and not (model_name or "").strip():
        raise ValueError("embedding model name is required for LanceDB ingestion")

    bodies = [str(r.get("text") or r.get("body") or "") for r in rows]
    vectors: list[list[float]] = []
    dim = 0
    if build_vectors:
        vectors = _embed_texts(model_name, bodies, normalize=True)
        if vectors:
            dim = len(vectors[0])

    tbl = _get_or_create_table(db_path, dim or (len(vectors[0]) if vectors else 384))

    # Construct the PyArrow Table to add/upsert
    pydict = {
        "id": [_stable_doc_id(r) for r in rows],
        "doc_url": [str(r.get("doc_url") or "") for r in rows],
        "body": bodies,
        "para_index": [int(r.get("para_index") or 0) for r in rows],
        "content_hash": [str(r.get("content_hash") or "") for r in rows],
        "file_mtime": [float(r.get("file_mtime") or 0.0) for r in rows],
        "vector": vectors if vectors else [[0.0] * dim for _ in rows],
    }

    # Perform upsert
    try:
        tbl.upsert(pydict, on="id")
    except Exception:
        # Fallback to delete and add
        ids: list[str] = pydict["id"]  # type: ignore[assignment]
        if ids:
            try:
                filter_expr = "id in (" + ", ".join(f"'{id_}'" for id_ in ids) + ")"
                tbl.delete(filter_expr)
            except Exception:
                pass
        tbl.add(pydict)

    if build_fts:
        try:
            tbl.create_fts_index("body", replace=True)
        except Exception as e:
            log.warning("Failed to create/replace FTS index on body: %s", e)

    count = len(tbl)

    # Write/refresh the shared corpus_meta.json
    from plugin.embeddings.embeddings_cache import ensure_corpus_meta, write_corpus_meta
    meta_p = Path(str(meta_path))
    ensure_corpus_meta(
        meta_p,
        embedding_model=model_name,
        dim=dim or None,
        chunk_count=count,
    )
    write_corpus_meta(meta_p, storage_backend="lancedb", updated_at=str(time.time()))

    return {
        "indexed": len(rows),
        "upserted": len(rows),
        "dim": dim,
        "storage_backend": "lancedb",
    }


def lancedb_delete_keys(db_path: str, keys: list[dict[str, Any]]) -> int:
    """Delete docs by the stable ids."""
    if not keys:
        return 0
    if not HAS_LANCEDB:
        return 0
    tbl = _open_for_search(db_path)
    ids = [_stable_doc_id(k) for k in keys]
    try:
        filter_expr = "id in (" + ", ".join(f"'{id_}'" for id_ in ids) + ")"
        tbl.delete(filter_expr)
        return len(ids)
    except Exception:
        return 0


def _shape_hit(d: dict[str, Any]) -> dict[str, Any]:
    """Map a LanceDB dictionary row to the expected hit contract."""
    # LanceDB returns score as '_distance' for vector search, or '_score' for FTS/hybrid.
    score_raw = d.get("_score") or d.get("_distance") or 0.0
    try:
        score = float(score_raw)
    except (TypeError, ValueError):
        score = 0.0

    # If it was vector search, score is distance; convert to similarity score.
    if "_distance" in d and "_score" not in d:
        score = max(0.0, 1.0 - score)

    return {
        "doc_url": str(d.get("doc_url") or ""),
        "score": score,
        "snippet": str(d.get("body") or "").strip(),
        "para_index": int(d.get("para_index") or 0) if d.get("para_index") is not None else None,
        "content_hash": str(d.get("content_hash") or ""),
    }


def lancedb_knn_search(
    db_path: str,
    query_text: str,
    k: int,
    *,
    model_name: str,
    doc_url_filter: str | None = None,
    use_mmr: bool = True,
    rerank_model: str | None = None,
) -> dict[str, Any]:
    """Semantic vector search."""
    if not HAS_LANCEDB:
        return {"hits": [], "error": "lancedb not available in venv", "backend": "lancedb"}
    tbl = _open_for_search(db_path)
    qvec = _embed_texts(model_name, [query_text], normalize=True)
    if not qvec:
        return {"hits": [], "backend": "lancedb"}

    qb = tbl.search(qvec[0]).limit(int(k or 5))
    if doc_url_filter:
        qb = qb.where(f"doc_url = '{doc_url_filter}'")

    docs = qb.to_list()
    hits = [_shape_hit(d) for d in docs]
    return {"hits": hits, "backend": "lancedb"}


def lancedb_hybrid_search(
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
    """Hybrid FTS + vector search."""
    if not HAS_LANCEDB:
        return {"hits": [], "error": "lancedb not available in venv", "backend": "lancedb"}
    tbl = _open_for_search(db_path)

    # Note: LanceDB hybrid search requires schema with embeddings or query_type="hybrid".
    qb = tbl.search(query_text, query_type="hybrid").limit(int(k or 10))
    if doc_url_filter:
        qb = qb.where(f"doc_url = '{doc_url_filter}'")

    try:
        docs = qb.to_list()
    except Exception as exc:
        # Fall back to vector search if hybrid/FTS index is not ready or fails
        log.warning("LanceDB hybrid search failed, falling back to KNN: %s", exc)
        qvec = _embed_texts(model_name, [query_text], normalize=True)
        if not qvec:
            return {"hits": [], "backend": "lancedb"}
        qb = tbl.search(qvec[0]).limit(int(k or 10))
        if doc_url_filter:
            qb = qb.where(f"doc_url = '{doc_url_filter}'")
        docs = qb.to_list()

    hits = [_shape_hit(d) for d in docs]
    return {"hits": hits, "backend": "lancedb"}


def maintain_folder_lancedb(
    listing_root: str,
    embedding_model: str,
    *,
    mode: str = "auto",
    heartbeat_fn: Callable[[dict[str, Any]], None] | None = None,
    hb: Any | None = None,
) -> dict[str, Any]:
    """LanceDB specific folder maintain."""
    if not HAS_LANCEDB or lancedb is None:
        raise RuntimeError(
            "LanceDB backend selected but the 'lancedb' package is not importable in the configured Python venv."
        )

    from plugin.embeddings.embeddings_cache import ensure_corpus_meta, write_corpus_meta, lancedb_collection_path
    from plugin.embeddings.embeddings_fs import guess_indexable_paths, indexable_chunks_from_path
    from plugin.framework.constants import EMBEDDINGS_HEARTBEAT_INTERVAL_S

    root = str(listing_root or "").strip()
    if not root:
        raise ValueError("listing_root is required")

    coll_path = str(lancedb_collection_path(root, create_parent=True))
    meta_path = Path(root) / "writeragent_embeddings" / "corpus_meta.json"

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

    _hb.force({"phase": "start", "mode": "lancedb", "listing_root": root, "files": total})

    # Probe dim
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
        dim = 384

    ensure_corpus_meta(meta_path, embedding_model=embedding_model, dim=dim)
    write_corpus_meta(meta_path, storage_backend="lancedb")

    for idx, entry in enumerate(files):
        _hb.force({"phase": "extract", "file": entry.name, "index": idx, "total": total, "mode": "lancedb"})

        try:
            paragraph_count, chunks = indexable_chunks_from_path(entry.path, doc_url=entry.url, file_mtime=entry.modified)
        except Exception as e:
            log.debug("lancedb extract failed for %s: %s", entry.name, e)
            continue

        rows = [{"doc_url": c.doc_url, "para_index": c.para_index, "char_start": c.char_start, "char_end": c.char_end,
                 "content_hash": c.content_hash, "text": c.text, "file_mtime": c.file_mtime} for c in chunks]

        _hb.force(
            {
                "phase": "extract",
                "file": entry.name,
                "paragraphs": paragraph_count,
                "chunks": len(rows),
                "mode": "lancedb",
            }
        )

        if not rows:
            continue

        # For clean per-file refresh, delete previous docs for this doc_url.
        try:
            tbl = _open_for_search(coll_path)
            tbl.delete(f"doc_url = '{entry.url}'")
        except Exception:
            pass

        res = lancedb_ingest_rows(
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
                "mode": "lancedb",
            }
        )

    try:
        tbl = _open_for_search(coll_path)
        final_count = len(tbl)
    except Exception:
        final_count = indexed

    ensure_corpus_meta(meta_path, embedding_model=embedding_model, dim=dim, chunk_count=final_count)
    write_corpus_meta(meta_path, storage_backend="lancedb", updated_at=str(time.time()))

    _hb.force({"phase": "done", "mode": "lancedb", "indexed_paragraphs": indexed, "upserted": upserted_total})

    return {
        "mode": "lancedb",
        "indexed_paragraphs": indexed,
        "files": total,
        "upserted": upserted_total,
        "row_count": final_count,
        "storage_backend": "lancedb",
    }
