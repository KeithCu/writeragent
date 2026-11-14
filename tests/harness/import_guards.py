# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Shared AST helpers for top-level import-layering guard tests."""

from __future__ import annotations

import ast
from collections.abc import Iterable
from pathlib import Path
from types import ModuleType


def _matches_forbidden(name: str, forbidden: tuple[str, ...]) -> bool:
    return any(name == prefix or name.startswith(prefix + ".") for prefix in forbidden)


def assert_no_toplevel_import(target: ModuleType | Path | str, forbidden: Iterable[str]) -> None:
    """Fail if *target* has a top-level import matching any prefix in *forbidden*."""
    if isinstance(target, ModuleType):
        if target.__file__ is None:
            raise AssertionError(f"module {target.__name__} has no __file__")
        path = Path(target.__file__)
        label = target.__name__
    else:
        path = Path(target)
        label = str(path)

    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    forbidden_tuple = tuple(forbidden)
    hits: list[str] = []
    for node in tree.body:
        if isinstance(node, ast.Import):
            for alias in node.names:
                if _matches_forbidden(alias.name, forbidden_tuple):
                    hits.append(alias.name)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            if _matches_forbidden(node.module, forbidden_tuple):
                hits.append(node.module)
    assert not hits, f"{label} has forbidden top-level imports: {hits}"
