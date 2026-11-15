#!/usr/bin/env python3
# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Sequential A/B prompt evaluation harness comparing origin/master (A) vs working tree (B).

Runs one ``run_eval_multi.py`` invocation at a time in the foreground with safe
memory settings (``-j 1 --lo-workers 2 --backend auto``), storing resumable
per-repeat JSON results under ``/tmp/wa-eval-ab/<suite>/``.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent.parent
for path in (REPO_ROOT, SCRIPT_DIR):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from dataset import ALL_EXAMPLES, parse_task_id_filter, task_kind  # noqa: E402
from run_eval_multi import details_path_for, parse_model_ids  # noqa: E402

DEFAULT_MODELS = "openai/gpt-oss-20b:nitro,openai/gpt-oss-120b:nitro"
DEFAULT_REPEATS = 3
DEFAULT_OUT_ROOT = Path(tempfile.gettempdir()) / "wa-eval-ab"
DEFAULT_WORKTREE_DIR = Path(tempfile.gettempdir()) / "wa-eval-ab-master"
SUITE_NAMES = ("all", "calc", "writer", "draw", "string", "lo")


def parse_models(spec: str) -> list[str]:
    """Parse model IDs from a comma-separated string, resolving aliases."""
    models = list(dict.fromkeys(parse_model_ids(spec))) if spec.strip() else []
    if not models:
        raise ValueError("MODELS must not be empty.")
    return models


def resolve_suite_tasks(suite: str, tasks_override: str | None = None) -> list[str]:
    """Return task_ids for ``suite``, or validate ``tasks_override`` if given."""
    override = parse_task_id_filter(tasks_override)
    if override:
        known_ids = {ex["task_id"] for ex in ALL_EXAMPLES}
        deduped = list(dict.fromkeys(override))
        unknown = [tid for tid in deduped if tid not in known_ids]
        if unknown:
            raise ValueError(f"Unknown task_id(s): {', '.join(unknown)}")
        return deduped

    key = suite.strip().lower()
    if key == "all":
        return [ex["task_id"] for ex in ALL_EXAMPLES]
    if key in ("calc", "writer", "draw"):
        return [ex["task_id"] for ex in ALL_EXAMPLES if task_kind(ex["task_id"]) == key]
    if key in ("string", "lo"):
        return [
            ex["task_id"]
            for ex in ALL_EXAMPLES
            if ex.get("backend", "string") == key
        ]
    raise ValueError(
        f"Unknown suite {suite!r}; expected one of: {', '.join(SUITE_NAMES)}"
    )


def result_paths(suite_dir: Path, side: str, repeat_idx: int) -> tuple[Path, Path]:
    """Return ``(summary_json, details_json)`` paths for one side and repeat."""
    summary_path = suite_dir / f"{side}-r{repeat_idx}.json"
    return summary_path, details_path_for(summary_path)


def is_repeat_done(suite_dir: Path, side: str, repeat_idx: int) -> bool:
    """Return True if both summary and details JSON exist and parse cleanly."""
    summary_path, details_path = result_paths(suite_dir, side, repeat_idx)
    if not summary_path.is_file() or not details_path.is_file():
        return False
    if summary_path.stat().st_size == 0 or details_path.stat().st_size == 0:
        return False
    try:
        summary_data = json.loads(summary_path.read_text(encoding="utf-8"))
        details_data = json.loads(details_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return isinstance(summary_data, list) and isinstance(details_data, list)


def _git_common_dir(path: Path) -> Path | None:
    """Return resolved git common dir for ``path``, or None if not a git tree."""
    res = subprocess.run(
        ["git", "-C", str(path), "rev-parse", "--git-common-dir"],
        capture_output=True,
        text=True,
        check=False,
    )
    if res.returncode != 0:
        return None
    raw = res.stdout.strip()
    if not raw:
        return None
    p = Path(raw)
    return (p if p.is_absolute() else (path / p)).resolve()


def ensure_side_a_worktree(
    repo_root: Path,
    worktree_dir: Path,
    ref: str = "origin/master",
) -> Path:
    """Ensure ``worktree_dir`` is a detached worktree at ``ref``, reusing if valid."""
    repo_common = _git_common_dir(repo_root)
    if repo_common is None:
        raise RuntimeError(f"Not a git repository: {repo_root}")

    if worktree_dir.exists() and _git_common_dir(worktree_dir) == repo_common:
        subprocess.run(
            ["git", "-C", str(worktree_dir), "checkout", "--detach", "--force", ref],
            check=True,
        )
        return worktree_dir

    worktree_dir.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["git", "-C", str(repo_root), "worktree", "add", "--detach", str(worktree_dir), ref],
        check=True,
    )
    return worktree_dir


def run_generate_manifest(root: Path) -> None:
    """Run ``scripts/generate_manifest.py`` in ``root`` using the active Python."""
    subprocess.run(
        [sys.executable, str(root / "scripts" / "generate_manifest.py")],
        cwd=str(root),
        check=True,
    )


def run_one_eval(
    root: Path,
    *,
    models: list[str],
    tasks: list[str],
    out_path: Path,
    jobs: int = 1,
    lo_workers: int = 2,
    backend: str = "auto",
) -> None:
    """Run one ``run_eval_multi.py`` invocation strictly in the foreground."""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        sys.executable,
        str(root / "scripts" / "prompt_optimization" / "run_eval_multi.py"),
        "--models",
        ",".join(models),
        "--example",
        ",".join(tasks),
        "--repeats",
        "1",
        "-j",
        str(jobs),
        "--lo-workers",
        str(lo_workers),
        "--backend",
        backend,
        "--out",
        str(out_path),
    ]
    subprocess.run(cmd, cwd=str(root), check=True)


def load_side_details(suite_dir: Path, side: str, repeats: int) -> list[dict[str, Any]]:
    """Load and concatenate ``_details.json`` rows for repeats ``1..repeats``."""
    rows: list[dict[str, Any]] = []
    for rep in range(1, repeats + 1):
        unused, details_path = result_paths(suite_dir, side, rep)
        if not details_path.is_file():
            continue
        data = json.loads(details_path.read_text(encoding="utf-8"))
        if isinstance(data, list):
            rows.extend(item for item in data if isinstance(item, dict))
    return rows


def _summarize_rows(rows: list[dict[str, Any]]) -> tuple[int, int, float, float | None]:
    """Return ``(passes, total, pass_rate, mean_quality)`` for detail rows."""
    total = len(rows)
    if total == 0:
        return 0, 0, 0.0, None
    passes = sum(1 for row in rows if bool(row.get("hard_pass")))
    qualities = [
        float(row["judge_score"])
        for row in rows
        if row.get("judge_score") is not None
    ]
    mean_q = sum(qualities) / len(qualities) if qualities else None
    return passes, total, passes / total, mean_q


def _group_by_key(
    details: list[dict[str, Any]],
) -> dict[tuple[str, str], list[dict[str, Any]]]:
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for row in details:
        key = (str(row.get("model_id") or ""), str(row.get("task_id") or ""))
        grouped.setdefault(key, []).append(row)
    return grouped


def _side_fields(prefix: str, rows: list[dict[str, Any]]) -> dict[str, Any]:
    passes, total, rate, qual = _summarize_rows(rows)
    return {
        f"{prefix}_pass": passes,
        f"{prefix}_total": total,
        f"{prefix}_pass_rate": rate,
        f"{prefix}_quality": qual,
    }


def _compare_metric(a_val: float | None, b_val: float | None, tol: float) -> str | None:
    if a_val is None or b_val is None:
        return None
    if b_val - a_val > tol:
        return "B better"
    if a_val - b_val > tol:
        return "B worse"
    return None


def tally_ab_results(
    a_details: list[dict[str, Any]],
    b_details: list[dict[str, Any]],
    models: list[str],
    tasks: list[str],
) -> dict[str, Any]:
    """Aggregate per-model, per-task, and overall A vs B metrics and verdict."""
    a_by_key = _group_by_key(a_details)
    b_by_key = _group_by_key(b_details)

    rows_out = [
        {
            "model_id": model_id,
            "task_id": task_id,
            **_side_fields("a", a_by_key.get((model_id, task_id), [])),
            **_side_fields("b", b_by_key.get((model_id, task_id), [])),
        }
        for model_id in models
        for task_id in tasks
    ]

    overall = {
        **_side_fields("a", a_details),
        **_side_fields("b", b_details),
    }
    verdict = (
        _compare_metric(overall["a_pass_rate"], overall["b_pass_rate"], 1e-9)
        or _compare_metric(overall["a_quality"], overall["b_quality"], 1e-4)
        or "B same"
    )

    return {
        "rows": rows_out,
        "overall": overall,
        "verdict": verdict,
    }


def _fmt_pass(passes: int, total: int, rate: float) -> str:
    if total == 0:
        return "n/a"
    return f"{passes}/{total} ({rate * 100:.0f}%)"


def _fmt_qual(val: float | None) -> str:
    if val is None:
        return "n/a"
    return f"{val:.3f}"


def format_ab_report(tally: dict[str, Any], *, suite: str, repeats: int) -> str:
    """Format the per-task A/B comparison table and short verdict line."""
    rows = tally["rows"]
    overall = tally["overall"]
    verdict = tally["verdict"]

    model_w = max([len("Model"), *(len(r["model_id"]) for r in rows)], default=12)
    task_w = max([len("Task"), *(len(r["task_id"]) for r in rows)], default=12)

    header = (
        f"{'Model':<{model_w}}  {'Task':<{task_w}}  "
        f"{'A Pass':<11}  {'B Pass':<11}  {'A Qual':>6}  {'B Qual':>6}"
    )
    sep = "-" * len(header)
    lines = [
        f"Eval A/B (suite={suite}, repeats={repeats}, A=origin/master, B=working-tree)",
        sep,
        header,
        sep,
    ]
    for r in rows:
        lines.append(
            f"{r['model_id']:<{model_w}}  {r['task_id']:<{task_w}}  "
            f"{_fmt_pass(r['a_pass'], r['a_total'], r['a_pass_rate']):<11}  "
            f"{_fmt_pass(r['b_pass'], r['b_total'], r['b_pass_rate']):<11}  "
            f"{_fmt_qual(r['a_quality']):>6}  "
            f"{_fmt_qual(r['b_quality']):>6}"
        )
    lines.append(sep)
    a_pass_s = _fmt_pass(overall["a_pass"], overall["a_total"], overall["a_pass_rate"])
    b_pass_s = _fmt_pass(overall["b_pass"], overall["b_total"], overall["b_pass_rate"])
    a_q_s = _fmt_qual(overall["a_quality"])
    b_q_s = _fmt_qual(overall["b_quality"])
    lines.append(
        f"{verdict}  (pass: A={a_pass_s} vs B={b_pass_s}; quality: A={a_q_s} vs B={b_q_s})"
    )
    return "\n".join(lines)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Run sequential A/B prompt eval (origin/master vs working tree)."
    )
    p.add_argument(
        "--suite",
        default="all",
        help=f"Task group ({', '.join(SUITE_NAMES)}). Default: all.",
    )
    p.add_argument(
        "--models",
        default=DEFAULT_MODELS,
        help=f"Comma-separated model IDs. Default: {DEFAULT_MODELS}.",
    )
    p.add_argument(
        "--repeats",
        type=int,
        default=DEFAULT_REPEATS,
        help=f"Number of repeats per side. Default: {DEFAULT_REPEATS}.",
    )
    p.add_argument(
        "--tasks",
        default="",
        help="Optional comma-separated task_id override.",
    )
    p.add_argument(
        "--out-dir",
        type=Path,
        default=DEFAULT_OUT_ROOT,
        help=f"Resumable output root directory. Default: {DEFAULT_OUT_ROOT}.",
    )
    p.add_argument(
        "--worktree-dir",
        type=Path,
        default=DEFAULT_WORKTREE_DIR,
        help=f"Reused detached worktree for Side A (origin/master). Default: {DEFAULT_WORKTREE_DIR}.",
    )
    p.add_argument(
        "--ref",
        default="origin/master",
        help="Git ref for Side A. Default: origin/master.",
    )
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.repeats < 1:
        print("ERROR: --repeats must be >= 1", file=sys.stderr)
        return 2

    try:
        models = parse_models(args.models)
        tasks = resolve_suite_tasks(args.suite, args.tasks or None)
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    suite_name = args.suite.strip().lower() or "custom"
    suite_dir = args.out_dir / suite_name
    suite_dir.mkdir(parents=True, exist_ok=True)

    run_generate_manifest(REPO_ROOT)

    need_a = any(
        not is_repeat_done(suite_dir, "A", rep) for rep in range(1, args.repeats + 1)
    )
    worktree_a: Path | None = None
    if need_a:
        worktree_a = ensure_side_a_worktree(REPO_ROOT, args.worktree_dir, ref=args.ref)
        run_generate_manifest(worktree_a)

    for rep in range(1, args.repeats + 1):
        for side, root in (("A", worktree_a), ("B", REPO_ROOT)):
            out_path, unused_details = result_paths(suite_dir, side, rep)
            if is_repeat_done(suite_dir, side, rep):
                print(f"[skip] {side}-r{rep} already exists: {out_path}")
                continue
            assert root is not None
            print(f"[run] {side}-r{rep} ({len(tasks)} tasks, {len(models)} models) -> {out_path}")
            run_one_eval(
                root,
                models=models,
                tasks=tasks,
                out_path=out_path,
                jobs=1,
                lo_workers=2,
                backend="auto",
            )

    a_details = load_side_details(suite_dir, "A", args.repeats)
    b_details = load_side_details(suite_dir, "B", args.repeats)
    tally = tally_ab_results(a_details, b_details, models, tasks)
    print()
    print(format_ab_report(tally, suite=suite_name, repeats=args.repeats))
    return 0


if __name__ == "__main__":
    sys.exit(main())
