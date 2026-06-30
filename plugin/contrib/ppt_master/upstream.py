# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Import helpers for upstream ppt-master scripts (user venv / pip install).

UPSTREAM NOTE:
  Unmodified ppt-master Python lives under ``<data_root>/scripts/`` after:
  ``uv pip install "ppt-master @ git+https://github.com/hugohe3/ppt-master.git"``
  WriterAgent only vendors adapter modules in this package; do not copy svg_to_pptx here.

  Import ``pptx_discovery`` by file path so we do not execute ``svg_to_pptx/__init__.py``
  (that pulls python-pptx). Full svg_to_pptx runs in the user venv worker when needed.
"""

from __future__ import annotations

import importlib.util
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
    """Load a standalone upstream module file and run *fn* (no svg_to_pptx package __init__)."""
    mod = loader(data_root)
    if mod is None:
        return None
    return fn(mod)


def collect_svg_files_upstream(project_path: Path, data_root: Path) -> list[Path] | None:
    """Use upstream ``pptx_discovery.find_svg_files`` when the venv skill tree is present."""

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
