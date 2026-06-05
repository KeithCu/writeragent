#!/usr/bin/env python3
"""Dump web research cache entries (kind=research) from writeragent_web_cache.db.

Run from repo root:
  python scripts/dump_web_research_cache.py
  python scripts/dump_web_research_cache.py --db ~/.config/libreoffice/4/user/config/writeragent_web_cache.db
  python scripts/dump_web_research_cache.py --preview-chars 200 --all-kinds
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path


def _libreoffice_user_dirs() -> list[Path]:
    home = Path.home()
    candidates = [
        home / ".config/libreoffice/4/user",
        home / ".config/libreoffice/24/user",
        home / "Library/Application Support/LibreOffice/4/user",
    ]
    appdata = os.environ.get("APPDATA")
    if appdata:
        candidates.append(Path(appdata) / "LibreOffice/4/user")
    out: list[Path] = []
    seen: set[Path] = set()
    for path in candidates:
        if path.is_dir() and path not in seen:
            seen.add(path)
            out.append(path)
    return out


def _cache_db_path(user_dir: Path) -> Path:
    """WriterAgent cache lives next to writeragent.json under user/config/."""
    return user_dir / "config" / "writeragent_web_cache.db"


def resolve_cache_db(explicit: str | None) -> Path | None:
    if explicit:
        path = Path(explicit).expanduser()
        return path if path.is_file() else None
    for user_dir in _libreoffice_user_dirs():
        db = _cache_db_path(user_dir)
        if db.is_file():
            return db
    return None


def dump_cache(db_path: Path, *, kind: str | None, preview_chars: int) -> int:
    conn = sqlite3.connect(str(db_path))
    try:
        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='web_cache'"
        ).fetchone()
        if not row:
            print(f"No web_cache table in {db_path}", file=sys.stderr)
            return 1

        if kind:
            rows = conn.execute(
                "SELECT kind, key, value, created_at FROM web_cache WHERE kind = ? ORDER BY created_at DESC",
                (kind,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT kind, key, value, created_at FROM web_cache ORDER BY kind, created_at DESC"
            ).fetchall()

        if not rows:
            label = kind or "any kind"
            print(f"No cache rows ({label}) in {db_path}")
            return 0

        now = time.time()
        print(f"Database: {db_path}")
        print(f"Entries: {len(rows)}")
        print("-" * 72)
        for entry_kind, key, value, created_at in rows:
            preview = (value or "").replace("\n", " ").strip()
            if len(preview) > preview_chars:
                preview = preview[:preview_chars] + "…"
            age_days = (now - float(created_at)) / 86400.0 if created_at else 0.0
            created_str = datetime.fromtimestamp(float(created_at)).strftime("%Y-%m-%d %H:%M") if created_at else "?"
            print(f"kind: {entry_kind}")
            print(f"key: {key}")
            print(f"created: {created_str} ({age_days:.1f} days ago)")
            print(f"preview: {preview}")
            print("-" * 72)
        return 0
    finally:
        conn.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Dump WriterAgent web cache entries.")
    parser.add_argument(
        "--db",
        help="Path to writeragent_web_cache.db (default: auto-detect under LibreOffice user profile)",
    )
    parser.add_argument(
        "--preview-chars",
        type=int,
        default=100,
        help="Characters of each cached value to print (default: 100)",
    )
    parser.add_argument(
        "--all-kinds",
        action="store_true",
        help="List every cache kind (search, page, research, …); default is research only",
    )
    args = parser.parse_args(argv)

    db_path = resolve_cache_db(args.db)
    if db_path is None:
        if args.db:
            print(f"Cache database not found: {args.db}", file=sys.stderr)
        else:
            searched = ", ".join(str(_cache_db_path(d)) for d in _libreoffice_user_dirs())
            print(f"Cache database not found. Checked: {searched}", file=sys.stderr)
        return 1

    kind = None if args.all_kinds else "research"
    return dump_cache(db_path, kind=kind, preview_chars=max(1, args.preview_chars))


if __name__ == "__main__":
    raise SystemExit(main())
