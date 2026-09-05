# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Per-folder ODS cache for DuckDB sibling XLSX/XLS ingress.

Amortizes LibreOffice's Excel import across chat re-queries. Layout mirrors
``writeragent_embeddings/`` (see ``docs/calc/duckdb-dev-plan.md`` § ODS cache
directory):

    <scoped_dir>/writeragent_ods_cache/<sha256(path+mtime+size)>.ods
    <scoped_dir>/writeragent_ods_cache/<sha256(path+mtime+size)>.meta.json

Native ``.ods`` and the live active workbook are never cached — those open
the source (or the already-loaded model) directly.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from pathlib import Path
from typing import Any

log = logging.getLogger("writeragent.calc.duckdb")

ODS_CACHE_DIRNAME = "writeragent_ods_cache"
CACHE_FORMAT_VERSION = 1
CACHEABLE_EXTENSIONS = frozenset({".xlsx", ".xls"})
# Schema key lives on the calc module (module.yaml). Plan name was duckdb.ods_cache_enabled.
ODS_CACHE_ENABLED_KEY = "calc.ods_cache_enabled"


def normalize_source_path(source_path: str) -> str:
    """Absolute, normalized filesystem path used in the cache key."""
    return os.path.normpath(os.path.abspath(source_path))


def is_cacheable_office_source(source_path: str) -> bool:
    """True for sibling Excel files. Native ``.ods`` is never cached."""
    return os.path.splitext(source_path)[1].lower() in CACHEABLE_EXTENSIONS


def source_stat(source_path: str) -> tuple[str, int, int]:
    """Return ``(abs_path, mtime_ns, size)`` for the cache key."""
    abs_path = normalize_source_path(source_path)
    st = os.stat(abs_path)
    mtime_ns = int(getattr(st, "st_mtime_ns", int(st.st_mtime * 1_000_000_000)))
    return abs_path, mtime_ns, int(st.st_size)


def cache_key(abs_path: str, mtime_ns: int, size: int) -> str:
    """Hash of absolute path + mtime + size (content hash only if mtime is flaky)."""
    payload = f"{abs_path}\n{mtime_ns}\n{size}".encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def ods_cache_dir(source_path: str, *, create: bool = False) -> Path:
    """``writeragent_ods_cache/`` beside the scoped folder that holds *source_path*."""
    root = Path(normalize_source_path(source_path)).parent / ODS_CACHE_DIRNAME
    if create:
        root.mkdir(parents=True, exist_ok=True)
    return root


def cache_entry_paths(source_path: str) -> tuple[Path, Path] | None:
    """``(cached.ods, sidecar.meta.json)`` for the current source stat, or None."""
    try:
        abs_path, mtime_ns, size = source_stat(source_path)
    except OSError:
        return None
    key = cache_key(abs_path, mtime_ns, size)
    folder = ods_cache_dir(source_path, create=False)
    return folder / f"{key}.ods", folder / f"{key}.meta.json"


def read_sidecar_meta(meta_path: Path) -> dict[str, Any] | None:
    """Load sidecar JSON; missing or corrupt meta is a cache miss."""
    if not meta_path.is_file():
        return None
    try:
        data = json.loads(meta_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        log.debug("ods cache meta unreadable: %s", meta_path, exc_info=True)
        return None
    return data if isinstance(data, dict) else None


def meta_matches_source(meta: dict[str, Any], abs_path: str, mtime_ns: int, size: int) -> bool:
    """True when sidecar still describes this source and format version."""
    try:
        version = int(meta.get("cache_format_version", -1))
        stored_mtime = int(meta.get("mtime_ns", -1))
        stored_size = int(meta.get("size", -1))
    except (TypeError, ValueError):
        return False
    if version != CACHE_FORMAT_VERSION:
        return False
    if str(meta.get("source_path") or "") != abs_path:
        return False
    return stored_mtime == mtime_ns and stored_size == size


def lookup_cached_ods(source_path: str) -> Path | None:
    """Return the cached ODS path on hit; None on miss or invalidation.

    Invalidate when the source mtime/size changes (new key), the sidecar is
    missing, or ``cache_format_version`` does not match.
    """
    if not is_cacheable_office_source(source_path):
        return None
    paths = cache_entry_paths(source_path)
    if paths is None:
        return None
    ods_path, meta_path = paths
    if not ods_path.is_file() or not meta_path.is_file():
        return None
    meta = read_sidecar_meta(meta_path)
    if meta is None:
        return None
    try:
        abs_path, mtime_ns, size = source_stat(source_path)
    except OSError:
        return None
    if not meta_matches_source(meta, abs_path, mtime_ns, size):
        return None
    return ods_path


def write_sidecar_meta(meta_path: Path, source_path: str) -> None:
    """Write source path / mtime / size / format version next to the cached ODS."""
    abs_path, mtime_ns, size = source_stat(source_path)
    payload = {
        "cache_format_version": CACHE_FORMAT_VERSION,
        "source_path": abs_path,
        "mtime_ns": mtime_ns,
        "size": size,
    }
    meta_path.parent.mkdir(parents=True, exist_ok=True)
    meta_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def ods_cache_enabled() -> bool:
    """Settings ``calc.ods_cache_enabled`` (default true). Missing schema → enabled."""
    try:
        from plugin.framework.config import get_config
        from plugin.framework.config_schema import as_bool

        return as_bool(get_config(ODS_CACHE_ENABLED_KEY))
    except Exception:
        return True
