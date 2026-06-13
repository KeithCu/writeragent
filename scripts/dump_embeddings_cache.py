#!/usr/bin/env python3
"""Inspect WriterAgent per-folder embeddings cache (schema v3 primary).

Reads ``corpus_meta.json`` and ``corpus.db`` (chunks + FTS5 passages + vec0 +
indexed_files / indexed_paragraphs). Accepts a document folder or the cache
directory itself:

  python scripts/dump_embeddings_cache.py ~/Desktop/Writing
  python scripts/dump_embeddings_cache.py ~/Desktop/Writing/writeragent_embeddings
  python scripts/dump_embeddings_cache.py --limit 20 --doc-url file:///path/to/doc.odt

Legacy pre-v3 caches used a separate ``chroma/`` directory. Pass ``--chromadb``
only when inspecting those old caches via the chromadb Python API (not needed
for schema v3).
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

project_root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(project_root))

from plugin.embeddings.embeddings_cache import (  # noqa: E402
    CHROMA_SUBDIR,
    CORPUS_DB_FILENAME,
    CORPUS_META_FILENAME,
    EMBEDDINGS_CACHE_DIRNAME,
    LEGACY_FILE_INDEX_STATE_FILENAME,
    LEGACY_INDEX_DB,
    folder_corpus_key,
    read_corpus_meta,
)

_METADATA_TEXT_KEY = "chroma:document"


def resolve_cache_paths(path: Path) -> tuple[Path, Path | None, str | None]:
    """Return (cache_dir, listing_root, folder_key).

    *listing_root* is None for legacy profile caches keyed only by hash.
    """
    path = path.expanduser().resolve()
    if path.name == CHROMA_SUBDIR and path.parent.name == EMBEDDINGS_CACHE_DIRNAME:
        cache_dir = path.parent
        listing_root = cache_dir.parent
        return cache_dir, listing_root, folder_corpus_key(str(listing_root))
    if path.name == EMBEDDINGS_CACHE_DIRNAME:
        listing_root = path.parent
        return path, listing_root, folder_corpus_key(str(listing_root))
    nested = path / EMBEDDINGS_CACHE_DIRNAME
    if nested.is_dir() or (path / CORPUS_META_FILENAME).is_file():
        return nested if nested.is_dir() else path, path, folder_corpus_key(str(path))
    if path.is_dir() and _looks_like_legacy_profile_hash_dir(path):
        return path, None, path.name
    raise FileNotFoundError(
        f"No {EMBEDDINGS_CACHE_DIRNAME}/ cache under {path} "
        f"(expected {path / EMBEDDINGS_CACHE_DIRNAME} or pass the cache dir directly)"
    )


def _looks_like_legacy_profile_hash_dir(path: Path) -> bool:
    if path.parent.name != EMBEDDINGS_CACHE_DIRNAME:
        return False
    name = path.name
    return len(name) == 64 and all(c in "0123456789abcdef" for c in name)


def _fmt_ts(raw: str | float | int | None) -> str:
    if raw in (None, ""):
        return "?"
    try:
        ts = float(raw)
    except (TypeError, ValueError):
        return str(raw)
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")


def _preview(text: str, limit: int) -> str:
    one_line = " ".join(str(text or "").split())
    if len(one_line) <= limit:
        return one_line
    return one_line[: max(1, limit - 1)] + "…"


def _print_corpus_meta(meta_path: Path) -> None:
    print("corpus_meta.json")
    if not meta_path.is_file():
        print("  (missing)")
        return
    meta = read_corpus_meta(meta_path)
    if not meta:
        print("  (empty or unreadable)")
        return
    for key in sorted(meta):
        value = meta[key]
        if key == "updated_at":
            print(f"  {key}: {value} ({_fmt_ts(value)})")
        else:
            print(f"  {key}: {value}")
    chunk_count = meta.get("chunk_count")
    if chunk_count in (None, "", "0"):
        print("  note: chunk_count missing/zero — index may still be building or cold build failed")


def _print_file_index_state(corpus_db: Path) -> None:
    print("indexed_files / indexed_paragraphs (corpus.db)")
    if not corpus_db.is_file():
        print("  (missing corpus.db)")
        return
    try:
        conn = sqlite3.connect(f"file:{corpus_db}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        tables = {
            row[0]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        }
        if "indexed_files" not in tables:
            print("  (no indexed_files table — delete writeragent_embeddings/ and rebuild)")
            conn.close()
            return
        files = conn.execute(
            "SELECT doc_url, file_mtime, last_indexed_at FROM indexed_files ORDER BY doc_url"
        ).fetchall()
        if not files:
            print("  (no indexed files recorded)")
            conn.close()
            return
        print(f"  files: {len(files)}")
        for row in files:
            doc_url = str(row["doc_url"] or "")
            para_row = conn.execute(
                "SELECT COUNT(*) AS c FROM indexed_paragraphs WHERE doc_url = ?",
                (doc_url,),
            ).fetchone()
            para_count = int(para_row["c"] if para_row else 0)
            print(
                f"  - {doc_url}\n"
                f"      paragraphs={para_count}  file_mtime={_fmt_ts(row['file_mtime'])}"
                f"  last_indexed={_fmt_ts(row['last_indexed_at'])}"
            )
        conn.close()
    except sqlite3.Error as exc:
        print(f"  error: {exc}")


def _chroma_sqlite_path(cache_dir: Path) -> Path:
    return cache_dir / CHROMA_SUBDIR / "chroma.sqlite3"


def _sqlite_table_names(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    return {str(row[0]) for row in rows}


def _sqlite_chroma_summary(sqlite_path: Path) -> dict[str, Any]:
    summary: dict[str, Any] = {"sqlite_path": str(sqlite_path), "collections": [], "embedding_count": 0}
    if not sqlite_path.is_file():
        summary["missing"] = True
        return summary

    conn = sqlite3.connect(f"file:{sqlite_path}?mode=ro", uri=True)
    try:
        tables = _sqlite_table_names(conn)
        summary["tables"] = sorted(tables)
        if "collections" not in tables:
            summary["error"] = "No collections table (not a Chroma DB?)"
            return summary

        collections = conn.execute("SELECT id, name, dimension FROM collections ORDER BY name").fetchall()
        summary["collections"] = [
            {"id": row[0], "name": row[1], "dimension": row[2]} for row in collections
        ]
        if "embeddings" in tables:
            summary["embedding_count"] = int(conn.execute("SELECT COUNT(*) FROM embeddings").fetchone()[0])
        if "embedding_metadata" in tables:
            summary["metadata_rows"] = int(
                conn.execute("SELECT COUNT(*) FROM embedding_metadata").fetchone()[0]
            )
    finally:
        conn.close()
    return summary


def _print_chroma_sqlite_summary(cache_dir: Path) -> None:
    sqlite_path = _chroma_sqlite_path(cache_dir)
    print("Chroma SQLite")
    print(f"  path: {sqlite_path}")
    summary = _sqlite_chroma_summary(sqlite_path)
    if summary.get("missing"):
        print("  (chroma.sqlite3 not found — index not built yet)")
        return
    if summary.get("error"):
        print(f"  error: {summary['error']}")
        return
    print(f"  embeddings: {summary.get('embedding_count', 0)}")
    print(f"  metadata rows: {summary.get('metadata_rows', '?')}")
    collections = summary.get("collections") or []
    if collections:
        print("  collections:")
        for col in collections:
            dim = col.get("dimension")
            dim_s = dim if dim is not None else "?"
            print(f"    - {col.get('name')}  id={col.get('id')}  dim={dim_s}")
    else:
        print("  collections: (none)")


def _load_entries_from_sqlite(
    sqlite_path: Path,
    *,
    doc_url_filter: str | None,
    limit: int,
) -> list[dict[str, Any]]:
    if not sqlite_path.is_file():
        return []

    conn = sqlite3.connect(f"file:{sqlite_path}?mode=ro", uri=True)
    try:
        tables = _sqlite_table_names(conn)
        if "embeddings" not in tables or "embedding_metadata" not in tables:
            return []

        rows = conn.execute(
            """
            SELECT e.id, e.embedding_id, m.key,
                   m.string_value, m.int_value, m.float_value
            FROM embeddings e
            JOIN embedding_metadata m ON m.id = e.id
            ORDER BY e.id, m.key
            """
        ).fetchall()

        by_embedding: dict[int, dict[str, Any]] = {}
        for row_id, embedding_id, key, string_value, int_value, float_value in rows:
            entry = by_embedding.setdefault(
                int(row_id),
                {"embedding_id": str(embedding_id), "metadata": {}},
            )
            if key == _METADATA_TEXT_KEY:
                entry["document"] = string_value or ""
                continue
            if string_value is not None:
                entry["metadata"][str(key)] = string_value
            elif int_value is not None:
                entry["metadata"][str(key)] = int(int_value)
            elif float_value is not None:
                entry["metadata"][str(key)] = float(float_value)

        entries = list(by_embedding.values())
        if doc_url_filter:
            entries = [
                e
                for e in entries
                if str((e.get("metadata") or {}).get("doc_url") or "") == doc_url_filter
            ]
        entries.sort(
            key=lambda e: (
                str((e.get("metadata") or {}).get("doc_url") or ""),
                int((e.get("metadata") or {}).get("para_index") or 0),
                int((e.get("metadata") or {}).get("chunk_index") or 0),
            )
        )
        return entries[: max(0, limit)]
    finally:
        conn.close()


def _load_entries_from_corpus_db(
    corpus_db: Path,
    *,
    doc_url_filter: str | None,
    limit: int,
) -> list[dict[str, Any]]:
    if not corpus_db.is_file():
        return []
    conn = sqlite3.connect(f"file:{corpus_db}?mode=ro", uri=True)
    try:
        tables = _sqlite_table_names(conn)
        if "chunks" not in tables:
            return []
        sql = """
            SELECT chunk_id, doc_url, para_index, char_start, char_end, content_hash, body
            FROM chunks
        """
        params: list[Any] = []
        if doc_url_filter:
            sql += " WHERE doc_url = ?"
            params.append(doc_url_filter)
        sql += " ORDER BY doc_url, para_index, char_start"
        if limit > 0:
            sql += " LIMIT ?"
            params.append(limit)
        rows = conn.execute(sql, params).fetchall()
        entries: list[dict[str, Any]] = []
        for chunk_id, doc_url, para_index, char_start, char_end, content_hash, body in rows:
            entries.append(
                {
                    "embedding_id": str(chunk_id),
                    "metadata": {
                        "doc_url": doc_url,
                        "para_index": int(para_index or 0),
                        "char_start": int(char_start or 0),
                        "char_end": int(char_end or 0),
                        "content_hash": str(content_hash or ""),
                    },
                    "document": str(body or ""),
                }
            )
        return entries
    finally:
        conn.close()


def _load_entries_from_chromadb(
    chroma_dir: Path,
    collection_name: str,
    *,
    doc_url_filter: str | None,
    limit: int,
) -> list[dict[str, Any]]:
    try:
        import chromadb
    except ImportError as exc:
        raise ImportError("chromadb not installed") from exc

    client = chromadb.PersistentClient(path=str(chroma_dir))
    collection = client.get_collection(name=collection_name)
    where = {"doc_url": doc_url_filter} if doc_url_filter else None
    data = collection.get(
        where=where,
        include=["metadatas", "documents"],
        limit=limit if limit > 0 else None,
    )
    entries: list[dict[str, Any]] = []
    ids = list(data.get("ids") or [])
    metas = list(data.get("metadatas") or [])
    docs = list(data.get("documents") or [])
    for idx, embedding_id in enumerate(ids):
        meta = metas[idx] if idx < len(metas) else {}
        doc = docs[idx] if idx < len(docs) else ""
        entries.append(
            {
                "embedding_id": embedding_id,
                "metadata": meta or {},
                "document": doc or "",
            }
        )
    entries.sort(
        key=lambda e: (
            str((e.get("metadata") or {}).get("doc_url") or ""),
            int((e.get("metadata") or {}).get("para_index") or 0),
            int((e.get("metadata") or {}).get("chunk_index") or 0),
        )
    )
    return entries


def _print_entries(
    entries: list[dict[str, Any]],
    *,
    preview_chars: int,
    source: str,
) -> None:
    print(f"Indexed chunks ({source}): showing {len(entries)}")
    if not entries:
        return
    print("-" * 72)
    for entry in entries:
        meta = entry.get("metadata") or {}
        doc_url = meta.get("doc_url", "?")
        para_index = meta.get("para_index", "?")
        char_start = meta.get("char_start", "?")
        char_end = meta.get("char_end", "?")
        chunk_index = meta.get("chunk_index")
        content_hash = meta.get("content_hash", "")
        hash_preview = f"{content_hash[:12]}…" if content_hash else "?"
        chunk_bits = f" chunk={chunk_index}" if chunk_index is not None else ""
        print(
            f"id={entry.get('embedding_id')}  doc={doc_url}\n"
            f"  para={para_index}  chars={char_start}-{char_end}{chunk_bits}  hash={hash_preview}"
        )
        print(f"  text: {_preview(str(entry.get('document') or ''), preview_chars)}")
        print("-" * 72)


def _print_doc_url_counts(entries: list[dict[str, Any]]) -> None:
    counts: dict[str, int] = defaultdict(int)
    for entry in entries:
        doc_url = str((entry.get("metadata") or {}).get("doc_url") or "(unknown)")
        counts[doc_url] += 1
    if not counts:
        return
    print("Chunks by doc_url:")
    for doc_url, count in sorted(counts.items(), key=lambda item: (-item[1], item[0])):
        print(f"  {count:4d}  {doc_url}")


def dump_cache(
    path: Path,
    *,
    preview_chars: int,
    limit: int,
    doc_url: str | None,
    summary_only: bool,
    prefer_chromadb: bool,
) -> int:
    cache_dir, listing_root, folder_key = resolve_cache_paths(path)
    corpus_db = cache_dir / CORPUS_DB_FILENAME
    chroma_dir = cache_dir / CHROMA_SUBDIR
    meta_path = cache_dir / CORPUS_META_FILENAME
    legacy_state = cache_dir / LEGACY_FILE_INDEX_STATE_FILENAME
    legacy_db = cache_dir / LEGACY_INDEX_DB

    print(f"Cache directory: {cache_dir}")
    if listing_root is not None:
        print(f"Document folder: {listing_root}")
        expected_key = folder_corpus_key(str(listing_root))
        print(f"Collection key:  {expected_key}")
        if folder_key and folder_key != expected_key:
            print(f"  warning: path key {folder_key} != recomputed {expected_key}")
    else:
        print("Document folder: (legacy profile cache — folder path unknown)")
        print(f"Collection key:  {folder_key}")

    if legacy_db.is_file():
        print(f"Legacy index.db: {legacy_db} (pre-v3; safe to delete after rebuild)")
    if legacy_state.is_file():
        print(f"Legacy file_index_state.json: {legacy_state} (removed; delete and rebuild)")

    print("=" * 72)
    _print_corpus_meta(meta_path)
    print("-" * 72)
    _print_file_index_state(corpus_db)
    print("-" * 72)
    if corpus_db.is_file():
        print(f"Corpus DB: {corpus_db}")
        try:
            count = sqlite3.connect(f"file:{corpus_db}?mode=ro", uri=True).execute("SELECT COUNT(*) FROM chunks").fetchone()
            print(f"  chunks: {count[0] if count else 0}")
        except sqlite3.Error as exc:
            print(f"  error: {exc}")
    elif chroma_dir.is_dir():
        _print_chroma_sqlite_summary(cache_dir)

    if summary_only or limit == 0:
        return 0

    if not folder_key:
        print("Cannot list Chroma entries without a collection key.", file=sys.stderr)
        return 1

    entries = _load_entries_from_corpus_db(corpus_db, doc_url_filter=doc_url, limit=limit)
    source = "corpus.db"
    if not entries and chroma_dir.is_dir():
        sqlite_path = _chroma_sqlite_path(cache_dir)
        if prefer_chromadb:
            try:
                entries = _load_entries_from_chromadb(
                    chroma_dir,
                    folder_key or "",
                    doc_url_filter=doc_url,
                    limit=limit,
                )
                source = "chromadb"
            except Exception as exc:
                print(f"chromadb read failed ({exc}); falling back to legacy chroma sqlite", file=sys.stderr)
        if not entries:
            all_entries = _load_entries_from_sqlite(
                sqlite_path,
                doc_url_filter=doc_url,
                limit=10_000_000,
            )
            if doc_url is None and limit > 0 and all_entries:
                _print_doc_url_counts(all_entries)
                print("-" * 72)
            entries = all_entries[:limit]
            source = "chroma.sqlite3"
    elif doc_url is None and limit > 0 and entries:
        _print_doc_url_counts(entries)
        print("-" * 72)

    print("-" * 72)
    _print_entries(entries, preview_chars=preview_chars, source=source)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Dump WriterAgent embeddings cache for a folder.")
    parser.add_argument(
        "directory",
        help="Document folder or writeragent_embeddings cache directory",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=50,
        help="Max chunks to print (default: 50; 0 = summary only)",
    )
    parser.add_argument(
        "--preview-chars",
        type=int,
        default=120,
        help="Characters of chunk text to print (default: 120)",
    )
    parser.add_argument(
        "--doc-url",
        help="Only show chunks for this doc_url (exact match, e.g. file:///home/you/doc.odt)",
    )
    parser.add_argument(
        "--summary-only",
        action="store_true",
        help="Print JSON/SQLite summary only (same as --limit 0)",
    )
    parser.add_argument(
        "--chromadb",
        action="store_true",
        help="Legacy pre-v3 only: read chroma/ via chromadb Python API (needs chromadb in venv)",
    )
    args = parser.parse_args(argv)

    try:
        return dump_cache(
            Path(args.directory),
            preview_chars=max(1, args.preview_chars),
            limit=0 if args.summary_only else max(0, args.limit),
            doc_url=args.doc_url,
            summary_only=args.summary_only,
            prefer_chromadb=args.chromadb,
        )
    except FileNotFoundError as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
