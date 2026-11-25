#!/usr/bin/env python3
# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Run the branch's added or modified UNO tests through isolated uno-probe.sh.

Used by ``make test-bot`` after incremental pytest. Selects
``tests/**/test_*_uno.py`` files added or modified relative to the merge-base
with ``origin/master`` (committed, staged, unstaged, and untracked), excluding
user-profile and demo-only suites. Invokes ``UNO_PROBE`` (default
``~/Desktop/Python/wa-pipeline-scripts/uno-probe.sh``) once with every selected
file so LibreOffice starts only once. Skips (exit 0) when the probe is not
installed, when ``SKIP_UNO=1``, or when run in the main WriterAgent checkout.
Fails the process if the probe reports a test failure.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.test_incremental import _is_eligible_uno_file  # noqa: E402
from scripts.typecheck_incremental import (  # noqa: E402
    _git_z_paths,
    get_changed_files,
)

DEFAULT_PROBE = Path.home() / "Desktop/Python/wa-pipeline-scripts/uno-probe.sh"
MAIN_CHECKOUT = Path.home() / "Desktop/Python/writeragent"
_BASE_REFS = ("origin/master", "master")


def _git_stdout(repo_root: Path, args: list[str]) -> str | None:
    """Return stripped git stdout, or None if the command failed."""
    proc = subprocess.run(
        ["git", *args],
        cwd=repo_root,
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    if proc.returncode != 0:
        return None
    return proc.stdout.strip()


def merge_base(repo_root: Path, refs: tuple[str, ...] = _BASE_REFS) -> str | None:
    """Return the merge-base of HEAD and the first existing base ref."""
    for ref in refs:
        if _git_stdout(repo_root, ["rev-parse", "--verify", ref]) is None:
            continue
        base = _git_stdout(repo_root, ["merge-base", "HEAD", ref])
        if base:
            return base
    return None


def is_main_checkout(repo_root: Path, main: Path = MAIN_CHECKOUT) -> bool:
    """True when ``repo_root`` is the main WriterAgent checkout, not a worktree."""
    try:
        return repo_root.resolve() == main.resolve()
    except OSError:
        return False


def list_branch_uno_files(repo_root: Path) -> list[str]:
    """Return eligible UNO test files added or modified on this branch.

    Uses ``git diff`` against the merge-base with ``origin/master`` (falling
    back to ``master``, then to uncommitted changes only) plus untracked files.
    Deleted paths and user-profile / demo-only suites are omitted.
    """
    paths: set[str] = set()
    base = merge_base(repo_root)
    if base is not None:
        paths.update(_git_z_paths(repo_root, ["diff", "-z", "--name-only", "--diff-filter=ACMR", base]))
    else:
        paths.update(get_changed_files(repo_root))
    paths.update(_git_z_paths(repo_root, ["ls-files", "-z", "--others", "--exclude-standard"]))
    selected: list[str] = []
    for raw in sorted(paths):
        rel = raw.replace("\\", "/")
        if _is_eligible_uno_file(rel) and (repo_root / rel).is_file():
            selected.append(rel)
    return selected


def resolve_probe(env: dict[str, str] | None = None) -> Path:
    """Return the configured probe path, which may not exist."""
    environ = os.environ if env is None else env
    raw = environ.get("UNO_PROBE") or str(DEFAULT_PROBE)
    return Path(raw).expanduser()


def main(argv: list[str] | None = None, repo_root: Path = REPO_ROOT) -> int:
    """CLI entry point for ``make test-bot``'s isolated UNO step."""
    parser = argparse.ArgumentParser(description="Run the branch's changed UNO tests via uno-probe.sh.")
    parser.add_argument(
        "--print-files",
        action="store_true",
        help="Print selected UNO test paths, one per line, and exit without running them.",
    )
    args = parser.parse_args(argv)

    if os.environ.get("SKIP_UNO") == "1":
        print("test-bot: SKIP_UNO=1 — skipping isolated UNO tests", flush=True)
        return 0

    if is_main_checkout(repo_root):
        print(
            "test-bot: skipping isolated UNO tests in the main checkout; use a worktree",
            flush=True,
        )
        return 0

    files = list_branch_uno_files(repo_root)
    if args.print_files:
        for rel in files:
            print(rel, flush=True)
        return 0

    if not files:
        print(
            "test-bot: no added or modified UNO tests vs merge-base; skipping isolated UNO tests",
            flush=True,
        )
        return 0

    probe = resolve_probe()
    if not probe.is_file() or not os.access(probe, os.X_OK):
        print(
            f"test-bot: uno-probe.sh not found at {probe}; skipping isolated UNO tests",
            flush=True,
        )
        return 0

    print(
        f"test-bot: running {len(files)} UNO file(s) via {probe}: {', '.join(files)}",
        flush=True,
    )
    cmd = [str(probe), *[str(repo_root / rel) for rel in files]]
    proc = subprocess.run(cmd, cwd=repo_root, check=False)
    if proc.returncode != 0:
        print(f"test-bot: UNO tests failed (exit {proc.returncode})", file=sys.stderr, flush=True)
        print("test-bot: UNO files in this run:", file=sys.stderr, flush=True)
        for rel in files:
            print(f"  {rel}", file=sys.stderr, flush=True)
        return int(proc.returncode) or 1
    print("test-bot: UNO tests passed", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
