#!/usr/bin/env python3
# WriterAgent - offline folder search (embeddings default, or SQLite FTS5).
"""Search writeragent_embeddings for a document folder (no LibreOffice).

Embeddings (default) requires the same venv packages as index maintenance:
  pip install sentence-transformers numpy sqlite-vec langgraph langchain-core langchain-text-splitters envwrap odfpy

FTS mode (--fts) uses corpus.db FTS5; build via Settings in LO or maintain_folder_corpus.

Example:
  .venv/bin/python scripts/search_embeddings_folder.py "remote work policy"
  .venv/bin/python scripts/search_embeddings_folder.py --fts "web search"
  .venv/bin/python scripts/search_embeddings_folder.py --fts "grammar checker" --folder ~/Desktop/Writing --k 10
  .venv/bin/python scripts/search_embeddings_folder.py "Q4 revenue" --folder ~/Desktop/Writing --k 10
  .venv/bin/python scripts/search_embeddings_folder.py "topic" --json
  .venv/bin/python scripts/search_embeddings_folder.py "topic" --doc-url file:///path/to/doc.odt
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

project_root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(project_root))

from plugin.embeddings.embeddings_cache import (  # noqa: E402
    corpus_db_path,
    corpus_meta_path,
    index_is_empty,
    read_corpus_meta,
)
from plugin.embeddings.folder_fts_cache import fts_db_path, fts_index_is_empty, fts_meta_path  # noqa: E402
from plugin.embeddings.venv.embeddings_index import EMBEDDINGS_VENV_PIP_INSTALL, hybrid_search, knn_search  # noqa: E402
from plugin.embeddings.venv.embeddings_llama_index import llama_index_hybrid_search  # noqa: E402
from plugin.embeddings.venv.folder_fts import search_folder_fts  # noqa: E402

DEFAULT_FOLDER = Path("~/Desktop/Writing")
DEFAULT_K = 10
MAX_K = 50
DEFAULT_MODEL = "paraphrase-multilingual-MiniLM-L12-v2"
DEFAULT_NEAR_SLOP = 10


class SearchFolderError(Exception):
    """CLI search preflight or runtime failure."""


def _clamp_k(k: int) -> int:
    return max(1, min(int(k), MAX_K))


def _resolve_model(meta_path: Path, model_override: str | None) -> str:
    if model_override and model_override.strip():
        return model_override.strip()
    meta = read_corpus_meta(meta_path)
    model = (meta.get("embedding_model") or "").strip()
    return model or DEFAULT_MODEL


def search_folder(
    folder: Path,
    query: str,
    *,
    k: int = DEFAULT_K,
    model: str | None = None,
    doc_url: str | None = None,
    near_slop: int = DEFAULT_NEAR_SLOP,
    use_mmr: bool = True,
    rerank_model: str | None = None,
) -> dict[str, Any]:
    """Hybrid FTS + semantic search over an existing per-folder corpus.db cache."""
    listing_root = folder.expanduser().resolve()
    if not listing_root.is_dir():
        raise SearchFolderError(f"Not a directory: {listing_root}")

    query_text = str(query or "").strip()
    if not query_text:
        raise SearchFolderError("query is required")

    db_path = corpus_db_path(str(listing_root), create_parent=False)
    meta_path = corpus_meta_path(str(listing_root), create_parent=False)

    if not db_path.is_file() or index_is_empty(meta_path, db_path):
        raise SearchFolderError(
            f"No indexed embeddings cache under {listing_root / 'writeragent_embeddings'}. "
            f"Build one with: .venv/bin/python scripts/index_embeddings_folder.py {listing_root}"
        )

    model_name = _resolve_model(meta_path, model)
    k_clamped = _clamp_k(k)

    try:
        result = hybrid_search(
            str(db_path),
            query_text,
            k_clamped,
            model_name=model_name,
            near_slop=max(0, int(near_slop)),
            doc_url_filter=doc_url,
            use_mmr=use_mmr,
            rerank_model=rerank_model,
        )
    except ImportError as exc:
        raise SearchFolderError(
            f"Embeddings packages not available ({exc}). Install with: {EMBEDDINGS_VENV_PIP_INSTALL}"
        ) from exc

    hits = result.get("hits") or []
    return {
        "status": "ok",
        "backend": "hybrid",
        "folder": str(listing_root),
        "query": query_text,
        "k": k_clamped,
        "model": model_name,
        "hits": hits,
    }


def search_folder_vec(
    folder: Path,
    query: str,
    *,
    k: int = DEFAULT_K,
    model: str | None = None,
    doc_url: str | None = None,
) -> dict[str, Any]:
    """Semantic-only search (dev/debug)."""
    listing_root = folder.expanduser().resolve()
    if not listing_root.is_dir():
        raise SearchFolderError(f"Not a directory: {listing_root}")

    query_text = str(query or "").strip()
    if not query_text:
        raise SearchFolderError("query is required")

    db_path = corpus_db_path(str(listing_root), create_parent=False)
    meta_path = corpus_meta_path(str(listing_root), create_parent=False)

    if not db_path.is_file() or index_is_empty(meta_path, db_path):
        raise SearchFolderError(
            f"No indexed embeddings cache under {listing_root / 'writeragent_embeddings'}. "
            f"Build one with: .venv/bin/python scripts/index_embeddings_folder.py {listing_root}"
        )

    model_name = _resolve_model(meta_path, model)
    k_clamped = _clamp_k(k)

    try:
        result = knn_search(
            str(db_path),
            query_text,
            k_clamped,
            model_name=model_name,
            doc_url_filter=doc_url,
        )
    except ImportError as exc:
        raise SearchFolderError(
            f"Embeddings packages not available ({exc}). Install with: {EMBEDDINGS_VENV_PIP_INSTALL}"
        ) from exc

    hits = result.get("hits") or []
    return {
        "status": "ok",
        "backend": "embeddings",
        "folder": str(listing_root),
        "query": query_text,
        "k": k_clamped,
        "model": model_name,
        "hits": hits,
    }


def search_folder_fts_cli(
    folder: Path,
    query: str,
    *,
    k: int = DEFAULT_K,
    near_slop: int = DEFAULT_NEAR_SLOP,
) -> dict[str, Any]:
    """Lexical FTS5 search over corpus.db passages in writeragent_embeddings/."""
    listing_root = folder.expanduser().resolve()
    if not listing_root.is_dir():
        raise SearchFolderError(f"Not a directory: {listing_root}")

    query_text = str(query or "").strip()
    if not query_text:
        raise SearchFolderError("query is required")

    db_path = fts_db_path(str(listing_root), create_parent=False)
    meta_path = fts_meta_path(str(listing_root), create_parent=False)

    if fts_index_is_empty(meta_path, db_path):
        raise SearchFolderError(
            f"No FTS index under {listing_root / 'writeragent_embeddings'}. "
            f"Enable folder FTS in WriterAgent or build with: "
            f".venv/bin/python -c \"from plugin.embeddings.venv.folder_fts import maintain_folder_fts; "
            f"maintain_folder_fts({listing_root!r})\""
        )

    k_clamped = _clamp_k(k)
    result = search_folder_fts(
        str(db_path),
        query_text,
        k=k_clamped,
        near_slop=max(0, int(near_slop)),
    )
    hits = result.get("hits") or []
    return {
        "status": "ok",
        "backend": "fts",
        "folder": str(listing_root),
        "query": query_text,
        "k": k_clamped,
        "near_slop": max(0, int(near_slop)),
        "match": result.get("match"),
        "hits": hits,
    }


def format_hits(result: dict[str, Any]) -> str:
    """Human-readable hit listing."""
    lines: list[str] = []
    folder = result.get("folder", "?")
    query = result.get("query", "?")
    hits = result.get("hits") or []

    lines.append(f"Folder: {folder}")
    lines.append(f"Query:  {query!r}")
    backend = str(result.get("backend") or "hybrid")
    if backend == "fts":
        lines.append("Backend: FTS (SQLite)")
        match = result.get("match")
        if match:
            lines.append(f"Match:  {match}")
    elif backend == "embeddings":
        lines.append("Backend: embeddings (vector only)")
        lines.append(f"Model:  {result.get('model', '?')}")
    else:
        lines.append("Backend: hybrid (FTS + embeddings, RRF)")
        lines.append(f"Model:  {result.get('model', '?')}")
    lines.append(f"Hits:   {len(hits)}")
    lines.append("=" * 72)

    if not hits:
        lines.append("(no matches)")
        return "\n".join(lines)

    for rank, hit in enumerate(hits, start=1):
        doc_url = hit.get("doc_url", "?")
        para_index = hit.get("para_index", "?")
        score = float(hit.get("score") or 0.0)
        snippet = str(hit.get("snippet") or "")
        lines.append(f"#{rank}  score={score:.4f}  para={para_index}")
        lines.append(f"     doc={doc_url}")
        lines.append(f"     {snippet}")
        lines.append("-" * 72)

    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Search a WriterAgent per-folder cache (hybrid FTS+embeddings default; --fts or --vec for single leg)",
    )
    parser.add_argument(
        "--fts",
        action="store_true",
        help="FTS keyword leg only (debug)",
    )
    parser.add_argument(
        "--vec",
        action="store_true",
        help="Vector semantic leg only (debug)",
    )
    parser.add_argument(
        "--backend",
        choices=("hybrid", "llama_index"),
        default="hybrid",
        help="Backend to use for hybrid search (default hybrid).",
    )
    parser.add_argument("query", help="Natural-language or keyword query")
    parser.add_argument(
        "--folder",
        type=Path,
        default=DEFAULT_FOLDER,
        help=f"Document directory (default: {DEFAULT_FOLDER})",
    )
    parser.add_argument(
        "--k",
        type=int,
        default=DEFAULT_K,
        help=f"Maximum hits to return (default: {DEFAULT_K}, max: {MAX_K})",
    )
    parser.add_argument(
        "--near-slop",
        type=int,
        default=DEFAULT_NEAR_SLOP,
        help=f"FTS NEAR token gap (--fts only; default: {DEFAULT_NEAR_SLOP})",
    )
    parser.add_argument(
        "--model",
        help=f"SentenceTransformer model id (embeddings only; default: corpus_meta.json or {DEFAULT_MODEL})",
    )
    parser.add_argument(
        "--doc-url",
        help="Only return hits from this doc_url (embeddings only; exact file:///… match)",
    )
    parser.add_argument(
        "--no-mmr",
        action="store_true",
        help="Hybrid/LlamaIndex backend: disable cross-encoder rerank (RRF-only)",
    )
    parser.add_argument(
        "--rerank-model",
        help="Hybrid/LlamaIndex backend: HuggingFace cross-encoder model id (default: cross-encoder/ms-marco-MiniLM-L-6-v2 when rerank on)",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Print JSON result (hits list) for scripting",
    )
    args = parser.parse_args(argv)

    try:
        if args.fts:
            result = search_folder_fts_cli(
                args.folder,
                args.query,
                k=args.k,
                near_slop=args.near_slop,
            )
        elif args.vec:
            result = search_folder_vec(
                args.folder,
                args.query,
                k=args.k,
                model=args.model,
                doc_url=args.doc_url,
            )
        else:
            use_mmr = not args.no_mmr
            rerank_model = None
            if use_mmr:
                rerank_model = (args.rerank_model or "").strip() or "cross-encoder/ms-marco-MiniLM-L-6-v2"
            if args.backend == "llama_index":
                result = llama_index_hybrid_search(
                    str(corpus_db_path(str(args.folder.expanduser().resolve()), create_parent=False)),
                    args.query,
                    args.k,
                    model_name=args.model,
                    near_slop=args.near_slop,
                    doc_url_filter=args.doc_url,
                    use_mmr=use_mmr,
                    rerank_model=rerank_model,
                )
                result = {
                    "status": "ok",
                    "backend": "llama_index",
                    "folder": str(args.folder.expanduser().resolve()),
                    "query": args.query,
                    "k": _clamp_k(args.k),
                    "model": args.model,
                    "hits": result.get("hits") or [],
                }
            else:
                result = search_folder(
                    args.folder,
                    args.query,
                    k=args.k,
                    model=args.model,
                    doc_url=args.doc_url,
                    near_slop=args.near_slop,
                    use_mmr=use_mmr,
                    rerank_model=rerank_model,
                )
    except SearchFolderError as exc:
        print(str(exc), file=sys.stderr)
        return 2 if "query is required" in str(exc) else 1

    if args.json:
        print(json.dumps(result, indent=2, ensure_ascii=False))
    else:
        print(format_hits(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
