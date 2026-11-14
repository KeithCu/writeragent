#!/usr/bin/env python3
# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Run a static checker incrementally on changed Python files.

Used by ``make typecheck`` to scope ``basedpyright``, ``mypy``, ``bandit``,
``opengrep``, and ``pyspector`` to Python files changed since
``origin/master...HEAD`` plus staged, unstaged, and untracked changes.
Falls back to the tool's full-repo invocation when ``origin/master`` has no
merge-base (for example, a shallow checkout without ``origin/master``).
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path
from typing import Iterable

REPO_ROOT = Path(__file__).resolve().parents[1]

_PYSPECTOR_EXCLUDED_PARTS = frozenset(
    {
        ".venv",
        "venv",
        ".git",
        "__pycache__",
        "build",
        "dist",
        "node_modules",
        "vendor",
        "contrib",
        "lib",
        "tests",
        "demos",
    }
)

SUPPORTED_TOOLS = ("basedpyright", "mypy", "bandit", "opengrep", "pyspector")


def _normalize_rel_path(path: str) -> str:
    norm = path.replace("\\", "/")
    while norm.startswith("./"):
        norm = norm[2:]
    return norm


def _is_in_scope(tool: str, rel_path: str) -> bool:
    if not rel_path.endswith(".py"):
        return False

    if tool == "basedpyright":
        if not (rel_path.startswith("plugin/") or rel_path.startswith("compute_service/")):
            return False
        if rel_path.startswith(("plugin/contrib/", "plugin/lib/", "plugin/tests/")):
            return False
        return rel_path not in {
            "plugin/scripting/venv/text_analytics.py",
            "plugin/scripting/venv/quant.py",
        }

    if tool == "mypy":
        if not (rel_path.startswith("plugin/") or rel_path.startswith("compute_service/")):
            return False
        if rel_path.startswith(("plugin/contrib/", "plugin/lib/", "plugin/tests/")):
            return False
        return rel_path != "plugin/scripting/venv/text_analytics.py"

    if tool == "bandit":
        if not rel_path.startswith("plugin/"):
            return False
        if rel_path.startswith(("plugin/contrib/", "plugin/lib/", "plugin/tests/")):
            return False
        return rel_path != "plugin/scripting/venv/text_analytics.py"

    if tool == "opengrep":
        if not rel_path.startswith("plugin/"):
            return False
        return not rel_path.startswith(("plugin/contrib/", "plugin/lib/"))

    if tool == "pyspector":
        if not rel_path.startswith("plugin/"):
            return False
        parts = rel_path.split("/")
        if any(part in _PYSPECTOR_EXCLUDED_PARTS for part in parts[:-1]):
            return False
        filename = parts[-1]
        if filename.startswith("test_") or filename.endswith("_test.py"):
            return False
        return True

    raise ValueError(f"Unsupported tool: {tool}")


def filter_files_for_tool(
    tool: str,
    paths: Iterable[str],
    repo_root: Path = REPO_ROOT,
) -> list[str]:
    """Return sorted, existing Python file paths within ``tool``'s scope."""
    selected: set[str] = set()
    for raw in paths:
        rel = _normalize_rel_path(raw)
        if not rel or not _is_in_scope(tool, rel):
            continue
        if (repo_root / rel).is_file():
            selected.add(rel)
    return sorted(selected)


def _git_z_paths(repo_root: Path, git_args: list[str]) -> list[str]:
    proc = subprocess.run(
        ["git", *git_args],
        cwd=repo_root,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.decode("utf-8", errors="replace").strip() or "git failed")
    raw = proc.stdout.decode("utf-8", errors="replace")
    return [entry for entry in raw.split("\0") if entry]


def get_changed_files(repo_root: Path = REPO_ROOT) -> list[str] | None:
    """Return changed/untracked repo-relative paths, or ``None`` if no merge-base."""
    merge_base = subprocess.run(
        ["git", "merge-base", "origin/master", "HEAD"],
        cwd=repo_root,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    if merge_base.returncode != 0:
        return None

    paths: set[str] = set()
    for args in (
        ["diff", "-z", "--name-only", "--diff-filter=ACMR", "origin/master...HEAD"],
        ["diff", "-z", "--name-only", "--diff-filter=ACMR", "--cached"],
        ["diff", "-z", "--name-only", "--diff-filter=ACMR"],
        ["ls-files", "-z", "--others", "--exclude-standard"],
    ):
        paths.update(_git_z_paths(repo_root, args))
    return sorted(paths)


def chunk_files(files: list[str], max_chars: int = 16000) -> list[list[str]]:
    """Split ``files`` into batches whose joined argument length stays under ``max_chars``."""
    if not files:
        return []
    chunks: list[list[str]] = []
    current: list[str] = []
    current_chars = 0
    for path in files:
        cost = len(path) + 1
        if current and current_chars + cost > max_chars:
            chunks.append(current)
            current = [path]
            current_chars = cost
        else:
            current.append(path)
            current_chars += cost
    if current:
        chunks.append(current)
    return chunks


def build_invocations(tool: str, base_cmd: list[str], files: list[str] | None) -> list[list[str]]:
    """Build subprocess argument lists for ``tool`` in full (``files is None``) or incremental mode."""
    if files is None:
        if tool in ("basedpyright", "mypy"):
            return [list(base_cmd)]
        if tool == "bandit":
            return [[*base_cmd, "-r", "plugin"]]
        if tool == "opengrep":
            return [[*base_cmd, "plugin"]]
        if tool == "pyspector":
            return [[*base_cmd, "scan", "plugin", "--ai", "-c", "pyspector.toml", "--msg=False"]]
        raise ValueError(f"Unsupported tool: {tool}")

    if tool == "pyspector":
        return [[*base_cmd, "scan", path, "--ai", "-c", "pyspector.toml", "--msg=False"] for path in files]

    return [[*base_cmd, *batch] for batch in chunk_files(files)]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run a checker incrementally on changed Python files.")
    parser.add_argument("--tool", required=True, choices=SUPPORTED_TOOLS)
    parser.add_argument("cmd", nargs=argparse.REMAINDER, help="Base command after '--'")
    args = parser.parse_args(argv)

    base_cmd = list(args.cmd)
    if base_cmd and base_cmd[0] == "--":
        base_cmd = base_cmd[1:]
    if not base_cmd:
        print(f"{args.tool}: missing base command after '--'", file=sys.stderr)
        return 2

    changed = get_changed_files(REPO_ROOT)
    if changed is None:
        print(f"{args.tool}: origin/master merge-base not found — running full check", flush=True)
        invocations = build_invocations(args.tool, base_cmd, None)
    else:
        scoped = filter_files_for_tool(args.tool, changed, REPO_ROOT)
        if not scoped:
            print(f"{args.tool}: no changed files in scope — skipped", flush=True)
            return 0
        print(f"{args.tool}: checking {len(scoped)} changed file(s)", flush=True)
        invocations = build_invocations(args.tool, base_cmd, scoped)

    env = os.environ.copy()
    if args.tool == "opengrep":
        env["SEMGREP_SEND_METRICS"] = "off"

    exit_code = 0
    for cmd in invocations:
        proc = subprocess.run(cmd, cwd=REPO_ROOT, env=env, check=False)
        if proc.returncode != 0 and exit_code == 0:
            exit_code = int(proc.returncode)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
