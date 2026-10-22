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
import functools
from collections import OrderedDict
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Sequence

from plugin.contrib.smolagents.local_python_executor import (
    _HANDLERS,
    is_forbidden_dunder_attribute,
)
from plugin.framework.deal_shim import (
    DEAL_MAX_CMD_ARGS,
    DEAL_MAX_SOURCE,
    DEAL_MAX_TOKEN,
    UNDER_CROSSHAIR,
    ascii_bounded,
    deal,
    str_bounded,
)
from plugin.scripting.sandbox import import_authorized


def _deal_sandbox_imports_ok_pytest(authorized_imports: object) -> bool:
    # Production passes VENV_AUTHORIZED_IMPORTS (tuple, ~99 names), not a short list.
    if not isinstance(authorized_imports, (list, tuple)):
        return False
    return all(isinstance(x, str) and "\0" not in x and str_bounded(x, DEAL_MAX_TOKEN) for x in authorized_imports)


def _deal_sandbox_imports_ok_crosshair(authorized_imports: object) -> bool:
    if not isinstance(authorized_imports, (list, tuple)):
        return False
    return (
        len(authorized_imports) <= DEAL_MAX_CMD_ARGS
        and all(ascii_bounded(x, DEAL_MAX_TOKEN) for x in authorized_imports)
    )


_deal_sandbox_imports_ok = (
    _deal_sandbox_imports_ok_crosshair if UNDER_CROSSHAIR else _deal_sandbox_imports_ok_pytest
)


def _deal_sandbox_code_ok_pytest(code: object) -> bool:
    # `_cache_key` joins with NUL; production =PY() scripts may be non-ASCII.
    # =PY() source is longer than DEAL_MAX_SOURCE. The cap raised
    # PreContractError before the cache key was built. NUL stays out:
    # the key joins on NUL. CrossHair keeps the short ASCII source.
    return isinstance(code, str) and "\0" not in code


def _deal_sandbox_code_ok_crosshair(code: object) -> bool:
    return isinstance(code, str) and ascii_bounded(code, DEAL_MAX_SOURCE) and "\0" not in code


_deal_sandbox_code_ok = (
    _deal_sandbox_code_ok_crosshair if UNDER_CROSSHAIR else _deal_sandbox_code_ok_pytest
)

# ExceptHandler is handled inside evaluate_try rather than in evaluate_ast.
_STRUCTURAL_NODES: frozenset[type[ast.AST]] = frozenset({ast.ExceptHandler})

_DEFAULT_MAX_ENTRIES = 256
_MAX_CACHED_SOURCE_CHARS = 65536


@deal.post(lambda result: result is None or isinstance(result, str))
def validate_sandbox_ast(module: ast.Module, authorized_imports: Sequence[str]) -> str | None:
    """Return an error message if *module* violates sandbox policy, else ``None``."""
    # AST walk on a symbolic module hangs deep check.
    # crosshair: off
    for node in ast.walk(module):
        if isinstance(node, (ast.stmt, ast.expr)):
            node_type = type(node)
            if node_type not in _HANDLERS and node_type not in _STRUCTURAL_NODES:
                return f"{node.__class__.__name__} is not supported."
        if isinstance(node, (ast.FunctionDef, ast.ClassDef)) and node.decorator_list:
            return "Decorators are not supported in the sandbox"
        if isinstance(node, ast.Import):
            for alias in node.names:
                # writeragent.X is checked as plugin.X. A blanket writeragent.*
                # on the list used to pass here, then AliasImporter loaded
                # framework.config and LlmClient.
                if not import_authorized(alias.name, authorized_imports):
                    # Keep the message short. Dumping authorized_imports exposes duckdb.
                    return f"Import of {alias.name} is not allowed."
        elif isinstance(node, ast.ImportFrom):
            if node.level and node.level > 0:
                # Relative imports (from . import x) are not supported in the sandbox.
                return "Relative imports are not allowed."
            module_name = node.module or ""
            if not import_authorized(module_name, authorized_imports):
                # Keep the message short. Dumping authorized_imports exposes duckdb.
                return f"Import from {module_name} is not allowed."
            for alias in node.names:
                if alias.name != "*" and is_forbidden_dunder_attribute(alias.name):
                    return f"Forbidden access to dunder attribute: {alias.name}"
        elif isinstance(node, ast.Attribute):
            if is_forbidden_dunder_attribute(node.attr):
                return f"Forbidden access to dunder attribute: {node.attr}"
    return None


@dataclass(frozen=True)
class HotEntry:
    """Cached parse + static validation for one code + import-policy key."""

    module: ast.Module | None
    error: str | None


_lock = threading.Lock()
_cache: OrderedDict[str, HotEntry] = OrderedDict()
_max_entries: int = _DEFAULT_MAX_ENTRIES


@functools.cache
def _memoized_fingerprint(imports_tuple: tuple[str, ...]) -> str:
    return "\n".join(sorted(set(imports_tuple)))


@deal.pre(lambda authorized_imports: _deal_sandbox_imports_ok(authorized_imports))
def _imports_fingerprint(authorized_imports: Sequence[str]) -> str:
    # crosshair: off
    # LocalPythonExecutor.authorized_imports is a set union, so order is not
    # the policy. Sort and dedupe or the same allowlist misses the cache.
    # Names are joined with newline. A newline or NUL in a name would alias
    # two allowlists; test_allowlist_entries_fit_cache_key locks that out.
    # _cache_key then joins source and this fingerprint with NUL.
    return _memoized_fingerprint(tuple(authorized_imports))


@deal.pre(lambda code, authorized_imports: _deal_sandbox_code_ok(code) and _deal_sandbox_imports_ok(authorized_imports))
def _cache_key(code: str, authorized_imports: Sequence[str]) -> str:
    # crosshair: off
    material = code + "\0" + _imports_fingerprint(authorized_imports)
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def _format_syntax_error(exc: SyntaxError) -> str:
    # crosshair: off
    # offset is 1-based. Spacing by offset puts the caret one column to the
    # right of the token CPython underlines (ast.parse("def f(:") is offset 7,
    # the ':'). A null byte on 3.12+ is a SyntaxError with lineno/offset/text
    # None; "line None" and a caret are not a location.
    if exc.lineno is None:
        return f"Code parsing failed due to: {type(exc).__name__}: {exc}"
    text = exc.text or ""
    offset = exc.offset or 0
    caret = (" " * (offset - 1) + "^") if offset else "^"
    return (
        f"Code parsing failed on line {exc.lineno} due to: {type(exc).__name__}: {exc}\n"
        f"{text}{caret}"
    )


@deal.pre(lambda code, authorized_imports: _deal_sandbox_code_ok(code) and _deal_sandbox_imports_ok(authorized_imports))
def _build_entry(code: str, authorized_imports: Sequence[str]) -> HotEntry:
    # A long 1+1+... chain makes ast.parse or validate_sandbox_ast raise
    # RecursionError (an Exception). execute_with_return logged it with
    # log.exception as an internal fault. It is bad user source, so return
    # a HotEntry error and do not log.
    try:
        try:
            module = ast.parse(code)
        except SyntaxError as exc:
            return HotEntry(module=None, error=_format_syntax_error(exc))
        except ValueError as exc:
            # What was wrong: ast.parse raises ValueError for a null byte on
            # CPython before 3.12 (gh-96670). LibreOffice 24.8 ships Python 3.9
            # and is the extension minimum; execute_with_return runs there.
            # How it happened: only SyntaxError and RecursionError were caught.
            # Release OXTs strip @deal, so the NUL deal pre does not run, and
            # the ValueError left get_hot_entry. execute_with_return logged
            # log.exception; _run_on_executor attached a traceback.
            # Why this fixes it: null bytes are bad user source. Cache a parse
            # error so evaluate_python_code raises InterpreterError.
            return HotEntry(
                module=None,
                error=f"Code parsing failed due to: {type(exc).__name__}: {exc}",
            )
        try:
            compile(module, "<sandbox>", "exec", dont_inherit=True)
        except SyntaxError as exc:
            return HotEntry(module=None, error=_format_syntax_error(exc))
        validation_error = validate_sandbox_ast(module, authorized_imports)
        return HotEntry(module=module, error=validation_error)
    except RecursionError:
        return HotEntry(module=None, error="Code parsing failed: expression is too deeply nested")


@deal.pre(lambda code, authorized_imports: _deal_sandbox_code_ok(code) and _deal_sandbox_imports_ok(authorized_imports))
def get_hot_entry(code: str, authorized_imports: Sequence[str]) -> HotEntry:
    """Return cached or freshly built parse + static validation for *code*."""
    # crosshair: off
    if len(code) > _MAX_CACHED_SOURCE_CHARS:
        return _build_entry(code, authorized_imports)
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
