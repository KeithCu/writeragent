#!/usr/bin/env python3
# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Run pytest incrementally on changed test files and module-matched tests.

Used by ``make test-bot`` and ``make test`` to scope pytest to staged,
unstaged, and untracked changes:
- Changed ``test_*.py`` files under ``tests/`` (excluding ``*_uno.py``) run directly.
- Changed non-test ``.py`` modules (e.g. ``plugin/a/b/foo.py``) map by filename
  to ``tests/**/test_foo.py`` and ``tests/**/test_foo_*.py`` (excluding ``*_uno.py``).
- Exits with ``EXIT_FALLBACK_FULL`` (10) when ``pyproject.toml``, ``Makefile``,
  any ``conftest.py``, or any shared test helper under ``tests/`` changed so
  the ``Makefile`` can fall back to the full ``pytest`` target.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path
from typing import Iterable

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.typecheck_incremental import (  # noqa: E402
    EXIT_FALLBACK_FULL,
    _normalize_rel_path,
    get_changed_files,
)

_FULL_RUN_CONFIGS = frozenset({"pyproject.toml", "Makefile"})


def _is_eligible_pytest_file(rel: str) -> bool:
    """Return True if ``rel`` is a non-UNO pytest file under ``tests/``."""
    if not rel.startswith("tests/") or not rel.endswith(".py"):
        return False
    if rel.startswith("tests/semgrep/"):
        return False
    name = Path(rel).name
    return name.startswith("test_") and not name.endswith("_uno.py")


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


def _collect_pytest_files(repo_root: Path) -> list[str]:
    """Return sorted repo-relative paths of all non-UNO pytest files under ``tests/``."""
    tests_dir = repo_root / "tests"
    if not tests_dir.is_dir():
        return []
    found: list[str] = []
    for path in tests_dir.rglob("test_*.py"):
        if not path.is_file():
            continue
        rel = _normalize_rel_path(path.relative_to(repo_root).as_posix())
        if _is_eligible_pytest_file(rel):
            found.append(rel)
    return sorted(found)


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


def select_pytest_files(changed: Iterable[str], repo_root: Path = REPO_ROOT) -> list[str]:
    """Map changed ``.py`` files to sorted, existing non-UNO pytest files."""
    all_pytest_files = _collect_pytest_files(repo_root)
    by_name: dict[str, list[str]] = {}
    for test_rel in all_pytest_files:
        by_name.setdefault(Path(test_rel).name, []).append(test_rel)

    selected: set[str] = set()
    for raw in changed:
        rel = _normalize_rel_path(raw)
        if not rel or not rel.endswith(".py"):
            continue
        if not (repo_root / rel).is_file():
            continue
        if rel.startswith("tests/"):
            if _is_eligible_pytest_file(rel):
                selected.add(rel)
            continue

        stem = _module_stem(rel)
        if not stem:
            continue
        exact_name = f"test_{stem}.py"
        prefix = f"test_{stem}_"
        for test_name, test_rels in by_name.items():
            if test_name == exact_name or test_name.startswith(prefix):
                selected.update(test_rels)

    return sorted(selected)


def main(argv: list[str] | None = None, repo_root: Path = REPO_ROOT) -> int:
    """CLI entry point for incremental pytest selection."""
    parser = argparse.ArgumentParser(description="Run pytest incrementally on changed files/modules.")
    parser.add_argument("cmd", nargs=argparse.REMAINDER, help="Base pytest command after '--'")
    args = parser.parse_args(argv)

    base_cmd = list(args.cmd)
    if base_cmd and base_cmd[0] == "--":
        base_cmd = base_cmd[1:]
    if not base_cmd:
        print("test-bot: missing base command after '--'", file=sys.stderr)
        return 2

    changed = get_changed_files(repo_root)
    if not changed:
        print("test-bot: no changed files — nothing to test", flush=True)
        return 0

    fallback = find_full_fallback_trigger(changed)
    if fallback is not None:
        reason, rel = fallback
        print(f"test-bot: {reason} changed ({rel}) — falling back to full pytest", flush=True)
        return EXIT_FALLBACK_FULL

    selected = select_pytest_files(changed, repo_root)
    if not selected:
        print("test-bot: no matching pytest files — nothing to test", flush=True)
        return 0

    print(f"test-bot: running pytest on {len(selected)} file(s): {', '.join(selected)}", flush=True)
    proc = subprocess.run([*base_cmd, *selected], cwd=repo_root, check=False)
    return int(proc.returncode)


if __name__ == "__main__":
    raise SystemExit(main())
