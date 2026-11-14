#!/usr/bin/env python3
# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Run a static checker incrementally on changed Python files.

Used by ``make typecheck`` to scope ``basedpyright``, ``mypy``, ``bandit``,
``opengrep``, and ``pyspector`` to Python files changed since
``origin/master...HEAD`` plus staged, unstaged, and untracked changes.
Excludes are read directly from their owning configs (``pyproject.toml``,
``pyspector.toml``, or ``--exclude`` flags from the ``Makefile``).

Exits with ``EXIT_FALLBACK_FULL`` (10) when ``origin/master`` has no
merge-base, when any ``--config-path`` changed, or when changed files exceed
``--max-files``, so the ``Makefile`` can delegate to the full target.
"""

from __future__ import annotations

import argparse
import fnmatch
import subprocess
import sys
import tomllib
from pathlib import Path
from typing import Iterable

REPO_ROOT = Path(__file__).resolve().parents[1]
EXIT_FALLBACK_FULL = 10

SUPPORTED_TOOLS = ("basedpyright", "mypy", "bandit", "opengrep", "pyspector")


def _normalize_rel_path(path: str) -> str:
    norm = path.replace("\\", "/")
    while norm.startswith("./"):
        norm = norm[2:]
    return norm


def _matches_exclude(rel_path: str, pattern: str) -> bool:
    pat = _normalize_rel_path(pattern).rstrip("/")
    if not pat:
        return False
    if "*" in pat or "?" in pat:
        if fnmatch.fnmatch(rel_path, pat):
            return True
        if pat.startswith("**/") and fnmatch.fnmatch(rel_path, pat[3:]):
            return True
        return False
    if "/" not in pat:
        parts = rel_path.split("/")
        return pat in parts[:-1] or parts[-1] == pat
    return rel_path == pat or rel_path.startswith(pat + "/")


def _load_tool_scope(
    tool: str,
    repo_root: Path,
    cli_roots: Iterable[str],
    cli_excludes: Iterable[str],
) -> tuple[tuple[str, ...], list[str]]:
    """Load (include_roots, excludes) from the tool's owning config or CLI flags."""
    roots = tuple(f"{_normalize_rel_path(r).rstrip('/')}/" for r in cli_roots)

    if tool in ("basedpyright", "mypy", "bandit"):
        pyproject = tomllib.loads((repo_root / "pyproject.toml").read_text(encoding="utf-8"))
        if tool == "basedpyright":
            cfg = pyproject["tool"]["basedpyright"]
            return roots or tuple(f"{p.rstrip('/')}/" for p in cfg["include"]), list(cfg["exclude"])
        if tool == "mypy":
            cfg = pyproject["tool"]["mypy"]
            return roots or tuple(f"{p.rstrip('/')}/" for p in cfg["packages"]), list(cfg["exclude"])
        cfg = pyproject["tool"]["bandit"]
        return roots or ("plugin/",), list(cfg["exclude_dirs"])

    if tool == "opengrep":
        return roots or ("plugin/",), list(cli_excludes)

    if tool == "pyspector":
        pyspector_cfg = tomllib.loads((repo_root / "pyspector.toml").read_text(encoding="utf-8"))
        return roots or ("plugin/",), list(pyspector_cfg["tool"]["pyspector"]["exclude"])

    raise ValueError(f"Unsupported tool: {tool}")


def _find_changed_config(changed: Iterable[str], config_paths: Iterable[str]) -> str | None:
    """Return the first changed path matching any ``config_paths`` file or directory."""
    normalized_cfg = [_normalize_rel_path(p).rstrip("/") for p in config_paths if p]
    if not normalized_cfg:
        return None
    for raw in changed:
        rel = _normalize_rel_path(raw)
        for cfg in normalized_cfg:
            if rel == cfg or rel.startswith(cfg + "/"):
                return rel
    return None


def filter_files_for_tool(
    tool: str,
    paths: Iterable[str],
    repo_root: Path = REPO_ROOT,
    cli_roots: Iterable[str] = (),
    cli_excludes: Iterable[str] = (),
) -> list[str]:
    """Return sorted, existing Python file paths within ``tool``'s scope."""
    roots, excludes = _load_tool_scope(tool, repo_root, cli_roots, cli_excludes)
    selected: set[str] = set()
    for raw in paths:
        rel = _normalize_rel_path(raw)
        if not rel or not rel.endswith(".py"):
            continue
        if not rel.startswith(roots):
            continue
        if any(_matches_exclude(rel, pat) for pat in excludes):
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


def build_invocations(base_cmd: list[str], files: list[str]) -> list[list[str]]:
    """Build subprocess argument lists for incremental ``files``.

    If ``{target}`` is present in ``base_cmd`` (used by ``pyspector scan`` which
    accepts only a single path), emit one invocation per file; otherwise append
    chunked file lists.
    """
    if "{target}" in base_cmd:
        idx = base_cmd.index("{target}")
        return [[*base_cmd[:idx], path, *base_cmd[idx + 1 :]] for path in files]
    return [[*base_cmd, *batch] for batch in chunk_files(files)]


def main(argv: list[str] | None = None, repo_root: Path = REPO_ROOT) -> int:
    parser = argparse.ArgumentParser(description="Run a checker incrementally on changed Python files.")
    parser.add_argument("--tool", required=True, choices=SUPPORTED_TOOLS)
    parser.add_argument("--root", action="append", default=[], help="Include root directory")
    parser.add_argument("--exclude", action="append", default=[], help="Exclude path/pattern (opengrep)")
    parser.add_argument(
        "--config-path",
        action="append",
        default=[],
        help="File or directory whose change triggers full-check fallback",
    )
    parser.add_argument("--max-files", type=int, default=0, help="Fall back to full check above N files")
    parser.add_argument("cmd", nargs=argparse.REMAINDER, help="Base command after '--'")
    args = parser.parse_args(argv)

    base_cmd = list(args.cmd)
    if base_cmd and base_cmd[0] == "--":
        base_cmd = base_cmd[1:]
    if not base_cmd:
        print(f"{args.tool}: missing base command after '--'", file=sys.stderr)
        return 2

    changed = get_changed_files(repo_root)
    if changed is None:
        print(f"{args.tool}: origin/master merge-base not found — falling back to full check", flush=True)
        return EXIT_FALLBACK_FULL

    config_hit = _find_changed_config(changed, args.config_path)
    if config_hit is not None:
        print(f"{args.tool}: config changed ({config_hit}) — falling back to full check", flush=True)
        return EXIT_FALLBACK_FULL

    scoped = filter_files_for_tool(args.tool, changed, repo_root, args.root, args.exclude)
    if not scoped:
        print(f"{args.tool}: no changed files in scope — skipped", flush=True)
        return 0

    if args.max_files > 0 and len(scoped) > args.max_files:
        print(
            f"{args.tool}: {len(scoped)} changed files exceed threshold ({args.max_files}) — falling back to full check",
            flush=True,
        )
        return EXIT_FALLBACK_FULL

    print(f"{args.tool}: checking {len(scoped)} changed file(s)", flush=True)
    exit_code = 0
    for cmd in build_invocations(base_cmd, scoped):
        proc = subprocess.run(cmd, cwd=repo_root, check=False)
        if proc.returncode != 0 and exit_code == 0:
            exit_code = int(proc.returncode)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
