#!/usr/bin/env python3
# WriterAgent — long-running CrossHair cover of all deal-instrumented plugin modules
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Discover ``@deal.`` modules under ``plugin/`` and run CrossHair cover (bounded).

Runs **one file at a time** so a CrossHair engine crash in one module does not abort the rest.
Failures are engine fatals / process crashes only — few examples do not fail the sweep.

Uses ``CROSSHAIR_CHECK_ALL_SKIP`` plus cover-only ``CROSSHAIR_COVER_ALL_SKIP`` (UNO / drain loops).
Every file is bounded with ``--max_uninteresting_iterations=25`` and
``--per_condition_timeout=60`` so cover cannot hang on infinite loops or SMT spam
(unlike check-all, which has no iteration budget).

Usage::

    make crosshair-cover-all
    python scripts/crosshair_cover_all.py
    python scripts/crosshair_cover_all.py --list
    python scripts/crosshair_cover_all.py plugin/scripting/payload_codec.py
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from scripts.crosshair_check_all import CROSSHAIR_CHECK_ALL_SKIP
from scripts.crosshair_stream import _TeeTextIO, discover_deal_plugin_files, run_crosshair

DEFAULT_LOG = Path("build/crosshair-cover-all.log")
# Bound per function so drain loops / hostile callables cannot spin forever.
# Iteration budget alone is not enough: SMT "examples" can look like progress forever.
MAX_UNINTERESTING_ITERATIONS = 25
PER_CONDITION_TIMEOUT_SEC = 60

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


def _posix_rel(path: Path) -> str:
    return path.as_posix()


def filter_cover_all_targets(files: list[Path], *, apply_skip: bool) -> tuple[list[Path], list[str]]:
    """Return (to_run, skipped_rels). Explicit CLI targets should pass apply_skip=False."""
    if not apply_skip:
        return files, []
    combined = CROSSHAIR_CHECK_ALL_SKIP | CROSSHAIR_COVER_ALL_SKIP
    to_run: list[Path] = []
    skipped: list[str] = []
    for path in files:
        rel = _posix_rel(path)
        if rel in combined:
            skipped.append(rel)
        else:
            to_run.append(path)
    return to_run, skipped


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
    parser.add_argument("--list", action="store_true", help="Print discovered files and exit")
    parser.add_argument("-q", "--quiet", action="store_true", help="Only errors/fatals and final banner")
    parser.add_argument("--raw", action="store_true", help="Also print suppressed CrossHair -v spam")
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
    if not files and not skipped:
        print(f"No @deal. modules under {args.plugin_root}", file=sys.stderr)
        return 2

    rels = [_posix_rel(p) for p in files]
    print(f"CrossHair cover-all: {len(rels)} module(s), one CrossHair process per file", flush=True)
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
    print(
        f"Logging to {args.log} "
        f"(max_uninteresting_iterations={MAX_UNINTERESTING_ITERATIONS}, "
        f"per_condition_timeout={PER_CONDITION_TIMEOUT_SEC}s)",
        flush=True,
    )

    failed: list[tuple[str, list[str]]] = []
    total_examples = 0
    total_explore = 0
    with args.log.open("w", encoding="utf-8") as log_fp:
        tee = _TeeTextIO(sys.stdout, log_fp)
        for index, path in enumerate(files, start=1):
            rel = str(path)
            tee.write(f"\n######## [{index}/{len(files)}] {rel} ########\n")
            tee.flush()
            ch_args = [
                "-v",
                f"--max_uninteresting_iterations={MAX_UNINTERESTING_ITERATIONS}",
                f"--per_condition_timeout={PER_CONDITION_TIMEOUT_SEC}",
                rel,
            ]
            code, stats = run_crosshair("cover", ch_args, "cover", args.raw, args.quiet, out=tee)
            total_examples += stats.examples
            total_explore += stats.explore
            if code != 0:
                details = list(stats.error_details or [])
                failed.append((rel, details))
                tee.write(f"[COVER FATAL           ] module failed: {rel} (exit {code})\n")
                tee.flush()
                if args.fail_fast:
                    break

        tee.write("\n=== cover-all summary ===\n")
        tee.write(f"  modules: {len(files)}\n")
        tee.write(f"  skipped: {len(skipped)}\n")
        tee.write(f"  failed:  {len(failed)}\n")
        tee.write(f"  examples (aggregate): {total_examples}\n")
        tee.write(f"  explore (aggregate):  {total_explore}\n")
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

    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
