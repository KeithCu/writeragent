# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Single owner for reading and rewriting the active UNO test profile's config."""

from __future__ import annotations

import contextlib
import json
import os
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

from plugin.framework.config import (
    _invalidate_config_cache,
    atomic_write_text,
    parse_config_json_text,
)
from plugin.scripting.venv_worker import shutdown_venv_workers_for_tests
from plugin.testing_runner import throwaway_writeragent_json

_VENV_KEY = "scripting.python_venv_path"


def profile_config_path() -> Path:
    """This run's writeragent.json. Raises instead of touching a real profile.

    A real profile holds API keys, and ``_leftover_shared_diag`` prints the
    file body on failure. Do not fall back to ``get_config_file_path()``: it
    keeps the first profile's path after an office recycle.
    """
    path = throwaway_writeragent_json()
    if path is None:
        raise AssertionError("no throwaway test profile; refusing to edit a real writeragent.json")
    return path


def patch_profile_config(mutate: Callable[[dict[str, Any]], None]) -> None:
    """Load, mutate and atomically rewrite this run's config; drop the client cache.

    The file may start with the ``//`` schema header, which plain json.load rejects.
    """
    path = profile_config_path()
    data: dict[str, Any] = {}
    if path.exists():
        parsed = parse_config_json_text(path.read_text(encoding="utf-8"))
        if parsed is None:
            raise AssertionError(f"unreadable test profile config: {path}")
        data = parsed
    mutate(data)
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(str(path), json.dumps(data, indent=2) + "\n")
    _invalidate_config_cache()


def settle_soffice_config() -> None:
    """Wait out the 2 s get_config cache in soffice (plugin/framework/config.py:_load).

    ``patch_profile_config`` drops only this process's cache.
    """
    time.sleep(2.1)


def project_venv() -> str:
    """Checkout ``.venv`` that has an interpreter.

    The repo is an ancestor of this file during ``make test``; during
    ``make release`` the tree is a stripped temp copy without ``.venv``. A
    configured venv with no interpreter is ``VENV_NOT_FOUND``, so ``=PY()``
    never spills. Release exports ``WRITERAGENT_PROJECT_ROOT`` from the
    checkout Makefile.
    """

    def _has_python(venv: Path) -> bool:
        return (venv / "bin" / "python").is_file() or (venv / "Scripts" / "python.exe").is_file()

    for parent in Path(__file__).resolve().parents:
        venv = parent / ".venv"
        if _has_python(venv):
            return str(venv)
    root = os.environ.get("WRITERAGENT_PROJECT_ROOT", "").strip()
    if root:
        venv = Path(root) / ".venv"
        if _has_python(venv):
            return str(venv)
    raise AssertionError("project .venv not found (release tree needs WRITERAGENT_PROJECT_ROOT)")


@contextlib.contextmanager
def venv_in_profile(path: str) -> Iterator[None]:
    """Run soffice's =PY() with *path* as the configured venv; the previous setting is restored on exit.

    Only this key is undone, so other saves made inside the block survive.
    No wait on exit: soffice may keep the venv for up to 2 s.
    """
    previous: dict[str, Any] = {}

    def _set(data: dict[str, Any]) -> None:
        if _VENV_KEY in data:
            previous[_VENV_KEY] = data[_VENV_KEY]
        data[_VENV_KEY] = path

    def _restore(data: dict[str, Any]) -> None:
        data.pop(_VENV_KEY, None)
        data.update(previous)

    patch_profile_config(_set)
    try:
        shutdown_venv_workers_for_tests()
        settle_soffice_config()
        yield
    finally:
        patch_profile_config(_restore)
        shutdown_venv_workers_for_tests()


@contextlib.contextmanager
def repo_venv_in_profile() -> Iterator[None]:
    """Run soffice's =PY() in the repo venv; the previous setting is restored on exit."""
    with venv_in_profile(project_venv()):
        yield
