#!/usr/bin/env python3
# WriterAgent — long-running CrossHair check of all deal-instrumented plugin modules
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Discover ``@deal.`` modules under ``plugin/`` and run CrossHair check (budgeted).

Runs **one FQN at a time** (same crash isolation as cover-all) and prints
``[CHECK START]`` *before* each spawn so a hang shows the current callable.
Errors are printed live and reprinted in a final ``ERRORS TO FIX`` / failed-module summary.

Two presets only (same numbers as cover-all; no per-flag budget overrides):

- **regular** (default): ``--max_uninteresting_iterations=25`` and
  ``--per_condition_timeout=5`` (breadth over depth), plus a hard **120s**
  per-module wall kill so no file dominates the sweep.
- **deep** (``--deep``): ``--max_uninteresting_iterations=200``, no per-condition
  timeout and no wall — hour-scale exploration (CrossHair's "hundreds" guidance).
  Speed comes from the short ``WRITERAGENT_CROSSHAIR=1`` deal table, not timeouts.

Wall timeout is exit 0 / not a sweep failure (budget exhaustion). Contract
``: error:`` lines and engine crashes still fail the sweep.

Usage::

    make crosshair-check-all
    make crosshair-check-all-deep
    python scripts/crosshair_check_all.py
    python scripts/crosshair_check_all.py --deep
    python scripts/crosshair_check_all.py --list
    python scripts/crosshair_check_all.py plugin/scripting/payload_codec.py
"""

from __future__ import annotations

import argparse
import sys
import time
from dataclasses import dataclass
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from scripts.crosshair_stream import (
    _TeeTextIO,
    cover_fqns_for_module,
    discover_deal_plugin_files,
    enable_crosshair_deal_table,
    run_crosshair,
)

DEFAULT_LOG = Path("build/crosshair-check-all.log")
# Regular: short per-condition slices so many callables get poked inside the wall.
REGULAR_MAX_UNINTERESTING = 25
REGULAR_PER_CONDITION_TIMEOUT_SEC = 5
# Hard stop per module in regular mode (backstop; soft bounds aim to finish earlier).
REGULAR_MODULE_WALL_TIMEOUT_SEC = 120
# Deep: CrossHair "hundreds" for multi-hour runs. No per-condition timeout, no module wall.
DEEP_MAX_UNINTERESTING = 200
# Regular only: payload_codec has many contracts; same 5s slice, fewer iters.
PAYLOAD_CODEC_REL = "plugin/scripting/payload_codec.py"
PAYLOAD_CODEC_REGULAR_MAX_UNINTERESTING = 5
PAYLOAD_CODEC_REGULAR_PER_CONDITION_TIMEOUT_SEC = 5

# Modules where CrossHair crashes (CrossHairInternal / UNO proxies / symbolic json.loads)
# rather than finding a contract counterexample. Prefer ``# crosshair: off`` on the
# hostile callable (and/or a ``sys.modules`` CrossHair shim) over module skips.
# Kept empty: cover-all / check-all re-enable every @deal. module.
CROSSHAIR_CHECK_ALL_SKIP: frozenset[str] = frozenset()


@dataclass(frozen=True)
class CheckBudget:
    """Resolved check-all preset (regular or deep)."""

    mode: str  # "regular" | "deep"
    max_uninteresting: int
    per_condition_timeout: int | None  # None = omit flag (deep)


def resolve_check_budget(*, deep: bool) -> CheckBudget:
    """Map --deep to CrossHair bound flags (only two presets)."""
    if deep:
        return CheckBudget(
            mode="deep",
            max_uninteresting=DEEP_MAX_UNINTERESTING,
            per_condition_timeout=None,
        )
    return CheckBudget(
        mode="regular",
        max_uninteresting=REGULAR_MAX_UNINTERESTING,
        per_condition_timeout=REGULAR_PER_CONDITION_TIMEOUT_SEC,
    )


def _posix_rel(path: Path) -> str:
    return path.as_posix()


def _schedule_key(path: Path) -> str:
    """Map a path to ``plugin/...`` form even under abs --plugin-root."""
    rel = _posix_rel(path)
    marker = "/plugin/"
    idx = rel.rfind(marker)
    if idx >= 0:
        return "plugin/" + rel[idx + len(marker) :]
    if rel.startswith("plugin/"):
        return rel
    return rel


def module_check_bounds(budget: CheckBudget, rel: str) -> tuple[int, int | None]:
    """Per-module CrossHair bounds; regular mode tightens payload_codec only."""
    key = rel if rel.startswith("plugin/") else _schedule_key(Path(rel))
    if budget.mode == "regular" and key == PAYLOAD_CODEC_REL:
        return (
            PAYLOAD_CODEC_REGULAR_MAX_UNINTERESTING,
            PAYLOAD_CODEC_REGULAR_PER_CONDITION_TIMEOUT_SEC,
        )
    return budget.max_uninteresting, budget.per_condition_timeout


def filter_check_all_targets(files: list[Path], *, apply_skip: bool) -> tuple[list[Path], list[str]]:
    """Return (to_run, skipped_rels). Explicit CLI targets should pass apply_skip=False."""
    if not apply_skip:
        return files, []
    to_run: list[Path] = []
    skipped: list[str] = []
    for path in files:
        rel = _posix_rel(path)
        if rel in CROSSHAIR_CHECK_ALL_SKIP:
            skipped.append(rel)
        else:
            to_run.append(path)
    return to_run, skipped


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="CrossHair check all deal-instrumented plugin modules")
    parser.add_argument(
        "targets",
        nargs="*",
        help="Optional file paths (default: every plugin/**/*.py containing @deal.)",
    )
    parser.add_argument(
        "--plugin-root",
        type=Path,
        default=Path("plugin"),
        help="Plugin tree to scan when no targets given (default: plugin)",
    )
    parser.add_argument(
        "--log",
        type=Path,
        default=DEFAULT_LOG,
        help=f"Tee formatted output here (default: {DEFAULT_LOG})",
    )
    parser.add_argument("--list", action="store_true", help="Print discovered files and exit")
    parser.add_argument("-q", "--quiet", action="store_true", help="Only errors/fatals and final banner")
    parser.add_argument("--raw", action="store_true", help="Also print suppressed CrossHair -v spam")
    parser.add_argument(
        "--deep",
        action="store_true",
        help=(
            f"Deep mode: max_uninteresting_iterations={DEEP_MAX_UNINTERESTING}, "
            "no per_condition_timeout (default regular: "
            f"{REGULAR_MAX_UNINTERESTING} iters / {REGULAR_PER_CONDITION_TIMEOUT_SEC}s)"
        ),
    )
    parser.add_argument(
        "--fail-fast",
        action="store_true",
        help="Stop after the first module that fails (default: continue and summarize)",
    )
    parser.add_argument(
        "--include-skipped",
        action="store_true",
        help="Also analyze CROSSHAIR_CHECK_ALL_SKIP modules (engine-crash hosts)",
    )
    args = parser.parse_args(argv)

    # Child CrossHair processes import deal_shim; set this before any spawn so
    # they bind the short @deal.pre table. Pytest / make test never set it.
    enable_crosshair_deal_table()

    budget = resolve_check_budget(deep=args.deep)

    explicit = bool(args.targets)
    if args.targets:
        files = [Path(t) for t in args.targets]
        missing = [p for p in files if not p.is_file()]
        if missing:
            print("Missing targets: " + ", ".join(str(p) for p in missing), file=sys.stderr)
            return 2
    else:
        files = discover_deal_plugin_files(args.plugin_root)

    apply_skip = not explicit and not args.include_skipped
    files, skipped = filter_check_all_targets(files, apply_skip=apply_skip)
    if not files and not skipped:
        print(f"No @deal. modules under {args.plugin_root}", file=sys.stderr)
        return 2

    rels = [_posix_rel(p) for p in files]
    timeout_desc = (
        "none" if budget.per_condition_timeout is None else f"{budget.per_condition_timeout}s"
    )
    wall_desc = (
        f"{REGULAR_MODULE_WALL_TIMEOUT_SEC}s"
        if budget.mode == "regular"
        else "none"
    )
    print(
        f"CrossHair check-all [{budget.mode}]: {len(rels)} module(s), one CrossHair process per FQN "
        f"(max_uninteresting={budget.max_uninteresting}, "
        f"per_condition_timeout={timeout_desc}, module_wall={wall_desc})",
        flush=True,
    )
    for rel in rels:
        print(f"  {rel}", flush=True)
    if skipped:
        print(f"Skipped (engine-hostile; pass path or --include-skipped to force): {len(skipped)}", flush=True)
        for rel in skipped:
            print(f"  SKIP {rel}", flush=True)
    if args.list:
        return 0
    if not files:
        print("Nothing to analyze after skip filter.", file=sys.stderr)
        return 0

    args.log.parent.mkdir(parents=True, exist_ok=True)
    print(f"Logging to {args.log}", flush=True)

    failed: list[tuple[str, list[str]]] = []
    with args.log.open("w", encoding="utf-8") as log_fp:
        tee = _TeeTextIO(sys.stdout, log_fp)
        for index, path in enumerate(files, start=1):
            rel = str(path)
            tee.write(f"\n######## [{index}/{len(files)}] {rel} ########\n")
            tee.flush()
            fqns = cover_fqns_for_module(path, require_deal=True)
            if not fqns:
                tee.write(f"[CHECK SKIP            ] all callables off: {rel}\n")
                tee.flush()
                continue
            max_uninteresting, per_condition_timeout = module_check_bounds(budget, rel)
            wall = (
                float(REGULAR_MODULE_WALL_TIMEOUT_SEC)
                if budget.mode == "regular"
                else None
            )
            started = time.perf_counter()
            deadline = (started + wall) if wall and wall > 0 else None
            code = 0
            details: list[str] = []
            for fqn in fqns:
                remaining = None
                if deadline is not None:
                    remaining = deadline - time.perf_counter()
                    if remaining <= 0:
                        tee.write(f"[CHECK TIMEOUT         ] wall {wall:g}s exceeded for {rel}\n")
                        tee.flush()
                        break
                ch_args = [
                    "-v",
                    f"--max_uninteresting_iterations={max_uninteresting}",
                ]
                if per_condition_timeout is not None:
                    ch_args.append(f"--per_condition_timeout={per_condition_timeout}")
                ch_args.extend(["--report_all", "--analysis_kind=deal", fqn])
                fqn_code, stats = run_crosshair(
                    "check",
                    ch_args,
                    "check",
                    args.raw,
                    args.quiet,
                    out=tee,
                    label=f"{rel} :: {fqn}",
                    timeout_sec=remaining,
                )
                if stats.error_details:
                    details.extend(stats.error_details)
                if fqn_code != 0:
                    code = fqn_code
            if code != 0:
                failed.append((rel, details))
                tee.write(f"[CHECK ERROR           ] module failed: {rel} (exit {code})\n")
                tee.flush()
                if args.fail_fast:
                    break

        tee.write("\n=== check-all summary ===\n")
        tee.write(f"  modules: {len(files)}\n")
        tee.write(f"  skipped: {len(skipped)}\n")
        tee.write(f"  failed:  {len(failed)}\n")
        if skipped:
            tee.write("\n=== SKIPPED (engine-hostile) ===\n")
            for rel in skipped:
                tee.write(f"  * {rel}\n")
        if failed:
            tee.write("\n=== ERRORS TO FIX (by module) ===\n")
            for rel, details in failed:
                tee.write(f"  * {rel}\n")
                if details:
                    for detail in details:
                        tee.write(f"      - {detail}\n")
                else:
                    tee.write("      - (no classified details; re-run this file with --raw)\n")
        tee.flush()

    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
