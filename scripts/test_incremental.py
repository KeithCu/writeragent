#!/usr/bin/env python3
# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Run pytest or UNO tests incrementally on changed test files and module-matched tests.

Used by ``make test-bot``, ``make test-uno``, and ``make test`` to scope pytest (or ``--uno`` for
``testing_runner.py``) to staged, unstaged, and untracked changes:
- Changed ``test_*.py`` files under ``tests/`` (excluding ``*_uno.py`` in pytest
  mode; selecting only ``test_*_uno.py`` in ``--uno`` mode) run directly.
- Changed non-test ``.py`` modules (e.g. ``plugin/a/b/foo.py``) map by filename
  to ``tests/**/test_foo.py`` and ``tests/**/test_foo_*.py`` (or
  ``test_foo_uno.py`` and ``test_foo_*_uno.py`` in ``--uno`` mode), and also to
  every test file that names the module: ``import plugin.a.b.foo``,
  ``from plugin.a.b.foo import x``, ``from plugin.a.b import foo``, or a dotted
  string such as ``patch("plugin.a.b.foo.bar")``. A package ``__init__.py``
  counts as the package (``plugin.a.b``). Tests that reach the module only
  through another module are not selected.
- Changed non-Python files (fixtures, prompts, YAML) select the test files whose
  text contains the file's name, unless the name is one many files share
  (``module.yaml``, ``README.md``...).
- Exits with ``EXIT_FALLBACK_FULL`` (10) when ``pyproject.toml``, ``Makefile``,
  any ``conftest.py``, or any shared test helper under ``tests/`` changed so
  the ``Makefile`` can fall back to the full target.
"""

from __future__ import annotations

import argparse
import ast
import re
import subprocess
import sys
from pathlib import Path
from typing import Callable, Iterable

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.typecheck_incremental import (  # noqa: E402
    EXIT_FALLBACK_FULL,
    _normalize_rel_path,
    get_changed_files,
)

_FULL_RUN_CONFIGS = frozenset({"pyproject.toml", "Makefile"})
# Keep in sync with _USER_PROFILE_ONLY_UNO | _DEMO_ONLY_UNO in plugin/testing_runner.py:
# make test-uno skips these without --user-profile / WRITERAGENT_DEMO=1, so passing
# them as FILTER would trigger a discovery miss failure.
_EXCLUDED_UNO_FILES = frozenset(
    {
        "test_mock_llm_sidebar_uno.py",
        "test_mock_llm_peer_sidebar_uno.py",
        "test_demo_video_uno.py",
    }
)


def _is_eligible_pytest_file(rel: str) -> bool:
    """Return True if ``rel`` is a non-UNO pytest file under ``tests/``."""
    if not rel.startswith("tests/") or not rel.endswith(".py"):
        return False
    if rel.startswith("tests/semgrep/"):
        return False
    name = Path(rel).name
    return name.startswith("test_") and not name.endswith("_uno.py")


def _is_eligible_uno_file(rel: str) -> bool:
    """Return True if ``rel`` is a standard UNO test file under ``tests/``."""
    if not rel.startswith("tests/") or not rel.endswith(".py"):
        return False
    if rel.startswith("tests/semgrep/"):
        return False
    name = Path(rel).name
    if name in _EXCLUDED_UNO_FILES:
        return False
    return name.startswith("test_") and name.endswith("_uno.py")


def find_full_fallback_trigger(changed: Iterable[str]) -> tuple[str, str] | None:
    """Return ``(reason, rel_path)`` if a change requires a full pytest run."""
    for raw in changed:
        rel = _normalize_rel_path(raw)
        if not rel:
            continue
        if rel in _FULL_RUN_CONFIGS:
            return ("config", rel)
        name = Path(rel).name
        if name == "conftest.py":
            return ("conftest", rel)
        if (
            rel.startswith("tests/")
            and rel.endswith(".py")
            and not rel.startswith("tests/semgrep/")
            and name != "__init__.py"
            and not name.startswith("test_")
            and not name.endswith("_uno.py")
        ):
            return ("shared test helper", rel)
    return None


def _collect_test_files(
    repo_root: Path, pattern: str, is_eligible: Callable[[str], bool]
) -> list[str]:
    """Return sorted repo-relative paths of eligible test files under ``tests/``."""
    tests_dir = repo_root / "tests"
    if not tests_dir.is_dir():
        return []
    found: list[str] = []
    for path in tests_dir.rglob(pattern):
        if not path.is_file():
            continue
        rel = _normalize_rel_path(path.relative_to(repo_root).as_posix())
        if is_eligible(rel):
            found.append(rel)
    return sorted(found)


def _collect_pytest_files(repo_root: Path) -> list[str]:
    """Return sorted repo-relative paths of all non-UNO pytest files under ``tests/``."""
    return _collect_test_files(repo_root, "test_*.py", _is_eligible_pytest_file)


def _collect_uno_files(repo_root: Path) -> list[str]:
    """Return sorted repo-relative paths of all standard UNO test files under ``tests/``."""
    return _collect_test_files(repo_root, "test_*_uno.py", _is_eligible_uno_file)


def _module_stem(rel: str) -> str | None:
    """Return the module name stem used to match ``test_<stem>.py`` files."""
    path = Path(rel)
    if path.suffix != ".py":
        return None
    if path.stem != "__init__":
        return path.stem
    parent_name = path.parent.name
    if not parent_name or parent_name == ".":
        return None
    return parent_name


# Non-Python file names too common to say which tests read them.
_GENERIC_DATA_NAMES = frozenset(
    {
        "README.md",
        "AGENTS.md",
        "module.yaml",
        "plugin.yaml",
        "__init__.py",
        "uv.lock",
        ".gitignore",
    }
)


def _dotted_module(rel: str) -> str | None:
    """Return the import name of the repo-relative ``.py`` file ``rel``."""
    path = Path(rel)
    if path.suffix != ".py":
        return None
    parts = list(path.with_suffix("").parts)
    if parts and parts[-1] == "__init__":
        parts.pop()
    if not parts or not all(part.isidentifier() for part in parts):
        return None
    return ".".join(parts)


class _TestFileIndex:
    """Lazily read test files and answer which ones name a module or file."""

    def __init__(self, repo_root: Path, test_files: list[str]) -> None:
        self._repo_root = repo_root
        self._test_files = test_files
        self._text: dict[str, str] = {}
        self._imports: dict[str, frozenset[str]] = {}

    def text(self, test_rel: str) -> str:
        cached = self._text.get(test_rel)
        if cached is None:
            try:
                cached = (self._repo_root / test_rel).read_text(encoding="utf-8", errors="replace")
            except OSError:
                cached = ""
            self._text[test_rel] = cached
        return cached

    def imported_names(self, test_rel: str) -> frozenset[str]:
        """Return dotted names ``test_rel`` imports, including ``pkg.name`` for ``from pkg import name``."""
        cached = self._imports.get(test_rel)
        if cached is not None:
            return cached
        names: set[str] = set()
        try:
            tree = ast.parse(self.text(test_rel), filename=test_rel)
        except (SyntaxError, ValueError):
            tree = None
        if tree is not None:
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    names.update(alias.name for alias in node.names)
                elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
                    names.add(node.module)
                    names.update(f"{node.module}.{alias.name}" for alias in node.names)
        result = frozenset(names)
        self._imports[test_rel] = result
        return result

    def naming_module(self, dotted: str) -> list[str]:
        """Return test files that import ``dotted`` or name it in a dotted string."""
        leaf = dotted.rsplit(".", 1)[-1]
        pattern = re.compile(r"(?<![\w.])" + re.escape(dotted) + r"(?!\w)")
        hits: list[str] = []
        for test_rel in self._test_files:
            text = self.text(test_rel)
            if leaf not in text:
                continue
            if pattern.search(text) or dotted in self.imported_names(test_rel):
                hits.append(test_rel)
        return hits

    def naming_file(self, name: str) -> list[str]:
        """Return test files whose text contains the file name ``name``."""
        return [test_rel for test_rel in self._test_files if name in self.text(test_rel)]


def _select_test_files(
    changed: Iterable[str],
    repo_root: Path,
    all_test_files: list[str],
    is_eligible: Callable[[str], bool],
    matches_stem: Callable[[str, str], bool],
) -> list[str]:
    """Map changed files to sorted, existing test files."""
    by_name: dict[str, list[str]] = {}
    for test_rel in all_test_files:
        by_name.setdefault(Path(test_rel).name, []).append(test_rel)
    index = _TestFileIndex(repo_root, all_test_files)

    selected: set[str] = set()
    for raw in changed:
        rel = _normalize_rel_path(raw)
        if not rel or not (repo_root / rel).is_file():
            continue
        if not rel.endswith(".py"):
            name = Path(rel).name
            if name not in _GENERIC_DATA_NAMES:
                selected.update(index.naming_file(name))
            continue
        if rel.startswith("tests/"):
            if is_eligible(rel):
                selected.add(rel)
            continue

        stem = _module_stem(rel)
        if stem:
            for test_name, test_rels in by_name.items():
                if matches_stem(test_name, stem):
                    selected.update(test_rels)
        dotted = _dotted_module(rel)
        if dotted:
            selected.update(index.naming_module(dotted))

    return sorted(selected)


def _matches_pytest_stem(test_name: str, stem: str) -> bool:
    return test_name == f"test_{stem}.py" or test_name.startswith(f"test_{stem}_")


def _matches_uno_stem(test_name: str, stem: str) -> bool:
    return test_name == f"test_{stem}_uno.py" or (
        test_name.startswith(f"test_{stem}_") and test_name.endswith("_uno.py")
    )


def select_pytest_files(changed: Iterable[str], repo_root: Path = REPO_ROOT) -> list[str]:
    """Map changed ``.py`` files to sorted, existing non-UNO pytest files."""
    return _select_test_files(
        changed,
        repo_root,
        _collect_pytest_files(repo_root),
        _is_eligible_pytest_file,
        _matches_pytest_stem,
    )


def select_uno_files(changed: Iterable[str], repo_root: Path = REPO_ROOT) -> list[str]:
    """Map changed ``.py`` files to sorted, existing standard UNO test files."""
    return _select_test_files(
        changed,
        repo_root,
        _collect_uno_files(repo_root),
        _is_eligible_uno_file,
        _matches_uno_stem,
    )


def main(argv: list[str] | None = None, repo_root: Path = REPO_ROOT) -> int:
    """CLI entry point for incremental pytest or UNO test selection."""
    parser = argparse.ArgumentParser(description="Run pytest or UNO tests incrementally on changed files/modules.")
    parser.add_argument("--uno", action="store_true", help="Select *_uno.py tests instead of unit pytest tests.")
    parser.add_argument(
        "--print-filter",
        action="store_true",
        help="Print space-separated selected files to stdout instead of running a command.",
    )
    parser.add_argument("cmd", nargs=argparse.REMAINDER, help="Base command after '--'")
    args = parser.parse_args(argv)

    base_cmd = list(args.cmd)
    if base_cmd and base_cmd[0] == "--":
        base_cmd = base_cmd[1:]
    if not args.print_filter and not base_cmd:
        print("test-bot: missing base command after '--'", file=sys.stderr)
        return 2

    label = "UNO" if args.uno else "pytest"
    status_stream = sys.stderr if args.print_filter else sys.stdout

    changed = get_changed_files(repo_root)
    if not changed:
        print(f"test-bot: no changed files — nothing to test ({label})", file=status_stream, flush=True)
        return 0

    fallback = find_full_fallback_trigger(changed)
    if fallback is not None:
        reason, rel = fallback
        print(
            f"test-bot: {reason} changed ({rel}) — falling back to full {label}",
            file=status_stream,
            flush=True,
        )
        return EXIT_FALLBACK_FULL

    selector = select_uno_files if args.uno else select_pytest_files
    selected = selector(changed, repo_root)
    if not selected:
        print(f"test-bot: no matching {label} files — nothing to test", file=status_stream, flush=True)
        return 0

    print(
        f"test-bot: running {label} on {len(selected)} file(s): {', '.join(selected)}",
        file=status_stream,
        flush=True,
    )
    if args.print_filter:
        print(" ".join(selected), flush=True)
        return 0

    proc = subprocess.run([*base_cmd, *selected], cwd=repo_root, check=False)
    return int(proc.returncode)


if __name__ == "__main__":
    raise SystemExit(main())
