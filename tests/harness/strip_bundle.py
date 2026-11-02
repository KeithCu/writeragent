# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Helpers for tests that run against both checkout source and stripped release bundles.

``make release`` runs pytest on a stripped temp tree (typically under ``/tmp``) after
``scripts/strip_code.py`` removes assert statements, ``@deal.*`` decorators, and
logger ``.debug`` / ``.info`` call sites.
``make test-run`` uses unstripped source with deal installed. Assertions must accept both.
"""

from __future__ import annotations

import inspect
from pathlib import Path

import pytest


def module_source_contains(obj: object, needle: str) -> bool:
    """True when *needle* appears in the source file of *obj* (module or function)."""
    target: object = obj
    if inspect.isfunction(obj) or inspect.ismethod(obj):
        target = inspect.unwrap(obj)
    try:
        path = inspect.getfile(target)  # type: ignore[arg-type]
        source = Path(path).read_text(encoding="utf-8")
    except (OSError, TypeError):
        return False
    return needle in source


def asserts_enabled() -> bool:
    """Return True if Python assert statements are active."""
    try:
        assert False
        return False
    except AssertionError:
        return True


def is_release_build() -> bool:
    """True when running against a stripped release bundle."""
    try:
        from plugin.framework import thread_guard as tg

        if not hasattr(tg, "_designated_main_thread"):
            return True
    except Exception:
        pass
    repo_root = Path(__file__).resolve().parent.parent
    return not (repo_root / "scripts").is_dir()


def skip_if_release_build(reason: str = "Test skipped in release builds") -> None:
    """Skip test execution if running in a stripped release bundle."""
    if is_release_build():
        pytest.skip(reason)


def asserts_stripped() -> bool:
    """True when assert statements are stripped in production code or under python -O."""
    if not __debug__:
        return True
    try:
        if not asserts_enabled():
            return True
    except Exception:
        return is_release_build()
    return False


requires_asserts = pytest.mark.skipif(
    asserts_stripped(),
    reason="assert statements stripped in release bundle or under python -O",
)


