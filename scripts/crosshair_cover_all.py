#!/usr/bin/env python3
# WriterAgent — long-running CrossHair cover of all deal-instrumented plugin modules
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Discover ``@deal.`` modules under ``plugin/`` and run CrossHair cover (bounded).

Runs modules in a process pool (default ``max(2, cpu_count - 2)`` workers) so wall
clock scales with cores. Each worker still owns one CrossHair process — an engine
crash in one module does not abort the rest. Formatted output is buffered per module
and printed as a whole block when that worker finishes (completion order; no interleaved
lines from concurrent runs).

Two presets only (no per-flag budget overrides):

- **regular** (default): ``--max_uninteresting_iterations=25`` and
  ``--per_condition_timeout=5`` (breadth over depth), plus a hard **120s**
  per-module wall kill so no file dominates the sweep.
- **deep** (``--deep``): ``--max_uninteresting_iterations=200``, no per-condition
  timeout and no wall — hour-scale exploration (CrossHair's "hundreds" guidance).

Failures are engine fatals / process crashes only — wall timeout and few examples
do not fail the sweep.

Modules are submitted via ``COVER_ALL_SCHEDULE_ORDER`` (measured regular-run
timings; longest-first among known modules). Paths **not** in that list run
**first** (stable by path) until they are timed and inserted into the schedule.
Completion banners still use finish order.

Uses ``CROSSHAIR_CHECK_ALL_SKIP`` plus cover-only ``CROSSHAIR_COVER_ALL_SKIP`` (UNO / drain loops).

Usage::

    make crosshair-cover-all
    make crosshair-cover-all-deep
    python scripts/crosshair_cover_all.py
    python scripts/crosshair_cover_all.py --deep
    python scripts/crosshair_cover_all.py --list
    python scripts/crosshair_cover_all.py --jobs 4
    python scripts/crosshair_cover_all.py plugin/scripting/payload_codec.py
"""

from __future__ import annotations

import argparse
import io
import json
import os
import sys
import time
from concurrent.futures import Future, ProcessPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TextIO

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from scripts.crosshair_check_all import CROSSHAIR_CHECK_ALL_SKIP
from scripts.crosshair_stream import _TeeTextIO, discover_deal_plugin_files, run_crosshair

DEFAULT_LOG = Path("build/crosshair-cover-all.log")
DEFAULT_TIMINGS_JSON = Path("build/crosshair-cover-all-timings.json")
# Regular: short per-condition slices so many callables get poked inside the wall.
REGULAR_MAX_UNINTERESTING = 25
REGULAR_PER_CONDITION_TIMEOUT_SEC = 5
# Hard stop per module in regular mode (backstop; soft bounds aim to finish earlier).
REGULAR_MODULE_WALL_TIMEOUT_SEC = 120
# Deep: CrossHair "hundreds" for multi-hour runs; iteration budget is the stop (no timeout).
DEEP_MAX_UNINTERESTING = 200
# Regular only: payload_codec has ~55 top-level callables; same 5s slice, fewer iters.
PAYLOAD_CODEC_REL = "plugin/scripting/payload_codec.py"
PAYLOAD_CODEC_REGULAR_MAX_UNINTERESTING = 5
PAYLOAD_CODEC_REGULAR_PER_CONDITION_TIMEOUT_SEC = 5
# Placeholder in worker-buffered text; parent replaces with [completed/total] on emit.
PROGRESS_SENTINEL = "__PROGRESS__"

# Cover walks all top-level callables (not only @deal). These are check-green but
# cover-hostile (UNO Tool.execute, sheet access, combobox, engine exit 2, drain loops,
# FSM/Exception/JSON surfaces that still crash-frame under cover after Literal→str).
CROSSHAIR_COVER_ALL_SKIP: frozenset[str] = frozenset(
    {
        "plugin/calc/cells.py",
        "plugin/calc/formula_dep_chain.py",
        "plugin/calc/calc_addin_data.py",
        "plugin/chatbot/chat_sidebar_mode.py",
        "plugin/chatbot/web_research_cache.py",
        "plugin/chatbot/state_machine.py",
        "plugin/chatbot/tool_loop_state.py",
        "plugin/framework/async_stream.py",
        # check-green; cover crash-frames / LazyIntSymbolicStr / engine exit 2
        # (cover ignores # crosshair: off on many of these entry points).
        "plugin/framework/client/auth.py",
        "plugin/framework/config.py",
        "plugin/framework/config_service.py",
        "plugin/framework/default_models.py",
        "plugin/framework/event_bus.py",
        "plugin/framework/i18n.py",
        "plugin/framework/tool.py",
        "plugin/framework/url_utils.py",
        "plugin/mcp/cors.py",
        # CalcRange materialize/pandas paths + payload_codec under symbolic grids.
        "plugin/scripting/calc_range.py",
        # json.dumps on symbolic template params → cover crash frame.
        "plugin/scripting/duckdb_sql.py",
        "plugin/scripting/editor_ipc.py",
        "plugin/scripting/helper_domain.py",  # cover engine exit 2
        "plugin/scripting/sandbox.py",
    }
)

# Longest → shortest submit order for the process pool (regular cover-all timings).
# Unknown @deal. modules (not in this list) sort *before* the schedule until timed
# and inserted here.
COVER_ALL_SCHEDULE_ORDER: tuple[str, ...] = (
    "plugin/calc/python/formula_edit.py",  # 711.6s
    "plugin/mcp/mcp_state.py",  # 478.5s
    "plugin/framework/client/stream_normalizer.py",  # 433.8s
    "plugin/chatbot/send_state.py",  # 275.5s
    "plugin/writer/word_diff_split.py",  # 216.7s
    "plugin/scripting/audio_silence_detector.py",  # 203.4s
    "plugin/calc/sheet_filter_criteria.py",  # 158.4s
    "plugin/scripting/payload_codec.py",  # 158.0s
    "plugin/scripting/sandbox_cache.py",  # 156.2s
    "plugin/writer/xhtml_style_postprocess.py",  # 145.1s
    "plugin/framework/client/response_normalizers.py",  # 136.5s
    "plugin/calc/address_utils.py",  # 126.9s
    "plugin/chatbot/audio_recorder_state.py",  # 86.0s
    "plugin/scripting/config_limits.py",  # 78.1s
    "plugin/framework/openrouter_model_id.py",  # 72.3s
    "plugin/framework/constants.py",  # 67.7s
    "plugin/framework/html_stripper.py",  # 64.3s
    "plugin/framework/ast_stmt_edit.py",  # 49.5s
    "plugin/calc/spreadsheet_import/preprocess.py",  # 38.0s
    "plugin/scripting/trusted_action_registry.py",  # 34.5s
    "plugin/scripting/trusted_rpc.py",  # 30.6s
    "plugin/scripting/import_policy.py",  # 17.2s
    "plugin/calc/excel_py_convert/resolve_refs.py",  # 13.8s
    "plugin/scripting/excel_xl.py",  # 8.2s
    "plugin/chatbot/research_cache_fluff.py",  # 0.5s
)
_COVER_ALL_SCHEDULE_RANK: dict[str, int] = {rel: i for i, rel in enumerate(COVER_ALL_SCHEDULE_ORDER)}


@dataclass(frozen=True)
class CoverBudget:
    """Resolved cover-all preset (regular or deep)."""

    mode: str  # "regular" | "deep"
    max_uninteresting: int
    per_condition_timeout: int | None  # None = omit flag (deep)


@dataclass(frozen=True)
class CoverModuleResult:
    """Picklable result from one cover worker (full formatted text, no live streaming)."""

    rel: str
    index: int
    total: int
    exit_code: int
    examples: int
    explore: int
    error_details: tuple[str, ...]
    formatted: str
    duration_sec: float


def resolve_cover_budget(*, deep: bool) -> CoverBudget:
    """Map --deep to CrossHair bound flags (only two presets)."""
    if deep:
        return CoverBudget(
            mode="deep",
            max_uninteresting=DEEP_MAX_UNINTERESTING,
            per_condition_timeout=None,
        )
    return CoverBudget(
        mode="regular",
        max_uninteresting=REGULAR_MAX_UNINTERESTING,
        per_condition_timeout=REGULAR_PER_CONDITION_TIMEOUT_SEC,
    )


def module_cover_bounds(budget: CoverBudget, rel: str) -> tuple[int, int | None]:
    """Per-module CrossHair bounds; regular mode tightens payload_codec only."""
    key = rel if rel.startswith("plugin/") else _schedule_key(Path(rel))
    if budget.mode == "regular" and key == PAYLOAD_CODEC_REL:
        return (
            PAYLOAD_CODEC_REGULAR_MAX_UNINTERESTING,
            PAYLOAD_CODEC_REGULAR_PER_CONDITION_TIMEOUT_SEC,
        )
    return budget.max_uninteresting, budget.per_condition_timeout


def default_cover_jobs() -> int:
    """Leave ~2 cores free so the desktop stays usable while the sweep runs.

    Never fewer than 2 workers (override with ``--jobs`` if you want all cores).
    """
    return max(2, (os.cpu_count() or 2) - 2)


def _posix_rel(path: Path) -> str:
    return path.as_posix()


def _schedule_key(path: Path) -> str:
    """Map a path to COVER_ALL_SCHEDULE_ORDER form (``plugin/...``), even under abs roots."""
    rel = _posix_rel(path)
    marker = "/plugin/"
    idx = rel.rfind(marker)
    if idx >= 0:
        return "plugin/" + rel[idx + len(marker) :]
    if rel.startswith("plugin/"):
        return rel
    return rel


def filter_cover_all_targets(files: list[Path], *, apply_skip: bool) -> tuple[list[Path], list[str]]:
    """Return (to_run, skipped_rels). Explicit CLI targets should pass apply_skip=False."""
    if not apply_skip:
        return files, []
    combined = CROSSHAIR_CHECK_ALL_SKIP | CROSSHAIR_COVER_ALL_SKIP
    to_run: list[Path] = []
    skipped: list[str] = []
    for path in files:
        # Skip list keys are repo-relative plugin/...; abs --plugin-root must still match.
        rel = _schedule_key(path)
        if rel in combined:
            skipped.append(rel)
        else:
            to_run.append(path)
    return to_run, skipped


def order_cover_targets(files: list[Path]) -> list[Path]:
    """Submit unknowns first, then known schedule ranks (longest-first).

    New ``@deal.`` modules are not in ``COVER_ALL_SCHEDULE_ORDER`` yet; running them
    early avoids parking a potentially slow first-time cover at the end of the pool.
    """

    def sort_key(path: Path) -> tuple[int, str]:
        key = _schedule_key(path)
        if key not in _COVER_ALL_SCHEDULE_RANK:
            # Rank -1: all unknowns before index 0 of the known schedule; alpha among unknowns.
            return (-1, key)
        return (_COVER_ALL_SCHEDULE_RANK[key], key)

    return sorted(files, key=sort_key)


def _cover_one_module(
    rel: str,
    index: int,
    total: int,
    *,
    quiet: bool,
    raw: bool,
    max_uninteresting: int,
    per_condition_timeout: int | None,
    wall_timeout_sec: float | None = None,
) -> CoverModuleResult:
    """Run CrossHair cover for one module; buffer all formatted output (pool worker entry)."""
    buf = io.StringIO()
    # Discovery index stays on CoverModuleResult; printed [n/total] is stamped on emit.
    section = f"######## {PROGRESS_SENTINEL} {rel} ########"
    buf.write(f"\n{section}\n")
    ch_args = [
        "-v",
        f"--max_uninteresting_iterations={max_uninteresting}",
    ]
    if per_condition_timeout is not None:
        ch_args.append(f"--per_condition_timeout={per_condition_timeout}")
    ch_args.append(rel)
    # Same path markers at top/bottom; parent renumbers PROGRESS_SENTINEL to completion order.
    started = time.perf_counter()
    code, stats = run_crosshair(
        "cover",
        ch_args,
        "cover",
        raw,
        quiet,
        out=buf,
        label=f"{PROGRESS_SENTINEL} {rel}",
        timeout_sec=wall_timeout_sec,
    )
    duration_sec = time.perf_counter() - started
    if code != 0:
        buf.write(f"[COVER FATAL           ] module failed: {rel} (exit {code})\n")
    buf.write(f"[COVER TIMING          ] {duration_sec:.1f}s  {rel}\n")
    buf.write(f"{section}\n")
    return CoverModuleResult(
        rel=rel,
        index=index,
        total=total,
        exit_code=code,
        examples=stats.examples,
        explore=stats.explore,
        error_details=tuple(stats.error_details or ()),
        formatted=buf.getvalue(),
        duration_sec=duration_sec,
    )


def emit_cover_module_result(out: TextIO, result: CoverModuleResult, *, completed: int) -> None:
    """Write one finished module block; stamp [completed/total] over the progress sentinel."""
    progress = f"[{completed}/{result.total}]"
    out.write(result.formatted.replace(PROGRESS_SENTINEL, progress))
    out.flush()


def build_timings_payload(
    *,
    mode: str,
    jobs: int,
    wall_sec: float,
    max_uninteresting: int,
    per_condition_timeout: int | None,
    results: list[CoverModuleResult],
) -> dict[str, object]:
    """JSON-serializable timings ranked longest-first for later scheduling."""
    ranked = sorted(results, key=lambda r: r.duration_sec, reverse=True)
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "mode": mode,
        "jobs": jobs,
        "wall_sec": round(wall_sec, 3),
        "max_uninteresting_iterations": max_uninteresting,
        "per_condition_timeout": per_condition_timeout,
        "modules": [
            {
                "rel": r.rel,
                "duration_sec": round(r.duration_sec, 3),
                "exit_code": r.exit_code,
                "examples": r.examples,
                "explore": r.explore,
            }
            for r in ranked
        ],
    }


def _write_cover_all_summary(
    tee: TextIO,
    *,
    modules: int,
    skipped: list[str],
    failed: list[tuple[str, list[str]]],
    total_examples: int,
    total_explore: int,
    results: list[CoverModuleResult],
    wall_sec: float,
    mode: str,
) -> None:
    tee.write("\n=== cover-all summary ===\n")
    tee.write(f"  mode:    {mode}\n")
    tee.write(f"  modules: {modules}\n")
    tee.write(f"  skipped: {len(skipped)}\n")
    tee.write(f"  failed:  {len(failed)}\n")
    tee.write(f"  wall:    {wall_sec:.1f}s\n")
    tee.write(f"  examples (aggregate): {total_examples}\n")
    tee.write(f"  explore (aggregate):  {total_explore}\n")
    if results:
        tee.write("\n=== TIMINGS (longest first) ===\n")
        for r in sorted(results, key=lambda x: x.duration_sec, reverse=True):
            tee.write(f"  {r.duration_sec:8.1f}s  exit={r.exit_code}  {r.rel}\n")
    if skipped:
        tee.write("\n=== SKIPPED (engine/UNO-hostile) ===\n")
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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="CrossHair cover all deal-instrumented plugin modules")
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
    parser.add_argument(
        "--timings-json",
        type=Path,
        default=DEFAULT_TIMINGS_JSON,
        help=f"Write per-module durations here (default: {DEFAULT_TIMINGS_JSON})",
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
        "--jobs",
        type=int,
        default=None,
        metavar="N",
        help=f"Process-pool workers (default: max(2, cpu_count-2) = {default_cover_jobs()})",
    )
    parser.add_argument(
        "--fail-fast",
        action="store_true",
        help="Stop after the first module that fails (default: continue and summarize)",
    )
    parser.add_argument(
        "--include-skipped",
        action="store_true",
        help="Also analyze CHECK_ALL + COVER_ALL skip modules (engine-crash / UNO hosts)",
    )
    args = parser.parse_args(argv)

    budget = resolve_cover_budget(deep=args.deep)

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
    files, skipped = filter_cover_all_targets(files, apply_skip=apply_skip)
    files = order_cover_targets(files)
    if not files and not skipped:
        print(f"No @deal. modules under {args.plugin_root}", file=sys.stderr)
        return 2

    jobs = args.jobs if args.jobs is not None else default_cover_jobs()
    if jobs < 1:
        print("--jobs must be >= 1", file=sys.stderr)
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
        f"CrossHair cover-all [{budget.mode}]: {len(rels)} module(s), {jobs} worker(s) "
        f"(process pool; max_uninteresting={budget.max_uninteresting}, "
        f"per_condition_timeout={timeout_desc}, module_wall={wall_desc})",
        flush=True,
    )
    for rel in rels:
        print(f"  {rel}", flush=True)
    if skipped:
        print(f"Skipped (engine/UNO-hostile; pass path or --include-skipped to force): {len(skipped)}", flush=True)
        for rel in skipped:
            print(f"  SKIP {rel}", flush=True)
    if args.list:
        return 0
    if not files:
        print("Nothing to analyze after skip filter.", file=sys.stderr)
        return 0

    args.log.parent.mkdir(parents=True, exist_ok=True)
    print(f"Logging to {args.log}; timings -> {args.timings_json}", flush=True)

    failed: list[tuple[str, list[str]]] = []
    results: list[CoverModuleResult] = []
    total_examples = 0
    total_explore = 0
    total = len(files)
    # One CrossHair per module still; pool only parallelizes which modules run.
    # Buffer per worker, emit whole blocks on completion so stdout/log never interleave.
    wall_started = time.perf_counter()
    with args.log.open("w", encoding="utf-8") as log_fp:
        tee = _TeeTextIO(sys.stdout, log_fp)
        with ProcessPoolExecutor(max_workers=jobs) as executor:
            futures: dict[Future[CoverModuleResult], str] = {}
            for index, path in enumerate(files, start=1):
                rel = _posix_rel(path)
                max_uninteresting, per_condition_timeout = module_cover_bounds(
                    budget, _schedule_key(path)
                )
                wall_timeout = (
                    float(REGULAR_MODULE_WALL_TIMEOUT_SEC) if budget.mode == "regular" else None
                )
                fut = executor.submit(
                    _cover_one_module,
                    rel,
                    index,
                    total,
                    quiet=args.quiet,
                    raw=args.raw,
                    max_uninteresting=max_uninteresting,
                    per_condition_timeout=per_condition_timeout,
                    wall_timeout_sec=wall_timeout,
                )
                futures[fut] = rel
            completed = 0
            for fut in as_completed(futures):
                rel = futures[fut]
                try:
                    result = fut.result()
                except Exception as exc:
                    # Keep sweep isolation: a broken worker must not abort siblings.
                    detail = f"worker exception: {exc}"
                    result = CoverModuleResult(
                        rel=rel,
                        index=0,
                        total=total,
                        exit_code=1,
                        examples=0,
                        explore=0,
                        error_details=(detail,),
                        formatted=(
                            f"\n######## {PROGRESS_SENTINEL} {rel} ########\n"
                            f"[COVER FATAL           ] {detail}\n"
                            f"######## {PROGRESS_SENTINEL} {rel} ########\n"
                        ),
                        duration_sec=0.0,
                    )
                completed += 1
                results.append(result)
                emit_cover_module_result(tee, result, completed=completed)
                total_examples += result.examples
                total_explore += result.explore
                if result.exit_code != 0:
                    failed.append((result.rel, list(result.error_details)))
                    if args.fail_fast:
                        for pending in futures:
                            pending.cancel()
                        break

        wall_sec = time.perf_counter() - wall_started
        _write_cover_all_summary(
            tee,
            modules=total,
            skipped=skipped,
            failed=failed,
            total_examples=total_examples,
            total_explore=total_explore,
            results=results,
            wall_sec=wall_sec,
            mode=budget.mode,
        )
        payload = build_timings_payload(
            mode=budget.mode,
            jobs=jobs,
            wall_sec=wall_sec,
            max_uninteresting=budget.max_uninteresting,
            per_condition_timeout=budget.per_condition_timeout,
            results=results,
        )
        args.timings_json.parent.mkdir(parents=True, exist_ok=True)
        args.timings_json.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        tee.write(f"\nWrote timings: {args.timings_json}\n")
        tee.flush()

    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
