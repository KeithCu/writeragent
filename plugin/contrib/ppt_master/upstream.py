# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Load skill-tree script modules by file path (avoids package __init__ side effects on LO host)."""

from __future__ import annotations

import importlib.util
import os
import sys
from contextlib import contextmanager
from pathlib import Path
from types import ModuleType
from typing import Any, Callable, Iterator, TypeVar

_T = TypeVar("_T")


@contextmanager
def upstream_scripts_path(data_root: Path) -> Iterator[bool]:
    """Temporarily prepend ``<data_root>/scripts`` to sys.path for pure-Python imports."""
    scripts = Path(data_root).expanduser().resolve() / "scripts"
    if not scripts.is_dir():
        yield False
        return
    entry = str(scripts)
    added = entry not in sys.path
    if added:
        sys.path.insert(0, entry)
    try:
        yield True
    finally:
        if added:
            sys.path.remove(entry)


def _load_module_from_file(module_name: str, file_path: Path) -> ModuleType | None:
    if not file_path.is_file():
        return None
    spec = importlib.util.spec_from_file_location(module_name, file_path)
    if spec is None or spec.loader is None:
        return None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _load_pptx_discovery(data_root: Path) -> ModuleType | None:
    scripts = Path(data_root).expanduser().resolve() / "scripts"
    discovery_py = scripts / "svg_to_pptx" / "pptx_discovery.py"
    return _load_module_from_file("ppt_master_upstream_pptx_discovery", discovery_py)


def with_upstream_script(
    data_root: Path,
    loader: Callable[[Path], ModuleType | None],
    fn: Callable[[ModuleType], _T],
) -> _T | None:
    """Load a standalone module file and run *fn* (no svg_to_pptx package __init__)."""
    mod = loader(data_root)
    if mod is None:
        return None
    return fn(mod)


def collect_svg_files_upstream(project_path: Path, data_root: Path) -> list[Path] | None:
    """Use ``find_svg_files`` from skill-tree ``pptx_discovery.py`` when installed."""

    def _collect(mod: ModuleType) -> list[Path] | None:
        find_svg_files: Any = getattr(mod, "find_svg_files", None)
        if not callable(find_svg_files):
            return None
        resolved = Path(project_path).expanduser().resolve()
        files, _used = find_svg_files(resolved, source="final")
        if not files:
            files, _used = find_svg_files(resolved, source="output")
        return list(files) if files else None

    return with_upstream_script(data_root, _load_pptx_discovery, _collect)


def collect_notes_upstream(project_path: Path, data_root: Path) -> dict[str, str] | None:
    """Use ``find_notes_files`` from skill-tree ``pptx_discovery.py`` when installed."""

    def _notes(mod: ModuleType) -> dict[str, str] | None:
        find_notes_files: Any = getattr(mod, "find_notes_files", None)
        if not callable(find_notes_files):
            return None
        resolved = Path(project_path).expanduser().resolve()
        notes = find_notes_files(resolved)
        return dict(notes) if notes else None

    return with_upstream_script(data_root, _load_pptx_discovery, _notes)


def collect_svg_files(project_path: Path, *, subdir: str = "svg_final", data_root: Path | None = None) -> list[Path]:
    """Return sorted SVG paths from a ppt-master project folder."""
    root = data_root
    if root is None:
        env = os.environ.get("PPT_MASTER_DATA_ROOT", "").strip()
        if env:
            root = Path(env)
    if root is not None:
        upstream_files = collect_svg_files_upstream(project_path, root)
        if upstream_files:
            return upstream_files

    # Fallback when skill tree is not installed (unit tests, minimal dirs).
    resolved = Path(project_path).expanduser().resolve()
    for name in (subdir, "svg_output"):
        folder = resolved / name
        if folder.is_dir():
            files = sorted(folder.glob("*.svg"))
            if files:
                return files
    return []
