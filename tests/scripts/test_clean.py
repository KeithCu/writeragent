# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for scripts/clean.py."""

from __future__ import annotations

from pathlib import Path

from scripts.clean import clean


def test_clean_removes_pycache_pyc_and_build(tmp_path: Path) -> None:
    build_dir = tmp_path / "build"
    build_dir.mkdir()
    (build_dir / "WriterAgent.oxt").write_text("dummy", encoding="utf-8")

    src_dir = tmp_path / "plugin" / "pkg"
    src_dir.mkdir(parents=True)
    source_file = src_dir / "module.py"
    source_file.write_text("print('hello')", encoding="utf-8")

    cache_dir1 = src_dir / "__pycache__"
    cache_dir1.mkdir()
    (cache_dir1 / "module.cpython-313.pyc").write_bytes(b"\x00")

    sub_cache = src_dir / "sub" / "__pycache__"
    sub_cache.mkdir(parents=True)
    (sub_cache / "sub.cpython-313.pyc").write_bytes(b"\x00")

    loose_pyc = tmp_path / "plugin" / "orphan.pyc"
    loose_pyc.write_bytes(b"\x00")

    native_so = tmp_path / "native" / "writeragent_vec" / "src" / "writeragent_vec" / "lib.so"
    native_so.parent.mkdir(parents=True)
    native_so.write_bytes(b"\x7fELF")

    clean(tmp_path)

    assert not build_dir.exists()
    assert not cache_dir1.exists()
    assert not sub_cache.exists()
    assert not loose_pyc.exists()
    assert source_file.exists()
    assert native_so.exists()


def test_clean_missing_build_does_not_raise(tmp_path: Path) -> None:
    # Running clean when build/ does not exist should complete cleanly.
    clean(tmp_path)


def test_clean_does_not_follow_symlinks(tmp_path: Path) -> None:
    external = tmp_path / "external"
    external.mkdir()
    ext_cache = external / "__pycache__"
    ext_cache.mkdir()
    ext_pyc = ext_cache / "ext.pyc"
    ext_pyc.write_bytes(b"\x00")

    target_root = tmp_path / "worktree"
    target_root.mkdir()
    (target_root / "vendor").symlink_to(external, target_is_directory=True)

    clean(target_root)

    assert ext_cache.exists()
    assert ext_pyc.exists()
