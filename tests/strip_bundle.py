# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Helpers for tests that run against both checkout source and stripped release bundles.

``make release`` runs pytest on a stripped temp tree (typically under ``/tmp``) after
``scripts/strip_code.py`` removes ``@deal.*`` and logger ``.debug`` / ``.info`` call sites.
``make test-run`` uses unstripped source with deal installed. Assertions must accept both.
"""

from __future__ import annotations

import inspect
from collections.abc import Callable
from pathlib import Path
from typing import Any

import deal
import pytest

_MISSING = object()


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


def _decorator_header(source: str) -> str:
    """Decorator lines from ``inspect.getsource`` through the ``def`` line."""
    header: list[str] = []
    for line in source.splitlines(keepends=True):
        header.append(line)
        stripped = line.lstrip()
        if stripped.startswith("def ") or stripped.startswith("async def "):
            break
    return "".join(header)


def deal_pre_present(obj: object) -> bool:
    """True when ``@deal.pre`` remains on *obj*'s definition (unstripped).

    Must not scan the whole module or function body: a comment that mentions
    ``@deal.pre`` (e.g. ``stream_normalizer._streaming_replay``) would look
    like the decorator survived ``strip_code``.
    """
    target: object = obj
    if inspect.isfunction(obj) or inspect.ismethod(obj):
        target = inspect.unwrap(obj)
    try:
        source = inspect.getsource(target)  # type: ignore[arg-type]
    except (OSError, TypeError):
        return module_source_contains(obj, "@deal.pre")
    return "@deal.pre" in _decorator_header(source)


def expect_pre_or_body(
    call: Callable[[], Any],
    *,
    body_result: Any = _MISSING,
    body_exc: type[BaseException] | tuple[type[BaseException], ...] = (),
) -> Any:
    """Run *call*: deal pre raises, or stripped/shim body returns *body_result* / *body_exc*.

    Must not leak IndexError or other unguarded crashes either way.
    """
    if isinstance(body_exc, type):
        body_exc = (body_exc,)
    try:
        result = call()
    except deal.PreContractError:
        return None
    except body_exc:
        return None
    if body_result is not _MISSING:
        assert result == body_result
        return result
    pytest.fail("expected deal.PreContractError or a body guard, got %r" % (result,))


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

