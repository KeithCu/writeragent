#!/usr/bin/env python3
"""Helpers for walking and validating plugin package import graphs."""

from __future__ import annotations

import ast
from collections import deque
import os
from pathlib import Path
from typing import Iterable

_SKIP_GRAPH_DIRS = frozenset({"build", "node_modules", "tests", "vendor", "__pycache__"})


def _plugin_mod_to_candidates(mod: str) -> tuple[str, ...]:
    rel = mod.replace(".", "/")
    return (rel + ".py", rel + "/__init__.py")


def _is_shipped_plugin_module(mod: str, shipped: set[str]) -> bool:
    if mod in ("plugin._manifest", "plugin._manifest_librepy", "plugin._manifest_libreharper"):
        return True
    cands = _plugin_mod_to_candidates(mod)
    return any(c in shipped for c in cands)


def _is_type_checking_if(node: ast.If) -> bool:
    test = node.test
    if isinstance(test, ast.Name) and test.id == "TYPE_CHECKING":
        return True
    if isinstance(test, ast.Attribute) and test.attr == "TYPE_CHECKING":
        return True
    return False


def _module_level_import_nodes(tree: ast.AST) -> list[ast.AST]:
    nodes: list[ast.AST] = []

    def walk(body: list[ast.stmt]) -> None:
        for stmt in body:
            if isinstance(stmt, (ast.Import, ast.ImportFrom)):
                nodes.append(stmt)
            elif isinstance(stmt, ast.If):
                if not _is_type_checking_if(stmt):
                    walk(stmt.body)
                walk(stmt.orelse)
            elif isinstance(stmt, ast.Try):
                handler_names = []
                for handler in stmt.handlers:
                    if handler.type is None:
                        handler_names.append("bare")
                    elif isinstance(handler.type, ast.Name):
                        handler_names.append(handler.type.id)
                    elif isinstance(handler.type, ast.Tuple):
                        handler_names.extend(
                            elt.id for elt in handler.type.elts if isinstance(elt, ast.Name)
                        )
                # Optional imports (Cython accelerator, generated manifest, etc.)
                if "ImportError" in handler_names or "ModuleNotFoundError" in handler_names:
                    walk(stmt.orelse)
                    walk(stmt.finalbody)
                    continue
                walk(stmt.body)
                for handler in stmt.handlers:
                    walk(handler.body)
                walk(stmt.orelse)
                walk(stmt.finalbody)
            elif isinstance(stmt, ast.With):
                walk(stmt.body)

    walk(getattr(tree, "body", []))
    return nodes


def _resolved_import_from_module(
    path: Path | str, node: ast.ImportFrom, repo_root: Path | str | None = None
) -> str | None:
    p = Path(path).resolve()
    root = (
        Path(repo_root).resolve()
        if repo_root is not None
        else Path(__file__).resolve().parents[1]
    )
    try:
        pkg_parts = p.relative_to(root).with_suffix("").parts
    except ValueError:
        return node.module
    parent_pkg = pkg_parts[:-1]
    if node.level:
        if node.level > len(parent_pkg):
            return None
        rel_base = parent_pkg[: len(parent_pkg) - node.level + 1]
        if node.module:
            return ".".join(rel_base + tuple(node.module.split(".")))
        return ".".join(rel_base)
    return node.module


def dotted_module_name(rel: str) -> str | None:
    """Return the dotted import name of a repo-relative ``.py`` file ``rel``."""
    path = Path(rel)
    if path.suffix != ".py":
        return None
    parts = list(path.with_suffix("").parts)
    if parts and parts[-1] == "__init__":
        parts.pop()
    if not parts or not all(part.isidentifier() for part in parts):
        return None
    return ".".join(parts)


def module_level_imports(path: Path | str, repo_root: Path | str | None = None) -> set[str]:
    """Return dotted module names imported at module level in ``path``.

    Uses ``_module_level_import_nodes`` (which skips ``if TYPE_CHECKING:``
    bodies while keeping ``else:`` branches, and skips ``try`` bodies guarded by
    ``ImportError``/``ModuleNotFoundError``) and resolves relative imports.
    """
    p = Path(path)
    try:
        tree = ast.parse(p.read_text(encoding="utf-8", errors="replace"), filename=str(p))
    except (OSError, SyntaxError, ValueError):
        return set()
    mods: set[str] = set()
    for node in _module_level_import_nodes(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                mods.add(alias.name)
        elif isinstance(node, ast.ImportFrom):
            mod = _resolved_import_from_module(p, node, repo_root)
            if mod:
                mods.add(mod)
                for alias in node.names:
                    if alias.name != "*":
                        mods.add(f"{mod}.{alias.name}")
    return mods


def _iter_non_test_py_files(repo_root: Path) -> Iterable[Path]:
    """Yield non-test Python files under ``plugin/``, ``scripts/``, and ``compute_service/``.

    Skips ``plugin/lib`` (third-party vendored packages) while keeping
    ``plugin/contrib`` because first-party adapters inside it import ``plugin.framework``.
    """
    for root_name in ("plugin", "scripts", "compute_service"):
        base = repo_root / root_name
        if not base.is_dir():
            continue
        for dirpath, dirnames, filenames in os.walk(base):
            rel_dir = Path(dirpath).relative_to(repo_root).as_posix()
            dirnames[:] = [
                d
                for d in dirnames
                if not d.startswith(".")
                and d not in _SKIP_GRAPH_DIRS
                and not (rel_dir == "plugin" and d == "lib")
            ]
            for filename in filenames:
                if filename.endswith(".py"):
                    yield Path(dirpath) / filename


def reverse_import_closure(
    seed_rels: Iterable[str],
    repo_root: Path | str,
    *,
    max_depth: int | None = 1,
    stop_at: frozenset[str] = frozenset({"plugin.testing_runner"}),
) -> list[str]:
    """Return repo-relative paths of modules importing ``seed_rels`` up to ``max_depth``.

    Only module-level runtime imports are followed. ``stop_at`` prevents
    expanding into harness entrypoints (such as ``plugin.testing_runner``)
    unless they were directly in ``seed_rels``.
    """
    root = Path(repo_root)
    seed_mods = {mod for rel in seed_rels if (mod := dotted_module_name(rel)) is not None}
    if not seed_mods:
        return []

    rev_imports: dict[str, set[str]] = {}
    mod_to_rel: dict[str, str] = {}
    for path in _iter_non_test_py_files(root):
        rel = path.relative_to(root).as_posix()
        dm = dotted_module_name(rel)
        if not dm:
            continue
        mod_to_rel[dm] = rel
        for imp in module_level_imports(path, root):
            rev_imports.setdefault(imp, set()).add(dm)

    seen = set(seed_mods)
    queue: deque[tuple[str, int]] = deque((mod, 0) for mod in seed_mods)
    while queue:
        mod, depth = queue.popleft()
        if max_depth is not None and depth >= max_depth:
            continue
        for dependent in rev_imports.get(mod, ()):
            if dependent in stop_at or dependent in seen:
                continue
            seen.add(dependent)
            queue.append((dependent, depth + 1))

    return [mod_to_rel[mod] for mod in seen if mod not in seed_mods and mod in mod_to_rel]


def missing_module_level_plugin_imports(
    repo_root: Path | str, shipped: set[str] | list[str]
) -> list[str]:
    root = Path(repo_root)
    shipped_set = set(shipped)
    missing: list[str] = []

    for rel in sorted(shipped_set):
        if not rel.endswith(".py"):
            continue
        path = root / rel
        if not path.is_file():
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=rel)
        for node in _module_level_import_nodes(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name.startswith("plugin.") and not _is_shipped_plugin_module(
                        alias.name, shipped_set
                    ):
                        missing.append(f"{rel} -> import {alias.name}")
                continue
            if not isinstance(node, ast.ImportFrom):
                continue
            mod = _resolved_import_from_module(path, node, root)
            if not mod or not (mod == "plugin" or mod.startswith("plugin.")):
                continue
            if not _is_shipped_plugin_module(mod, shipped_set):
                missing.append(f"{rel} -> from {mod}")
                continue
            for alias in node.names:
                if alias.name == "*":
                    continue
                sub = f"{mod}.{alias.name}"
                if not any((root / cand).is_file() for cand in _plugin_mod_to_candidates(sub)):
                    continue  # attribute, not submodule
                if not _is_shipped_plugin_module(sub, shipped_set):
                    missing.append(f"{rel} -> from {mod} import {alias.name}")

    return missing


def missing_dual_install_imports(
    repo_root: Path | str, shipped: set[str] | list[str]
) -> list[str]:
    root = Path(repo_root)
    shipped_set = set(shipped)
    init_path = root / "plugin" / "chatbot" / "__init__.py"
    if not init_path.is_file():
        return []
    tree = ast.parse(init_path.read_text(encoding="utf-8"), filename="plugin/chatbot/__init__.py")
    missing: list[str] = []
    for node in tree.body:
        if not isinstance(node, ast.ImportFrom) or not node.module:
            continue
        if not (node.module == "plugin" or node.module.startswith("plugin.")):
            continue
        for alias in node.names:
            if alias.name == "*":
                continue
            rel = node.module.replace(".", "/") + "/" + alias.name + ".py"
            pkg = node.module.replace(".", "/") + "/" + alias.name + "/__init__.py"
            file_mod = node.module.replace(".", "/") + ".py"
            if rel not in shipped_set and pkg not in shipped_set and file_mod not in shipped_set:
                missing.append(f"plugin/chatbot/__init__.py -> from {node.module} import {alias.name}")
    return missing
