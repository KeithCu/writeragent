# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
"""Per-folder corpus cache paths, corpus_meta.json state, and legacy store cleanup."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from plugin.framework.constants import EMBEDDINGS_SCHEMA_VERSION as SCHEMA_VERSION

log = logging.getLogger(__name__)

EMBEDDINGS_CACHE_DIRNAME = "writeragent_embeddings"
STORAGE_BACKEND = "sqlite_vec"
CORPUS_META_FILENAME = "corpus_meta.json"
LEGACY_FILE_INDEX_STATE_FILENAME = "file_index_state.json"
CORPUS_DB_FILENAME = "corpus.db"
LEGACY_INDEX_DB = "index.db"
LEGACY_FTS_DB = "fts5.db"
LEGACY_FTS_META = "fts_meta.json"


def folder_corpus_key(directory_path: str) -> str:
    """Stable cache key for a normalized directory path."""
    norm = os.path.normpath(os.path.abspath(directory_path))
    return hashlib.sha256(norm.encode("utf-8")).hexdigest()


def resolve_folder_for_active_doc(ctx: Any, model: Any) -> str | None:
    """Directory whose siblings are indexed — same scope as list_nearby_files."""
    # Lazy import: document_research pulls uno; venv maintain code must not load it at import time.
    from plugin.doc.document_research import resolve_listing_directory

    return resolve_listing_directory(ctx, model)


def _normalized_listing_root(listing_root: str) -> str:
    return os.path.normpath(os.path.abspath(listing_root))


def folder_cache_dir(listing_root: str, *, create_parent: bool = True) -> Path:
    """Base directory for one folder's corpus.db + JSON state (beside indexed documents)."""
    path = Path(_normalized_listing_root(listing_root)) / EMBEDDINGS_CACHE_DIRNAME
    if create_parent:
        path.mkdir(parents=True, exist_ok=True)
    return path


def corpus_db_path(listing_root: str, *, create_parent: bool = True) -> Path:
    """Unified SQLite corpus (chunks + FTS5 + vec0) for the document folder."""
    return folder_cache_dir(listing_root, create_parent=create_parent) / CORPUS_DB_FILENAME


def zvec_collection_path(listing_root: str, *, create_parent: bool = True) -> Path:
    """Filesystem path for a zvec collection store for this folder (side-by-side with corpus.db)."""
    # Place under the same writeragent_embeddings/ sibling dir as the sqlite corpus.
    base = folder_cache_dir(listing_root, create_parent=create_parent)
    return base / "zvec"


def _store_dir_populated(path: Path) -> bool:
    """Host-safe (no backend import) heuristic: dir exists and has any entries."""
    try:
        p = Path(path)
        if not p.exists() or not p.is_dir():
            return False
        return any(p.iterdir())
    except OSError:
        return False


def zvec_collection_looks_populated(collection_path: Path) -> bool:
    """Host-safe (no zvec import) heuristic: dir exists and has any entries.
    Used for early 'empty index' checks in tools before kicking off background maintain.
    """
    return _store_dir_populated(collection_path)


def lancedb_collection_path(listing_root: str, *, create_parent: bool = True) -> Path:
    """Filesystem path for a lancedb collection store for this folder (side-by-side with corpus.db)."""
    base = folder_cache_dir(listing_root, create_parent=create_parent)
    p = base / "lancedb"
    if create_parent:
        p.mkdir(parents=True, exist_ok=True)
    return p


def lancedb_collection_looks_populated(collection_path: Path) -> bool:
    """Host-safe (no lancedb import) heuristic: dir exists and has any entries."""
    return _store_dir_populated(collection_path)


def corpus_meta_path(listing_root: str, *, create_parent: bool = True) -> Path:
    """JSON corpus metadata beside corpus.db."""
    return folder_cache_dir(listing_root, create_parent=create_parent) / CORPUS_META_FILENAME


def _remove_path(path: Path) -> bool:
    if not path.exists():
        return False
    try:
        if path.is_dir():
            # Bugfix: rmtree(ignore_errors=True) hid a failed directory delete
            # and returned True. remove_stale_corpus_stores then logged
            # "Removed stale embeddings stores" while the directory remained.
            # Success means the path is gone.
            shutil.rmtree(path)
        else:
            path.unlink()
    except OSError:
        log.debug("Could not remove %s", path, exc_info=True)
        return False
    if path.exists():
        log.debug("Could not remove %s", path)
        return False
    return True


def _load_meta_object(meta_path: Path) -> dict[str, Any] | None:
    """Parse corpus_meta.json as an object.

    None means missing, unreadable, or a JSON value that is not an object
    (a list, string, or number). Callers that delete the corpus must not
    treat None as a schema mismatch: that path cold-wipes a live index.
    Decode errors were covered by #1153; non-objects still reached ``.get``.
    """
    if not meta_path.is_file():
        return None
    try:
        raw_text = meta_path.read_text(encoding="utf-8")
    except OSError:
        log.debug("read_corpus_meta failed for %s", meta_path, exc_info=True)
        return None
    # read_text is str. A non-str means the path handle did not yield a file.
    if not isinstance(raw_text, str):
        return None
    try:
        data = json.loads(raw_text)
    except json.JSONDecodeError:
        log.debug("read_corpus_meta failed for %s", meta_path, exc_info=True)
        return None
    if not isinstance(data, dict):
        log.debug("corpus_meta %s is %s, not an object", meta_path, type(data).__name__)
        return None
    return data


@dataclass
class CorpusMeta:
    """Typed view and atomic gateway for corpus_meta.json."""

    data: dict[str, str] = field(default_factory=dict)
    path: Path | None = None
    is_valid: bool = False

    @classmethod
    def load(cls, meta_path: Path | str) -> CorpusMeta:
        path = Path(meta_path)
        raw = _load_meta_object(path)
        if raw is None:
            return cls(path=path, is_valid=False)
        data = {str(k): str(v) for k, v in raw.items()}
        return cls(data=data, path=path, is_valid=True)

    @property
    def schema_version(self) -> str:
        return self.data.get("schema_version", "")

    @property
    def embedding_model(self) -> str:
        return self.data.get("embedding_model", "")

    @property
    def storage_backend(self) -> str:
        return self.data.get("storage_backend", "")

    @property
    def updated_at(self) -> str:
        return self.data.get("updated_at", "")

    @property
    def dim(self) -> int:
        val = self.data.get("dim")
        if val is None:
            return 0
        try:
            i = int(val)
            return i if i >= 0 else 0
        except (ValueError, TypeError):
            return 0

    @property
    def chunk_count(self) -> int:
        val = self.data.get("chunk_count")
        if val is None:
            return 0
        try:
            i = int(val)
            return i if i >= 0 else 0
        except (ValueError, TypeError):
            return 0

    def update(self, **fields: Any) -> CorpusMeta:
        for k, v in fields.items():
            if k in ("dim", "chunk_count"):
                try:
                    i = int(v) if v is not None else 0
                    val = i if i >= 0 else 0
                except (ValueError, TypeError):
                    val = 0
                self.data[k] = str(val)
            elif k in ("schema_version", "embedding_model", "storage_backend", "updated_at"):
                self.data[k] = str(v or "")
            else:
                self.data[str(k)] = str(v)
        return self

    def to_dict(self) -> dict[str, str]:
        return dict(self.data)

    def save(self, meta_path: Path | str | None = None) -> CorpusMeta:
        dest = Path(meta_path) if meta_path is not None else self.path
        if dest is None:
            raise ValueError("No destination path specified for CorpusMeta.save")
        dest.parent.mkdir(parents=True, exist_ok=True)

        payload = json.dumps(self.data, indent=2, sort_keys=True)
        fd, tmp_name = tempfile.mkstemp(prefix=dest.name + ".", suffix=".tmp", dir=str(dest.parent))
        tmp_path = Path(tmp_name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(payload)
            # The in-memory dict is the merge base; os.replace is the publish.
            # A second read of dest before writing was not atomic either and could overwrite concurrent writes.
            os.replace(tmp_path, dest)
        except Exception:
            tmp_path.unlink(missing_ok=True)
            raise

        self.path = dest
        self.is_valid = True
        return self

    @classmethod
    def ensure(cls, meta_path: Path | str, *, embedding_model: str, dim: int | None = None, chunk_count: int | None = None) -> CorpusMeta:
        """Initialize or refresh corpus metadata on the host."""
        now = str(time.time())
        fields: dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "embedding_model": embedding_model,
            "storage_backend": STORAGE_BACKEND,
            "updated_at": now,
        }
        if dim is not None:
            fields["dim"] = dim
        if chunk_count is not None:
            fields["chunk_count"] = chunk_count
        return cls.load(meta_path).update(**fields).save(meta_path)

    @classmethod
    def write_row_count(cls, listing_root: str, row_count: int, *, embedding_model: str = "", dim: int = 0) -> CorpusMeta:
        """Record row and chunk counts into folder corpus metadata (sqlite-vec backend)."""
        fields: dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "storage_backend": STORAGE_BACKEND,
            "row_count": str(row_count),
            "chunk_count": row_count,
            "updated_at": str(time.time()),
        }
        if embedding_model:
            fields["embedding_model"] = embedding_model
        if dim > 0:
            fields["dim"] = dim
        path = corpus_meta_path(listing_root)
        return cls.load(path).update(**fields).save(path)

    def schema_matches(self) -> bool:
        return self.is_valid and self.schema_version == SCHEMA_VERSION

    def model_matches_index(self, embedding_model: str) -> bool:
        return self.is_valid and self.embedding_model.strip() == embedding_model.strip()

    def needs_cold_rebuild(self, embedding_model: str) -> bool:
        if not self.path or not self.path.is_file():
            return True
        if not self.is_valid:
            # Corrupt or non-object meta should not trigger a cold rebuild (which wipes the DB).
            return False
        if not self.schema_matches():
            return True
        if self.chunk_count == 0:
            return True
        return not self.model_matches_index(embedding_model)

    def query_blocked_for_model(self, embedding_model: str) -> bool:
        """True when a vector query would hit a stale or wrong-dimension index."""
        if not self.path or not self.path.is_file():
            return False
        return self.needs_cold_rebuild(embedding_model)


def _vector_backend_is_empty(mode: str, listing_root: str | None, meta_path: Path, meta: CorpusMeta | None = None) -> bool:
    """True when a zvec or lancedb folder store has nothing to search.

    A built store has a positive ``chunk_count`` in corpus_meta.json and at
    least one file in the collection directory. Either signal missing means
    empty. A corrupt meta beside a live collection is not empty: a cold
    rebuild would delete that collection.
    """
    if not listing_root:
        return True
    if mode == "zvec":
        probe = zvec_collection_path(listing_root, create_parent=False)
        populated = zvec_collection_looks_populated(probe)
    else:
        probe = lancedb_collection_path(listing_root, create_parent=False)
        populated = lancedb_collection_looks_populated(probe)
    if not populated:
        return True
    if not meta_path.is_file():
        return False
    if meta is None:
        meta = CorpusMeta.load(meta_path)
    if not meta.is_valid:
        return False
    return meta.chunk_count <= 0


def index_is_empty(
    meta_path: Path,
    db_path: Path | None = None,
    *,
    search_mode: str | None = None,
    listing_root: str | None = None,
    meta: CorpusMeta | None = None,
) -> bool:
    """True when the active folder store has no indexed chunks.

    sqlite callers may omit *db_path*; it is the corpus.db beside *meta_path*.
    That omitted path still applies the live-db corrupt-meta guard.
    *search_mode* ``zvec`` / ``lancedb`` ignore *db_path*. Those backends never
    create corpus.db; pass *listing_root* so the collection directory can be
    probed. Other modes keep the sqlite rule: a missing corpus.db is empty.
    """
    mode = str(search_mode or "").strip().lower()
    if mode in ("zvec", "lancedb"):
        # A missing corpus.db means empty for sqlite. zvec and lancedb use
        # chunk_count plus collection presence, the same signals search_ui
        # and the research tools use. Treating a missing file as empty there
        # resolved cold on every tick and clear_folder_cache deleted the
        # real collection.
        return _vector_backend_is_empty(mode, listing_root, meta_path, meta=meta)
    if db_path is None:
        db_path = meta_path.parent / CORPUS_DB_FILENAME
    if not db_path.is_file():
        return True
    # Missing meta beside a live corpus.db is empty: needs_cold_rebuild and
    # cold maintain may wipe that db. Corrupt or non-object JSON is the
    # opposite (the branch below returns False): a file was written, and
    # treating it as empty would delete a live corpus (#1153). zvec and
    # lancedb do not use this branch; a populated collection with missing
    # or corrupt meta is not empty.
    if not meta_path.is_file():
        return True
    # Corrupt or non-object meta with a live DB is not "empty". Treating it as
    # empty selects a cold rebuild and deletes the corpus. #1153 covered JSON
    # decode errors; a JSON list or string took the chunk_count-0 path.
    if meta is None:
        meta = CorpusMeta.load(meta_path)
    if not meta.is_valid and db_path.is_file():
        return False
    return meta.chunk_count <= 0


SearchMode = Literal["fts", "embeddings", "hybrid", "llama_index", "zvec", "lancedb"]


def configured_folder_search_mode() -> str:
    """Settings value, lowercased. Includes "none"."""
    from plugin.framework.config import get_config

    return str(get_config("embeddings.folder_search_mode") or "none").strip().lower()


def parse_search_mode(raw: str | None) -> SearchMode:
    """Worker mode. Unknown and "none" become "hybrid", matching
    _resolved_folder_search_mode and embeddings_indexer._resolve_search_mode."""
    mode = str(raw or "").strip().lower()
    if mode in ("fts", "embeddings", "hybrid", "llama_index", "zvec", "lancedb"):
        return mode  # type: ignore[return-value]
    return "hybrid"


@dataclass(frozen=True)
class FolderStore:
    mode: str
    search_path: str | None
    is_empty: bool
    meta: CorpusMeta


def ready_search_path(
    mode: str,
    listing_root: str | None,
    meta_path: Any,
    db_path: Any,
    *,
    meta: CorpusMeta | None = None,
) -> str | None:
    """Path to search, or None when that store is not built yet.

    create_parent=True runs only after the probe says the store is populated.
    An empty store must not create the parent directory as a side effect of the check.
    """
    if mode == "zvec":
        if not listing_root:
            return None
        probe = zvec_collection_path(listing_root, create_parent=False)
        if not zvec_collection_looks_populated(probe):
            return None
        return str(zvec_collection_path(listing_root, create_parent=True))
    if mode == "lancedb":
        if not listing_root:
            return None
        probe = lancedb_collection_path(listing_root, create_parent=False)
        if not lancedb_collection_looks_populated(probe):
            return None
        return str(lancedb_collection_path(listing_root, create_parent=True))
    if index_is_empty(meta_path, db_path, meta=meta):
        return None
    return str(db_path)


def folder_store(
    listing_root: str | None,
    meta_path: Path,
    db_path: Path | None = None,
    mode: str | None = None,
) -> FolderStore:
    """Consolidated folder store state: mode, search_path, is_empty, meta."""
    resolved_mode = configured_folder_search_mode() if mode is None else mode
    meta = CorpusMeta.load(meta_path)
    empty = index_is_empty(meta_path, db_path, search_mode=resolved_mode, listing_root=listing_root, meta=meta)
    path = ready_search_path(resolved_mode, listing_root, meta_path, db_path, meta=meta)
    return FolderStore(mode=resolved_mode, search_path=path, is_empty=empty, meta=meta)


def remove_stale_corpus_stores(listing_root: str) -> bool:
    """Delete pre-v3 stores (legacy index.db, separate fts5.db)."""
    base = folder_cache_dir(listing_root, create_parent=False)
    removed = False
    removed |= _remove_path(base / LEGACY_INDEX_DB)
    removed |= _remove_path(base / LEGACY_FTS_DB)
    removed |= _remove_path(base / LEGACY_FTS_META)
    if removed:
        log.info("Removed stale embeddings stores in %s (corpus.db cold rebuild)", base)
    return removed


def clear_folder_cache(listing_root: str) -> None:
    """Remove corpus.db and JSON state for a cold rebuild. Also clears zvec store for the folder."""
    base = folder_cache_dir(listing_root, create_parent=False)
    _remove_path(base / CORPUS_DB_FILENAME)
    # An open WAL connection keeps sidecars after corpus.db is unlinked,
    # and the next open of a new file at that path raises sqlite3 disk I/O error.
    _remove_path(base / (CORPUS_DB_FILENAME + "-wal"))
    _remove_path(base / (CORPUS_DB_FILENAME + "-shm"))
    remove_stale_corpus_stores(listing_root)
    for name in (CORPUS_META_FILENAME, LEGACY_FILE_INDEX_STATE_FILENAME):
        _remove_path(base / name)
    # Side-by-side zvec/lancedb store
    _remove_path(base / "zvec")
    _remove_path(base / "lancedb")

    from plugin.embeddings.venv.embeddings_zvec import zvec_clear_cache

    zvec_clear_cache(str(base / "zvec"))


def maybe_upgrade_legacy_index(listing_root: str) -> None:
    """Drop stale v1/v2 stores. Called from resolve_index_context on every resolve.

    Missing, corrupt, or non-object meta: return without deleting.
    schema_version == SCHEMA_VERSION: remove legacy index.db, fts5.db, and
    fts_meta.json only.
    Any other object schema_version: clear_folder_cache (corpus.db, meta, zvec, lancedb).
    """
    meta = corpus_meta_path(listing_root, create_parent=False)
    data = _load_meta_object(meta)
    if data is None:
        # Missing, corrupt, or non-object JSON. Do not nuke the corpus, and
        # do not call .get on a list.
        return
    if str(data.get("schema_version", "")) == SCHEMA_VERSION:
        remove_stale_corpus_stores(listing_root)
        return
    clear_folder_cache(listing_root)


_USE_DEFAULT = object()


def resolve_index_context(ctx: Any = None, model: Any = None, *, listing_root: Any = _USE_DEFAULT) -> tuple[str, Path, Path, str] | tuple[None, None, None, str]:
    """Return (folder_key, corpus_db_path, corpus_meta_path, listing_root) or (None, None, None, error_message).

    On success the fourth value is the listing directory. On failure the first
    three are None and the fourth is the error string. Callers must branch on
    folder_key is None before using the fourth value as a path.

    Calls maybe_upgrade_legacy_index before returning paths. That deletes the
    folder cache when corpus_meta.json is an object whose schema_version is
    not SCHEMA_VERSION. When the version already matches, it only removes
    legacy index.db, fts5.db, and fts_meta.json if those files exist. Missing,
    corrupt, or non-object meta does not delete. This stays here so the search
    dialog, enqueue, FTS, and both research tools share one upgrade point.
    """
    if listing_root is _USE_DEFAULT:
        listing_root = resolve_folder_for_active_doc(ctx, model)
    if not listing_root:
        return None, None, None, "No nearby files found. Save the document or open sibling files in LibreOffice."
    folder_key = folder_corpus_key(listing_root)
    maybe_upgrade_legacy_index(listing_root)
    # This resolve is a read; writers already mkdir in CorpusMeta.save and connect_corpus_db.
    db_path = corpus_db_path(listing_root, create_parent=False)
    meta = corpus_meta_path(listing_root, create_parent=False)
    return folder_key, db_path, meta, listing_root
