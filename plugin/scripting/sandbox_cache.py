# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
"""Static AST sandbox policy checks and in-memory parse/validation hot cache."""

from __future__ import annotations

import ast
import hashlib
import threading
from collections import OrderedDict
from dataclasses import dataclass

from plugin.contrib.smolagents.local_python_executor import (
    check_import_authorized,
    is_forbidden_dunder_attribute,
)
from plugin.framework.deal_shim import (
    DEAL_MAX_CMD_ARGS,
    DEAL_MAX_SOURCE,
    DEAL_MAX_TOKEN,
    ascii_bounded,
    deal,
)


def _deal_sandbox_imports_ok(authorized_imports: object) -> bool:
    return (
        isinstance(authorized_imports, list)
        and len(authorized_imports) <= DEAL_MAX_CMD_ARGS
        and all(ascii_bounded(x, DEAL_MAX_TOKEN) for x in authorized_imports)
    )


def _deal_sandbox_code_ok(code: object) -> bool:
    # ascii_bounded allows NUL (isascii); `_cache_key` joins with NUL — exclude it.
    return isinstance(code, str) and ascii_bounded(code, DEAL_MAX_SOURCE) and "\0" not in code

# Statement/expression forms the interpreter refuses outright (see evaluate_ast else branch).
_FORBIDDEN_NODE_TYPES: tuple[type[ast.AST], ...] = (
    ast.AsyncFunctionDef,
    ast.AsyncFor,
    ast.AsyncWith,
    ast.Global,
    ast.Nonlocal,
    ast.NamedExpr,
)
if hasattr(ast, "Match"):
    _FORBIDDEN_NODE_TYPES = _FORBIDDEN_NODE_TYPES + (ast.Match,)  # type: ignore[attr-defined]
if hasattr(ast, "MatchStar"):
    _FORBIDDEN_NODE_TYPES = _FORBIDDEN_NODE_TYPES + (ast.MatchStar,)  # type: ignore[attr-defined]

_DEFAULT_MAX_ENTRIES = 256


@deal.post(lambda result: result is None or isinstance(result, str))
def validate_sandbox_ast(module: ast.Module, authorized_imports: list[str]) -> str | None:
    """Return an error message if *module* violates sandbox policy, else ``None``."""
    # AST walk on a symbolic module hangs deep check.
    # crosshair: off
    for node in ast.walk(module):
        if isinstance(node, _FORBIDDEN_NODE_TYPES):
            return f"{node.__class__.__name__} is not supported."
        if isinstance(node, ast.Import):
            for alias in node.names:
                if not check_import_authorized(alias.name, authorized_imports):
                    return (
                        f"Import of {alias.name} is not allowed. "
                        f"Authorized imports are: {str(authorized_imports)}"
                    )
        elif isinstance(node, ast.ImportFrom):
            module_name = node.module or ""
            if not check_import_authorized(module_name, authorized_imports):
                return (
                    f"Import from {module_name} is not allowed. "
                    f"Authorized imports are: {str(authorized_imports)}"
                )
        elif isinstance(node, ast.Attribute):
            if is_forbidden_dunder_attribute(node.attr):
                return f"Forbidden access to dunder attribute: {node.attr}"
    return None


@dataclass(frozen=True, slots=True)
class HotEntry:
    """Cached parse + static validation for one code + import-policy key."""

    module: ast.Module | None
    error: str | None


_lock = threading.Lock()
_cache: OrderedDict[str, HotEntry] = OrderedDict()
_max_entries = _DEFAULT_MAX_ENTRIES


@deal.pre(lambda authorized_imports: _deal_sandbox_imports_ok(authorized_imports))
def _imports_fingerprint(authorized_imports: list[str]) -> str:
    return "\n".join(sorted(set(authorized_imports)))


@deal.pre(lambda code, authorized_imports: _deal_sandbox_code_ok(code) and _deal_sandbox_imports_ok(authorized_imports))
def _cache_key(code: str, authorized_imports: list[str]) -> str:
    material = code + "\0" + _imports_fingerprint(authorized_imports)
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def _format_syntax_error(exc: SyntaxError) -> str:
    # crosshair: off  # SyntaxError text/offset formatting (cover-all 33355986432: sandbox_cache in-flight 6h, no flushed log). Doable later with a tiny lineno/text domain.
    text = exc.text or ""
    return (
        f"Code parsing failed on line {exc.lineno} due to: {type(exc).__name__}: {str(exc)}\n"
        f"{text}"
        f"{' ' * (exc.offset or 0)}^"
    )


@deal.pre(lambda code, authorized_imports: _deal_sandbox_code_ok(code) and _deal_sandbox_imports_ok(authorized_imports))
def _build_entry(code: str, authorized_imports: list[str]) -> HotEntry:
    try:
        module = ast.parse(code)
    except SyntaxError as exc:
        return HotEntry(module=None, error=_format_syntax_error(exc))
    validation_error = validate_sandbox_ast(module, authorized_imports)
    return HotEntry(module=module, error=validation_error)


@deal.pre(lambda code, authorized_imports: _deal_sandbox_code_ok(code) and _deal_sandbox_imports_ok(authorized_imports))
def get_hot_entry(code: str, authorized_imports: list[str]) -> HotEntry:
    """Return cached or freshly built parse + static validation for *code*."""
    key = _cache_key(code, authorized_imports)
    with _lock:
        if key in _cache:
            _cache.move_to_end(key)
            return _cache[key]
    entry = _build_entry(code, authorized_imports)
    with _lock:
        if key in _cache:
            _cache.move_to_end(key)
            return _cache[key]
        _cache[key] = entry
        if len(_cache) > _max_entries:
            _cache.popitem(last=False)
    return entry


def clear_python_code_hot_cache() -> None:
    """Clear the hot cache (tests)."""
    with _lock:
        _cache.clear()
