#!/usr/bin/env python3
# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Run the branch's selected UNO tests through isolated uno-probe.sh.

Used by ``make test-bot`` after incremental pytest. Collects files changed
relative to the merge-base with ``origin/master`` (committed, staged, unstaged,
and untracked) and selects matching ``tests/**/test_*_uno.py`` files via
``select_uno_files`` (direct UNO test edits, stem-matched UNO tests, UNO tests
naming changed modules, and transitive importers), excluding user-profile and
demo-only suites. Invokes ``UNO_PROBE`` (default
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

from scripts.test_incremental import (  # noqa: E402
    get_branch_changed_files,
    select_uno_files,
)

DEFAULT_PROBE = Path.home() / "Desktop/Python/wa-pipeline-scripts/uno-probe.sh"
MAIN_CHECKOUT = Path.home() / "Desktop/Python/writeragent"


def is_main_checkout(repo_root: Path, main: Path = MAIN_CHECKOUT) -> bool:
    """True when ``repo_root`` is the main WriterAgent checkout, not a worktree."""
    try:
        return repo_root.resolve() == main.resolve()
    except OSError:
        return False


def list_branch_uno_files(repo_root: Path) -> list[str]:
    """Return eligible UNO test files for this branch's changes."""
    changed = get_branch_changed_files(repo_root)
    return select_uno_files(changed, repo_root)


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
            "test-bot: no matching UNO tests vs merge-base; skipping isolated UNO tests",
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
