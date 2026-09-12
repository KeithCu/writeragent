#!/usr/bin/env python3
"""
Run the Writer assistant across multiple models and mark the cost-quality Pareto frontier.

This reuses the same dataset and metric as run_eval.py (LlmClient tool loop;
default in-memory `--backend string`), but iterates over model configurations
(see model_configs.py) and estimates cost using list prices (USD per 1M tokens).
Recommendation is maximize ``avg_correctness`` and minimize ``avg_cost_per_example``.

Usage:
  export OPENROUTER_API_KEY="your-key"   # or OPENAI_API_KEY / WRITERAGENT_API_KEY
  cd scripts/prompt_optimization
  python run_eval_multi.py
  python run_eval_multi.py --backend lo  # LibreOffice instead of string simulator
  python run_eval_multi.py --models openai/gpt-oss-120b,openai/gpt-4o-mini
  python run_eval_multi.py -n 2
  python run_eval_multi.py -j 20  # 20 models in parallel (default)
  python run_eval_multi.py -j 1   # sequential, verbose per-example output
  python run_eval_multi.py --allow-unknown-model --models llama3.2
"""
from __future__ import annotations

import argparse
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Iterable, Sequence

from dataset import ALL_EXAMPLES, to_dspy_examples, to_eval_examples
from eval_auth import (
    require_api_key,
    resolve_api_base,
    resolve_api_key,
    resolve_judge_model,
)
from eval_catalog import add_eval_tool_sweep_arguments
from eval_core import ExampleEval, example_passed, run_eval_on_examples_llm
from plugin.framework.openrouter_model_id import resolve_openrouter_catalog_id
from model_configs import (
    DEFAULT_GOLD_MODEL,
    MODEL_ALIASES,
    MODEL_BY_ID,
    ModelConfig,
    get_default_models,
)
import tools_lo as _tools_lo

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))


def _parse_model_ids(arg: str | None) -> Sequence[str]:
    if not arg:
        return [m.openrouter_id for m in get_default_models()]
    return [MODEL_ALIASES.get(s.strip(), s.strip()) for s in arg.split(",") if s.strip()]


def _model_id_for_llm_client(model_id: str) -> str:
    """Strip ``openrouter/`` for ``LlmClient`` (OpenRouter HTTP API uses ``provider/model``)."""
    m = model_id
    if m.startswith("openrouter/"):
        m = m[len("openrouter/") :]
    return m


def _model_config_for_id(model_id: str, *, allow_unknown: bool) -> ModelConfig:
    model_id = MODEL_ALIASES.get(model_id, model_id)
    if model_id in MODEL_BY_ID:
        return MODEL_BY_ID[model_id]
    resolved = resolve_openrouter_catalog_id(model_id, set(MODEL_BY_ID))
    if resolved in MODEL_BY_ID:
        base = MODEL_BY_ID[resolved]
        if model_id == resolved:
            return base
        suffix = model_id.rsplit(":", 1)[-1]
        return ModelConfig(
            openrouter_id=model_id,
            display_name=f"{base.display_name} ({suffix})",
            context_window_tokens=base.context_window_tokens,
            input_cost_per_million=base.input_cost_per_million,
            output_cost_per_million=base.output_cost_per_million,
            notes=base.notes,
        )
    if allow_unknown:
        return ModelConfig(
            openrouter_id=model_id,
            display_name=model_id,
            context_window_tokens=None,
            input_cost_per_million=0.0,
            output_cost_per_million=0.0,
            notes="unknown pricing (use MODEL_BY_ID or OpenRouter catalog for cost/IpD)",
        )
    raise KeyError(model_id)


def _estimate_cost_usd(
    results: Iterable[ExampleEval],
    cfg: ModelConfig,
) -> float:
    if cfg.input_cost_per_million == 0.0 and cfg.output_cost_per_million == 0.0:
        return 0.0
    total_cost = 0.0
    for r in results:
        total_cost += (
            (r.prompt_tokens / 1_000_000.0) * cfg.input_cost_per_million
            + (r.completion_tokens / 1_000_000.0) * cfg.output_cost_per_million
        )
    return total_cost


PARETO_FRONTIER = "frontier"
PARETO_DOMINATED = "dominated"
PARETO_UNAVAILABLE = "unavailable"
_PARETO_ORDER = {PARETO_FRONTIER: 0, PARETO_DOMINATED: 1, PARETO_UNAVAILABLE: 2}


def _pareto_eligible(summary: dict[str, Any]) -> bool:
    """True when the model produced at least one scored task with known positive cost."""
    n = int(summary.get("n_examples") or 0)
    n_error = int(summary.get("n_error") or 0)
    if n <= 0 or n_error >= n:
        return False
    if not summary.get("pricing_known"):
        return False
    return float(summary.get("avg_cost_per_example") or 0.0) > 0.0


def _pareto_dominates(left: dict[str, Any], right: dict[str, Any]) -> bool:
    """True when left is at least as correct and no more expensive, with one strict gain."""
    left_q = float(left.get("avg_correctness") or 0.0)
    right_q = float(right.get("avg_correctness") or 0.0)
    left_c = float(left.get("avg_cost_per_example") or 0.0)
    right_c = float(right.get("avg_cost_per_example") or 0.0)
    return (left_q >= right_q and left_c <= right_c) and (left_q > right_q or left_c < right_c)


def annotate_pareto_fronts(summaries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Peel successive Pareto fronts; set ``pareto_front`` (1-based) and ``pareto_status``."""
    remaining = [row for row in summaries if _pareto_eligible(row)]
    front_num = 1
    while remaining:
        current_front = [
            row
            for row in remaining
            if not any(
                other is not row and _pareto_dominates(other, row) for other in remaining
            )
        ]
        for row in current_front:
            row["pareto_front"] = front_num
            remaining.remove(row)
        front_num += 1
    for row in summaries:
        if not _pareto_eligible(row):
            row["pareto_front"] = None
            row["pareto_status"] = PARETO_UNAVAILABLE
            continue
        front = int(row.get("pareto_front") or 0)
        row["pareto_status"] = PARETO_FRONTIER if front == 1 else PARETO_DOMINATED
    return summaries


def annotate_pareto_status(summaries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Set ``pareto_status`` (and ``pareto_front``) from the current completed set."""
    return annotate_pareto_fronts(summaries)


def _pareto_min_max_norm(values: list[float]) -> tuple[list[float], float]:
    """Map *values* to [0, 1]; return unit list and span (1.0 when flat)."""
    if not values:
        return [], 1.0
    lo = min(values)
    hi = max(values)
    span = hi - lo if hi > lo else 1.0
    return [(value - lo) / span for value in values], span


def _pareto_point_to_segment_dist(
    px: float,
    py: float,
    x1: float,
    y1: float,
    x2: float,
    y2: float,
) -> float:
    dx = x2 - x1
    dy = y2 - y1
    if dx == 0.0 and dy == 0.0:
        return ((px - x1) ** 2 + (py - y1) ** 2) ** 0.5
    t = max(0.0, min(1.0, ((px - x1) * dx + (py - y1) * dy) / (dx * dx + dy * dy)))
    proj_x = x1 + t * dx
    proj_y = y1 + t * dy
    return ((px - proj_x) ** 2 + (py - proj_y) ** 2) ** 0.5


def _pareto_distance_to_polyline(
    px: float,
    py: float,
    polyline: list[tuple[float, float]],
) -> float:
    if not polyline:
        return float("inf")
    if len(polyline) == 1:
        x0, y0 = polyline[0]
        return ((px - x0) ** 2 + (py - y0) ** 2) ** 0.5
    best = float("inf")
    for idx in range(len(polyline) - 1):
        x1, y1 = polyline[idx]
        x2, y2 = polyline[idx + 1]
        best = min(best, _pareto_point_to_segment_dist(px, py, x1, y1, x2, y2))
    return best


def pareto_f1_distances(summaries: list[dict[str, Any]]) -> dict[int, float]:
    """Distance from each eligible row to the F1 polyline in normalized plot space."""
    import math

    eligible = [row for row in summaries if _pareto_eligible(row)]
    if not eligible:
        return {}
    log_costs = [math.log10(float(row["avg_cost_per_example"])) for row in eligible]
    correctness = [float(row["avg_correctness"]) for row in eligible]
    norm_x, _ = _pareto_min_max_norm(log_costs)
    norm_y, _ = _pareto_min_max_norm(correctness)
    unit_by_id = {
        id(row): (norm_x[idx], norm_y[idx]) for idx, row in enumerate(eligible)
    }
    f1 = sorted(
        (row for row in eligible if int(row.get("pareto_front") or 0) == 1),
        key=lambda row: float(row["avg_cost_per_example"]),
    )
    if not f1:
        return {}
    polyline = [unit_by_id[id(row)] for row in f1]
    f1_ids = {id(row) for row in f1}
    distances: dict[int, float] = {}
    for row in eligible:
        px, py = unit_by_id[id(row)]
        if id(row) in f1_ids:
            distances[id(row)] = 0.0
        else:
            distances[id(row)] = _pareto_distance_to_polyline(px, py, polyline)
    return distances


def pareto_tradeoff_scores(summaries: list[dict[str, Any]]) -> dict[int, float]:
    """Inverted F1 distance in normalized plot space (1.0 = on frontier, lower = worse tradeoff)."""
    return {
        row_id: max(0.0, 1.0 - dist) for row_id, dist in pareto_f1_distances(summaries).items()
    }


def _sort_tradeoff_display(
    summaries: list[dict[str, Any]],
    scores: dict[int, float],
) -> None:
    """Eligible rows by descending tradeoff score, then cost, then correctness."""
    summaries.sort(
        key=lambda row: (
            -scores.get(id(row), -1.0),
            float(row.get("avg_cost_per_example") or 0.0),
            -float(row.get("avg_correctness") or 0.0),
        )
    )


def _sort_pareto_display(summaries: list[dict[str, Any]]) -> None:
    """Frontier first, then dominated, then unavailable; cheaper then more correct."""
    summaries.sort(
        key=lambda row: (
            _PARETO_ORDER.get(str(row.get("pareto_status") or ""), 3),
            float(row.get("avg_cost_per_example") or 0.0)
            if row.get("pareto_status") != PARETO_UNAVAILABLE
            else float("inf"),
            -float(row.get("avg_correctness") or 0.0),
        )
    )


def _write_details(out_path: Path, all_details: list[dict[str, Any]]) -> None:
    """Write detailed per-example results to a separate file (e.g. eval_details.json/csv)."""
    detailed_path = out_path.parent / (out_path.stem + "_details" + out_path.suffix)
    detailed_path.parent.mkdir(parents=True, exist_ok=True)

    as_csv = detailed_path.suffix.lower() == ".csv"
    if as_csv:
        import csv
        if not all_details:
            detailed_path.write_text("")
            return
        keys = list(all_details[0].keys())
        with detailed_path.open("w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=keys)
            w.writeheader()
            w.writerows(all_details)
    else:
        import json
        detailed_path.write_text(json.dumps(all_details, indent=2), encoding="utf-8")


def _write_results(out_path: Path, model_summaries: list[dict[str, Any]]) -> None:
    """Write model_summaries to out_path as JSON or CSV (by extension). Creates parent dirs if needed."""
    out_path = out_path.resolve()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    as_csv = out_path.suffix.lower() == ".csv"
    if as_csv:
        import csv
        if not model_summaries:
            out_path.write_text("")
            return
        keys = list(model_summaries[0].keys())
        with out_path.open("w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=keys)
            w.writeheader()
            w.writerows(model_summaries)
    else:
        import json
        out_path.write_text(json.dumps(model_summaries, indent=2), encoding="utf-8")


def _out_path(args: argparse.Namespace) -> Path | None:
    if not args.out:
        return None
    p = Path(args.out)
    return p if p.is_absolute() else (Path.cwd() / p)


def _run_one_model(
    model_id: str,
    api_base: str,
    api_key: str,
    example_arg: str | None,
    n: int | None,
    verbose: bool,
    debug_usage: bool,
    bust_cache: bool,
    judge_model_id: str | None,
    gold_model_id: str | None,
    backend: str,
    allow_unknown: bool,
    student: str = "llm",
    no_judge: bool = False,
    repeats: int = 1,
    tools_spec: str | None = None,
    schema_density: str = "full",
) -> dict[str, Any]:
    """Run eval for one model (used in a worker process). Returns summary dict."""
    from dataset import ALL_EXAMPLES, to_dspy_examples
    from eval_core import summarize_results

    _tools_lo.VERBOSE = verbose
    examples = to_dspy_examples(ALL_EXAMPLES, with_inputs=True)
    if example_arg:
        examples = [ex for ex in examples if getattr(ex, "task_id", "") == example_arg]
    if n is not None:
        examples = examples[:n]
    cfg = _model_config_for_id(model_id, allow_unknown=allow_unknown)
    model = _model_id_for_llm_client(model_id)
    jm = _model_id_for_llm_client(judge_model_id) if judge_model_id else None
    gm = _model_id_for_llm_client(gold_model_id) if gold_model_id else None

    results = run_eval_on_examples_llm(
        examples,
        endpoint=api_base,
        api_key=api_key,
        model=model,
        instruction=None,
        backend=backend,
        verbose=verbose,
        debug_usage=debug_usage,
        bust_cache=bust_cache,
        quiet=False,
        judge_model=jm,
        gold_model=gm,
        student=student,
        no_judge=no_judge or student == "scripted",
        tools_spec=tools_spec,
        schema_density=schema_density,
    )
    if repeats > 1:
        extra: list[ExampleEval] = []
        for _rep in range(repeats - 1):
            extra.extend(
                run_eval_on_examples_llm(
                    examples,
                    endpoint=api_base,
                    api_key=api_key,
                    model=model,
                    instruction=None,
                    backend=backend,
                    verbose=verbose,
                    debug_usage=debug_usage,
                    bust_cache=bust_cache,
                    quiet=False,
                    judge_model=jm,
                    gold_model=gm,
                    student=student,
                    no_judge=no_judge or student == "scripted",
                    tools_spec=tools_spec,
                    schema_density=schema_density,
                )
            )
        results = results + extra
    summary = summarize_results(results)
    total_cost = _estimate_cost_usd(results, cfg)
    pricing_known = cfg.input_cost_per_million > 0 or cfg.output_cost_per_million > 0
    avg_cost_per_example = total_cost / len(results) if results else 0.0
    if pricing_known and avg_cost_per_example > 0:
        ipd_correctness = (summary["avg_correctness"] ** 2) / avg_cost_per_example
        ipd_metric = (summary["avg_metric_score"] ** 2) / avg_cost_per_example
    else:
        ipd_correctness = 0.0
        ipd_metric = 0.0
    details = []
    for r in results:
        details.append({
            "task_id": r.task_id,
            "category": r.task_category,
            "judge_score": r.judge_score,
            "judge_accuracy": r.judge_accuracy,
            "judge_formatting": r.judge_formatting,
            "judge_naturalness": r.judge_naturalness,
            "judge_reasoning": r.judge_reasoning,
            "judge_error": r.judge_error,
            "document_score": r.document_score,
            "correctness": r.correctness,
            "agent_score": r.agent_score,
            "hard_pass": example_passed(r),
            "process_failures": r.process_failures,
            "oracle_failures": r.oracle_failures,
            "missing_expected": r.missing_expected,
            "found_reject": r.found_reject,
            "metric_score": r.metric_score,
            "total_tokens": r.total_tokens,
            "final_document": r.final_document,
            "error": r.error,
        })

    return {
        "summary": {
            "openrouter_id": cfg.openrouter_id,
            "display_name": cfg.display_name,
            "context_window_tokens": cfg.context_window_tokens,
            "input_cost_per_million": cfg.input_cost_per_million,
            "output_cost_per_million": cfg.output_cost_per_million,
            "pricing_known": pricing_known,
            "avg_correctness": summary["avg_correctness"],
            "avg_agent_score": summary.get("avg_agent_score", 0.0),
            "hard_pass_rate": summary.get("hard_pass_rate", 0.0),
            "document_pass_rate": summary.get("document_pass_rate", 0.0),
            "avg_quality": summary.get("avg_quality", 0.0),
            "n_judged": summary.get("n_judged", 0),
            "n_error": summary.get("n_error", 0),
            "n_examples": len(results),
            "avg_metric_score": summary["avg_metric_score"],
            "total_tokens": summary["total_tokens"],
            "total_cost_usd": total_cost,
            "avg_cost_per_example": avg_cost_per_example,
            "intelligence_per_dollar_correctness": ipd_correctness,
            "intelligence_per_dollar_metric": ipd_metric,
        },
        "details": details,
    }


def parse_args(argv: list[str] | None = None):
    p = argparse.ArgumentParser(
        description=(
            "Eval Writer assistant on dataset across multiple models and "
            "compare intelligence per dollar."
        )
    )
    p.add_argument(
        "--models",
        metavar="KEYS",
        help=(
            "Comma-separated model ids (e.g. openai/gpt-oss-120b). "
            "Default: all in get_default_models()."
        ),
    )
    p.add_argument(
        "--api-base",
        default=None,
        help="API base URL (default: WRITERAGENT_API_BASE / OPENAI_API_BASE / OpenRouter).",
    )
    p.add_argument(
        "--api-key",
        "-k",
        default=None,
        help="API key (default: WRITERAGENT_API_KEY / OPENAI_API_KEY / OPENROUTER_API_KEY).",
    )
    p.add_argument(
        "--allow-unknown-model",
        action="store_true",
        help="Allow model ids not listed in model_configs.py (cost/IpD n/a).",
    )
    p.add_argument(
        "--yes-all-models",
        action="store_true",
        help="Allow the default catalog sweep when --models is omitted.",
    )
    p.add_argument(
        "--repeats",
        type=int,
        default=1,
        help="Repeat each example this many times (default: 1; use 3 for selection runs).",
    )
    p.add_argument(
        "--example",
        "-e",
        metavar="TASK_ID",
        help="Run only this task_id (e.g. table_from_mess). Recommended with --generate-golds (one teacher call per run).",
    )
    p.add_argument(
        "-n",
        type=int,
        default=None,
        help="Run only first N examples.",
    )
    p.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="Print every tool call as it runs.",
    )
    p.add_argument(
        "--debug-usage",
        action="store_true",
        help="Print raw usage when tokens=0 to debug token extraction.",
    )
    p.add_argument(
        "--no-bust-cache",
        action="store_true",
        help="Disable cache-busting (default: enabled for accurate token counts).",
    )
    p.add_argument(
        "--out",
        metavar="PATH",
        default="eval_results.csv",
        help="Write per-model summary to PATH (.json or .csv). Default: eval_results.csv in this script's directory.",
    )
    p.add_argument(
        "--judge",
        "-J",
        metavar="ID",
        default=None,
        help="Judge model id (default: openai/gpt-oss-120b:nitro on OpenRouter; else first --models id on other endpoints).",
    )
    p.add_argument(
        "--no-judge",
        action="store_true",
        help="Skip LLM judge; use result oracles + expected/reject only.",
    )
    p.add_argument(
        "--gold-model",
        metavar="ID",
        default=DEFAULT_GOLD_MODEL,
        help=f"Model id for --generate-golds only (default: {DEFAULT_GOLD_MODEL}). Not used during ranking.",
    )
    p.add_argument(
        "--generate-golds",
        action="store_true",
        help=(
            "Generate gold answers with --gold-model (default GPT-5.6 Luna). "
            "Writes/merges gold_standards.json. By default only one example per run — use -e TASK_ID or -n 1; "
            "for several in one invocation pass --yes-multi-gold."
        ),
    )
    p.add_argument(
        "--yes-multi-gold",
        action="store_true",
        help=(
            "With --generate-golds, allow more than one dataset example in this process "
            "(multiple teacher API calls). Omit this to force single-example runs."
        ),
    )
    p.add_argument(
        "--jobs",
        "-j",
        type=int,
        default=20,
        help="Number of models to run in parallel (default: 20). Use 1 for sequential.",
    )
    p.add_argument(
        "--backend",
        choices=("string", "lo"),
        default="string",
        help=(
            "Document backend: 'string' (in-memory HTML, default) or "
            "'lo' (headless Writer/Draw/Calc)."
        ),
    )
    p.add_argument(
        "--student",
        choices=("llm", "scripted"),
        default="llm",
        help="llm (default, needs API key) or scripted (replay SCRIPTS, no key).",
    )
    add_eval_tool_sweep_arguments(p)
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    api_base = resolve_api_base(cli_base=args.api_base)
    api_key = resolve_api_key(cli_key=args.api_key)
    if args.student != "scripted":
        require_api_key(api_key, api_base)

    model_summaries: list[dict[str, Any]] = []
    all_details: list[dict[str, Any]] = []
    model_ids = _parse_model_ids(args.models)
    if not args.models and not args.yes_all_models and not args.generate_golds:
        print(
            "Refusing: omit --models only with --yes-all-models "
            f"(would sweep {len(model_ids)} catalog models). "
            "Pass --models id1,id2 or --yes-all-models.",
            file=sys.stderr,
        )
        return 1
    unknown = [
        mid
        for mid in model_ids
        if mid not in MODEL_BY_ID
        and resolve_openrouter_catalog_id(mid, set(MODEL_BY_ID)) not in MODEL_BY_ID
    ]
    if unknown and not args.allow_unknown_model:
        print(f"Unknown model id(s): {unknown}", file=sys.stderr)
        print(f"Known ids: {sorted(MODEL_BY_ID.keys())}", file=sys.stderr)
        print("Pass --allow-unknown-model for local/custom endpoints.", file=sys.stderr)
        return 1

    judge_model_id: str | None = None
    if not args.no_judge and args.student != "scripted":
        judge_model_id = resolve_judge_model(
            cli_judge=args.judge,
            endpoint=api_base,
            model_ids=model_ids,
        )
        print(f"Judge model: {judge_model_id} @ {api_base}")

    # Dataset selection. Gold generation must not import dspy.
    if args.generate_golds:
        examples = to_eval_examples(ALL_EXAMPLES)
    else:
        examples = to_dspy_examples(ALL_EXAMPLES, with_inputs=True)
    if args.example:
        examples = [
            ex
            for ex in examples
            if getattr(ex, "task_id", "") == args.example
        ]
        if not examples:
            print(
                f"No example with task_id={args.example!r}. "
                f"Valid: {[getattr(e, 'task_id', '') for e in to_eval_examples(ALL_EXAMPLES)]}",
                file=sys.stderr,
            )
            return 1
    if args.n is not None:
        examples = examples[: args.n]

    _tools_lo.VERBOSE = args.verbose

    # One-time gold generation logic
    if args.generate_golds:
        import json

        from llm_chat_eval import run_llm_chat_eval
        from eval_prompts import get_eval_system_prompt
        from oracles import check_oracle
        from process_oracles import check_process

        if len(examples) > 1 and not args.yes_multi_gold:
            print(
                "Refusing: --generate-golds would run multiple examples (multiple costly --gold-model calls). "
                "Run one task: add -e <task_id> or -n 1, or pass --yes-multi-gold to generate many in one go.",
                file=sys.stderr,
            )
            return 1
        print(f"Generating gold standards for {len(examples)} examples using {args.gold_model}...")
        gm = _model_id_for_llm_client(args.gold_model)

        gold_map: dict[str, str] = {}
        details: list[dict[str, Any]] = []
        if args.backend == "lo":
            _tools_lo.LOBackend.start()
        try:
            for i, ex in enumerate(examples):
                tid = getattr(ex, "task_id", f"example_{i}")
                print(f"  [{i+1}/{len(examples)}] Generating gold for {tid}...")
                html, usage, gerr, gtrace = run_llm_chat_eval(
                    system_prompt=get_eval_system_prompt(tid),
                    document_content=ex.document_content,
                    user_question=ex.user_question,
                    endpoint=api_base,
                    api_key=api_key,
                    model=gm,
                    backend=args.backend,
                    verbose=args.verbose,
                    task_id=tid,
                    tools_spec=args.tools,
                    schema_density=args.schema_density,
                )
                if gerr:
                    print(f"  Warning: gold error for {tid}: {gerr}", file=sys.stderr)
                gold_map[tid] = html
                oracle_failures = check_oracle(tid, html)
                process_failures = check_process(tid, gtrace)
                details.append(
                    {
                        "task_id": tid,
                        "error": gerr,
                        "oracle_failures": oracle_failures,
                        "process_failures": process_failures,
                        "chars": len(html or ""),
                        "total_tokens": int((usage or {}).get("total_tokens") or 0),
                        "trace_names": [str(t.get("name") or "") for t in (gtrace or [])],
                    }
                )
                print(
                    f"    oracle={oracle_failures or []} process={process_failures or []} "
                    f"chars={len(html or '')} err={gerr!r}",
                    flush=True,
                )
        finally:
            if args.backend == "lo":
                _tools_lo.LOBackend.stop()

        out_p = SCRIPT_DIR / "gold_standards.json"
        merged: dict[str, str] = {}
        if out_p.exists():
            try:
                merged = json.loads(out_p.read_text(encoding="utf-8"))
            except Exception:
                merged = {}
        merged.update(gold_map)
        out_p.write_text(json.dumps(merged, indent=2), encoding="utf-8")
        gen_p = SCRIPT_DIR / "gold_standards.generated.json"
        gen_p.write_text(json.dumps(gold_map, indent=2), encoding="utf-8")
        details_p = SCRIPT_DIR / "gold_generation_details.json"
        details_p.write_text(json.dumps(details, indent=2), encoding="utf-8")
        print(f"\nDone! Saved {len(gold_map)} gold standard(s) to {out_p} (merged with existing keys).")
        print(f"This run only: {gen_p}")
        print(f"Per-task oracle/process/trace: {details_p}")
        return 0

    jobs = max(1, args.jobs)
    print(
        f"Running {len(examples)} example(s) for {len(model_ids)} model(s)"
        + (f" ({jobs} in parallel)." if jobs > 1 else " (sequential).")
        + "\nEach example can take 15–60+ seconds (multiple API calls per model)."
    )
    sys.stdout.flush()

    worker_kw = dict(
        example_arg=args.example,
        n=args.n,
        verbose=args.verbose,
        debug_usage=args.debug_usage,
        bust_cache=not args.no_bust_cache,
        judge_model_id=judge_model_id,
        gold_model_id=args.gold_model if args.generate_golds else None,
        backend=args.backend,
        allow_unknown=args.allow_unknown_model,
        student=args.student,
        no_judge=args.no_judge or args.student == "scripted",
        repeats=max(1, args.repeats),
        tools_spec=args.tools,
        schema_density=args.schema_density,
    )

    if args.backend == "lo":
        _tools_lo.LOBackend.start()
    try:
        if jobs <= 1:
            for model_id in model_ids:
                cfg = _model_config_for_id(model_id, allow_unknown=args.allow_unknown_model)
                print("=" * 60)
                print(f"Model: {cfg.display_name} ({cfg.openrouter_id})")
                if cfg.context_window_tokens:
                    print(f"  Context window: {cfg.context_window_tokens} tokens")
                if cfg.input_cost_per_million or cfg.output_cost_per_million:
                    print(
                        f"  Pricing: ${cfg.input_cost_per_million}/M input, "
                        f"${cfg.output_cost_per_million}/M output"
                    )
                else:
                    print("  Pricing: n/a (--allow-unknown-model)")
                print(f"  Using model id: {model_id} @ {api_base}\n")

                res = _run_one_model(model_id, api_base, api_key, **worker_kw)
                model_summaries.append(res["summary"])
                for d in res["details"]:
                    d["model_id"] = model_id
                all_details.extend(res["details"])

                out_path = _out_path(args)
                if out_path:
                    annotate_pareto_status(model_summaries)
                    _write_results(out_path, model_summaries)
                    _write_details(out_path, all_details)

                m = res["summary"]
                cost_s = f"${m['total_cost_usd']:.4f}" if m.get("pricing_known") else "n/a"
                print(
                    f"Done: {m['openrouter_id']}  avg_correctness={m['avg_correctness']:.3f}  "
                    f"cost={cost_s}  ({len(model_summaries)}/{len(model_ids)} models)"
                )
        else:
            out_path = _out_path(args)
            with ThreadPoolExecutor(max_workers=jobs) as pool:
                futures = {
                    pool.submit(_run_one_model, model_id, api_base, api_key, **worker_kw): model_id
                    for model_id in model_ids
                }
                for future in as_completed(futures):
                    model_id = futures[future]
                    try:
                        res = future.result()
                        model_summaries.append(res["summary"])
                        for d in res["details"]:
                            d["model_id"] = model_id
                        all_details.extend(res["details"])
                        if out_path:
                            annotate_pareto_status(model_summaries)
                            _write_results(out_path, model_summaries)
                            _write_details(out_path, all_details)
                        m = res["summary"]
                        cost_s = f"${m['total_cost_usd']:.4f}" if m.get("pricing_known") else "n/a"
                        print(
                            f"Done: {m['openrouter_id']}  avg_correctness={m['avg_correctness']:.3f}  "
                            f"cost={cost_s}  ({len(model_summaries)}/{len(model_ids)} models)"
                        )
                    except Exception as e:
                        print(f"Model {model_id} failed: {e}", file=sys.stderr)
                        try:
                            cfg = _model_config_for_id(
                                model_id, allow_unknown=args.allow_unknown_model
                            )
                        except KeyError:
                            cfg = ModelConfig(
                                openrouter_id=model_id,
                                display_name=model_id,
                                context_window_tokens=None,
                                input_cost_per_million=0.0,
                                output_cost_per_million=0.0,
                            )
                        model_summaries.append({
                            "openrouter_id": cfg.openrouter_id,
                            "display_name": cfg.display_name,
                            "context_window_tokens": cfg.context_window_tokens,
                            "input_cost_per_million": cfg.input_cost_per_million,
                            "output_cost_per_million": cfg.output_cost_per_million,
                            "pricing_known": False,
                            "avg_correctness": 0.0,
                            "avg_agent_score": 0.0,
                            "hard_pass_rate": 0.0,
                            "document_pass_rate": 0.0,
                            "avg_quality": 0.0,
                            "n_judged": 0,
                            "n_error": 1,
                            "n_examples": 0,
                            "avg_metric_score": 0.0,
                            "total_tokens": 0,
                            "total_cost_usd": 0.0,
                            "avg_cost_per_example": 0.0,
                            "intelligence_per_dollar_correctness": 0.0,
                            "intelligence_per_dollar_metric": 0.0,
                        })
                        if out_path:
                            annotate_pareto_status(model_summaries)
                            _write_results(out_path, model_summaries)
    finally:
        if args.backend == "lo":
            _tools_lo.LOBackend.stop()

    if not model_summaries:
        print("No models were evaluated.")
        return 0

    annotate_pareto_status(model_summaries)
    _sort_pareto_display(model_summaries)

    print("=" * 60)
    print("RESULTS (Pareto: maximize avg correctness, minimize avg $/task)")
    print("=" * 60)
    print(
        f"{'Status':<12}  {'Model':<32}  {'Hard%':>6}  {'Agent':>6}  {'Qual':>6}  "
        f"{'AvgCorr':>7}  {'AvgCost($)':>11}"
    )
    for m in model_summaries:
        if m.get("pricing_known") and float(m.get("avg_cost_per_example") or 0.0) > 0:
            cost_col = f"{m['avg_cost_per_example']:>11.5f}"
        else:
            cost_col = f"{'n/a':>11}"
        print(
            f"{str(m.get('pareto_status') or PARETO_UNAVAILABLE):<12}  "
            f"{m['openrouter_id']:<32}  "
            f"{m.get('hard_pass_rate', 0.0):>6.3f}  "
            f"{m.get('avg_agent_score', 0.0):>6.3f}  "
            f"{m.get('avg_quality', 0.0):>6.3f}  "
            f"{m['avg_correctness']:>7.3f}  "
            f"{cost_col}"
        )

    out_path = _out_path(args)
    if out_path:
        _write_results(out_path, model_summaries)
        _write_details(out_path, all_details)
        fmt = "CSV" if out_path.suffix.lower() == ".csv" else "JSON"
        print(f"\nWrote per-model summary ({fmt}) to {out_path}")
        print(f"Wrote per-test details to {out_path.parent / (out_path.stem + '_details' + out_path.suffix)}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
