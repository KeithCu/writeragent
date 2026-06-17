#!/usr/bin/env python3
"""Inspect WriterAgent per-folder embeddings cache (schema v3).

Reads ``corpus_meta.json`` and ``corpus.db`` (chunks + FTS5 passages + vec0 +
indexed_files / indexed_paragraphs). Accepts a document folder or the cache
directory itself:

  python scripts/dump_embeddings_cache.py ~/Desktop/Writing
  python scripts/dump_embeddings_cache.py ~/Desktop/Writing/writeragent_embeddings
  python scripts/dump_embeddings_cache.py --limit 20 --doc-url file:///path/to/doc.odt
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
    CORPUS_DB_FILENAME,
    CORPUS_META_FILENAME,
    EMBEDDINGS_CACHE_DIRNAME,
    LEGACY_FILE_INDEX_STATE_FILENAME,
    LEGACY_INDEX_DB,
    folder_corpus_key,
    read_corpus_meta,
)


def resolve_cache_paths(path: Path) -> tuple[Path, Path | None, str | None]:
    """Return (cache_dir, listing_root, folder_key).

    *listing_root* is None for legacy profile caches keyed only by hash.
    """
    path = path.expanduser().resolve()
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


def _sqlite_table_names(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    return {str(row[0]) for row in rows}


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
) -> int:
    cache_dir, listing_root, folder_key = resolve_cache_paths(path)
    corpus_db = cache_dir / CORPUS_DB_FILENAME
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
    else:
        print(f"Corpus DB: {corpus_db} (missing — index not built yet)")

    if summary_only or limit == 0:
        return 0

    if not folder_key:
        print("Cannot list corpus entries without a collection key.", file=sys.stderr)
        return 1

    entries = _load_entries_from_corpus_db(corpus_db, doc_url_filter=doc_url, limit=limit)
    source = "corpus.db"
    if doc_url is None and limit > 0 and entries:
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
    args = parser.parse_args(argv)

    try:
        return dump_cache(
            Path(args.directory),
            preview_chars=max(1, args.preview_chars),
            limit=0 if args.summary_only else max(0, args.limit),
            doc_url=args.doc_url,
            summary_only=args.summary_only,
        )
    except FileNotFoundError as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
