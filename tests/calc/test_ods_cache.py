# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for plugin.calc.ods_cache (mtime key, hit/miss, ODS skip)."""

from __future__ import annotations

import json
import os
from pathlib import Path

from plugin.calc.ods_cache import (
    CACHE_FORMAT_VERSION,
    ODS_CACHE_DIRNAME,
    cache_entry_paths,
    cache_key,
    is_cacheable_office_source,
    lookup_cached_ods,
    meta_matches_source,
    ods_cache_dir,
    ods_cache_enabled,
    source_stat,
    write_sidecar_meta,
)


def _write_xlsx(path: Path, payload: bytes = b"PK\x03\x04xlsx") -> Path:
    path.write_bytes(payload)
    return path


def _seed_cache(xlsx: Path, *, ods_bytes: bytes = b"PK\x03\x04cached-ods") -> tuple[Path, Path]:
    paths = cache_entry_paths(str(xlsx))
    assert paths is not None
    ods_path, meta_path = paths
    ods_path.parent.mkdir(parents=True, exist_ok=True)
    ods_path.write_bytes(ods_bytes)
    write_sidecar_meta(meta_path, str(xlsx))
    return ods_path, meta_path


def test_is_cacheable_office_source_xlsx_only():
    assert is_cacheable_office_source("/tmp/budget.xlsx")
    assert is_cacheable_office_source("/tmp/legacy.xls")
    assert not is_cacheable_office_source("/tmp/ledger.ods")
    assert not is_cacheable_office_source("/tmp/sales.csv")


def test_cache_key_changes_with_mtime_or_size(tmp_path: Path):
    xlsx = _write_xlsx(tmp_path / "budget.xlsx")
    abs_path, mtime_ns, size = source_stat(str(xlsx))
    first = cache_key(abs_path, mtime_ns, size)
    assert first == cache_key(abs_path, mtime_ns, size)
    assert first != cache_key(abs_path, mtime_ns + 1, size)
    assert first != cache_key(abs_path, mtime_ns, size + 1)
    assert first != cache_key(abs_path + "x", mtime_ns, size)


def test_lookup_hit_then_mtime_change_misses(tmp_path: Path):
    xlsx = _write_xlsx(tmp_path / "budget.xlsx")
    ods_path, _meta = _seed_cache(xlsx)
    hit = lookup_cached_ods(str(xlsx))
    assert hit == ods_path

    st = os.stat(xlsx)
    os.utime(xlsx, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000_000))
    assert lookup_cached_ods(str(xlsx)) is None


def test_lookup_size_change_misses(tmp_path: Path):
    xlsx = _write_xlsx(tmp_path / "budget.xlsx")
    _seed_cache(xlsx)
    xlsx.write_bytes(xlsx.read_bytes() + b"x")
    assert lookup_cached_ods(str(xlsx)) is None


def test_lookup_missing_meta_is_miss(tmp_path: Path):
    xlsx = _write_xlsx(tmp_path / "budget.xlsx")
    ods_path, meta_path = _seed_cache(xlsx)
    meta_path.unlink()
    assert ods_path.is_file()
    assert lookup_cached_ods(str(xlsx)) is None


def test_lookup_format_version_bump_is_miss(tmp_path: Path):
    xlsx = _write_xlsx(tmp_path / "budget.xlsx")
    _ods_path, meta_path = _seed_cache(xlsx)
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    meta["cache_format_version"] = CACHE_FORMAT_VERSION + 1
    meta_path.write_text(json.dumps(meta), encoding="utf-8")
    assert lookup_cached_ods(str(xlsx)) is None


def test_ods_source_never_looks_up_cache(tmp_path: Path):
    ods = tmp_path / "ledger.ods"
    ods.write_bytes(b"PK\x03\x04ods")
    assert lookup_cached_ods(str(ods)) is None
    assert not (tmp_path / ODS_CACHE_DIRNAME).exists()


def test_ods_cache_dir_is_beside_source_folder(tmp_path: Path):
    xlsx = _write_xlsx(tmp_path / "budget.xlsx")
    assert ods_cache_dir(str(xlsx)) == tmp_path / ODS_CACHE_DIRNAME


def test_meta_matches_source_rejects_wrong_path(tmp_path: Path):
    xlsx = _write_xlsx(tmp_path / "budget.xlsx")
    abs_path, mtime_ns, size = source_stat(str(xlsx))
    meta = {
        "cache_format_version": CACHE_FORMAT_VERSION,
        "source_path": abs_path,
        "mtime_ns": mtime_ns,
        "size": size,
    }
    assert meta_matches_source(meta, abs_path, mtime_ns, size)
    assert not meta_matches_source(meta, abs_path + ".bak", mtime_ns, size)


def test_ods_cache_enabled_defaults_true_when_config_missing():
    from unittest.mock import patch

    with patch("plugin.framework.config.get_config", side_effect=Exception("no schema")):
        assert ods_cache_enabled() is True
