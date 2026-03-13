#!/usr/bin/env python3
"""
Run the Writer assistant across multiple models and compare intelligence per dollar.

This reuses the same dataset, program, and metric as run_eval.py, but iterates
over a set of model configurations (see model_configs.py) and estimates cost
using list prices (USD per 1M tokens).

Usage:
  export OPENROUTER_API_KEY="your-key"   # or OPENAI_API_KEY
  cd scripts/prompt_optimization
  python run_eval_multi.py
  python run_eval_multi.py --models openai/gpt-oss-120b,openai/gpt-4o-mini
  python run_eval_multi.py -n 2
  python run_eval_multi.py -j 8   # 8 models in parallel (default)
  python run_eval_multi.py -j 1   # sequential, verbose per-example output
"""
from __future__ import annotations

import argparse
import os
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Iterable, Sequence

import dspy

from dataset import ALL_EXAMPLES, to_dspy_examples
from eval_core import ExampleEval, run_eval_on_examples, summarize_results
from model_configs import MODEL_BY_ID, ModelConfig, get_default_models
from program import build_program
import tools_lo as _tools_lo

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

DEFAULT_API_BASE = "https://openrouter.ai/api/v1"


def _parse_model_ids(arg: str | None) -> Sequence[str]:
    if not arg:
        return [m.openrouter_id for m in get_default_models()]
    return [s.strip() for s in arg.split(",") if s.strip()]


def _get_lm(model_id: str, api_key: str, api_base: str) -> dspy.LM:
    """Instantiate a dspy.LM with OpenRouter prefixing if needed."""
    model = model_id
    if "openrouter" in api_base.lower() and not model.startswith("openrouter/"):
        model = "openrouter/" + model
    return dspy.LM(model=model, api_key=api_key, api_base=api_base, model_type="chat")


def _estimate_cost_usd(
    results: Iterable[ExampleEval],
    cfg: ModelConfig,
) -> float:
    total_cost = 0.0
    for r in results:
        total_cost += (
            (r.prompt_tokens / 1_000_000.0) * cfg.input_cost_per_million
            + (r.completion_tokens / 1_000_000.0) * cfg.output_cost_per_million
        )
    return total_cost


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
    return p if p.is_absolute() else (SCRIPT_DIR / p)


def _run_one_model(
    model_id: str,
    api_base: str,
    api_key: str,
    example_arg: str | None,
    n: int | None,
    verbose: bool,
    debug_usage: bool,
    bust_cache: bool,
    judge_model_id: str | None = None,
    gold_model_id: str | None = None,
) -> dict[str, Any]:
    """Run eval for one model (used in a worker process). Returns summary dict."""
    import dspy
    from dataset import ALL_EXAMPLES, to_dspy_examples
    from eval_core import run_eval_on_examples, summarize_results
    from model_configs import MODEL_BY_ID
    from program import build_program

    _tools_lo.VERBOSE = verbose
    examples = to_dspy_examples(ALL_EXAMPLES, with_inputs=True)
    if example_arg:
        examples = [ex for ex in examples if getattr(ex, "task_id", "") == example_arg]
    if n is not None:
        examples = examples[:n]
    cfg = MODEL_BY_ID[model_id]
    model = model_id
    lm = _get_lm(model_id, api_key, api_base)
    
    judge_lm = None
    if judge_model_id:
        judge_lm = _get_lm(judge_model_id, api_key, api_base)
    
    gold_lm = None
    if gold_model_id:
        gold_lm = _get_lm(gold_model_id, api_key, api_base)

    dspy.configure(lm=lm)
    program = build_program(instruction=None, tool_names=None)
    results = run_eval_on_examples(
        program,
        examples,
        verbose=verbose,
        debug_usage=debug_usage,
        bust_cache=bust_cache,
        quiet=False,
        judge_lm=judge_lm,
        gold_lm=gold_lm,
    )
    summary = summarize_results(results)
    total_cost = _estimate_cost_usd(results, cfg)
    avg_cost_per_example = total_cost / len(results) if results else 0.0
    eps = 1e-9
    # Intelligence per Dollar = (Correctness^2 / AvgCost). 
    # Squaring the correctness (P=2) prioritizes quality/accuracy over raw cost, 
    # ensuring "cheap but broken" models don't dominate the leaderboard.
    ipd_correctness = (summary["avg_correctness"]**2) / max(avg_cost_per_example, eps) if avg_cost_per_example > 0 else 0.0
    ipd_metric = (summary["avg_metric_score"]**2) / max(avg_cost_per_example, eps) if avg_cost_per_example > 0 else 0.0
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
            "correctness": r.correctness,
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
            "avg_correctness": summary["avg_correctness"],
            "avg_metric_score": summary["avg_metric_score"],
            "total_tokens": summary["total_tokens"],
            "total_cost_usd": total_cost,
            "avg_cost_per_example": avg_cost_per_example,
            "intelligence_per_dollar_correctness": ipd_correctness,
            "intelligence_per_dollar_metric": ipd_metric,
        },
        "details": details
    }


def main() -> int:
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
            "Comma-separated OpenRouter model ids (e.g. openai/gpt-oss-120b). "
            "Default: all in get_default_models()."
        ),
    )
    p.add_argument(
        "--api-base",
        default=os.environ.get("OPENAI_API_BASE", DEFAULT_API_BASE),
        help=f"API base URL (default: {DEFAULT_API_BASE}).",
    )
    p.add_argument(
        "--api-key",
        "-k",
        default=os.environ.get("OPENROUTER_API_KEY")
        or os.environ.get("OPENAI_API_KEY", ""),
        help="API key (default: OPENROUTER_API_KEY or OPENAI_API_KEY env).",
    )
    p.add_argument(
        "--example",
        "-e",
        metavar="TASK_ID",
        help="Run only this task_id (e.g. table_from_mess).",
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
        help="Print raw get_lm_usage() when tokens=0 to debug token extraction.",
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
        metavar="ID",
        default="x-ai/grok-4.1-fast",
        help="OpenRouter model id for the judge (default: x-ai/grok-4.1-fast).",
    )
    p.add_argument(
        "--gold-model",
        metavar="ID",
        default="anthropic/claude-sonnet-4.6",
        help="OpenRouter model id for gold standard generation (default: anthropic/claude-sonnet-4.6).",
    )
    p.add_argument(
        "--generate-golds",
        action="store_true",
        help="One-time generation of gold standard answers using the gold-model. Saves to gold_standards.json.",
    )
    p.add_argument(
        "--jobs",
        "-j",
        type=int,
        default=8,
        help="Number of models to run in parallel (default: 8). Use 1 for sequential (verbose) run.",
    )
    args = p.parse_args()

    api_key = args.api_key
    api_base = args.api_base
    if not api_key and "openrouter" in api_base.lower():
        print(
            "Warning: OPENROUTER_API_KEY (or OPENAI_API_KEY) not set.",
            file=sys.stderr,
        )

    model_summaries: list[dict[str, Any]] = []
    all_details: list[dict[str, Any]] = []
    model_ids = _parse_model_ids(args.models)
    unknown = [mid for mid in model_ids if mid not in MODEL_BY_ID]
    if unknown:
        print(f"Unknown model id(s): {unknown}", file=sys.stderr)
        print(
            f"Known ids: {sorted(MODEL_BY_ID.keys())}",
            file=sys.stderr,
        )
        return 1

    # Dataset selection
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
                f"Valid: {[getattr(e, 'task_id', '') for e in to_dspy_examples(ALL_EXAMPLES)]}",
                file=sys.stderr,
            )
            return 1
    if args.n is not None:
        examples = examples[: args.n]

    _tools_lo.VERBOSE = args.verbose

    # One-time gold generation logic
    if args.generate_golds:
        import json
        print(f"Generating gold standards for {len(examples)} examples using {args.gold_model}...")
        gold_lm = _get_lm(args.gold_model, api_key, api_base)
        # We use a dummy program but configure it with the gold LM
        program = build_program(instruction=None, tool_names=None)
        
        # We can't use run_eval_on_examples directly easily because we want to save them
        # so let's just do a simple loop here.
        gold_map = {}
        with dspy.settings.context(lm=gold_lm, cache=False):
            for i, ex in enumerate(examples):
                tid = getattr(ex, "task_id", f"example_{i}")
                print(f"  [{i+1}/{len(examples)}] Generating gold for {tid}...")
                pred = program(document_content=ex.document_content, user_question=ex.user_question)
                gold_map[tid] = getattr(pred, "final_document", "")
        
        out_p = SCRIPT_DIR / "gold_standards.json"
        out_p.write_text(json.dumps(gold_map, indent=2), encoding="utf-8")
        print(f"\nDone! Saved {len(gold_map)} gold standards to {out_p}")
        return 0

    jobs = max(1, args.jobs)
    print(
        f"Running {len(examples)} example(s) for {len(model_ids)} model(s)"
        + (f" ({jobs} in parallel)." if jobs > 1 else " (sequential).")
        + "\nEach example can take 15–60+ seconds (multiple API calls per model)."
    )
    sys.stdout.flush()

    _tools_lo.LOBackend.start()
    try:
        model_summaries: list[dict[str, Any]] = []
        if jobs <= 1:
            # Sequential: verbose per-model and per-example output
            for model_id in model_ids:
                cfg = MODEL_BY_ID[model_id]
                model = model_id
                print("=" * 60)
                print(f"Model: {cfg.display_name} ({cfg.openrouter_id})")
                print(f"  Context window: {cfg.context_window_tokens or 'unknown'} tokens")
                print(f"  Pricing: ${cfg.input_cost_per_million}/M input, "
                      f"${cfg.output_cost_per_million}/M output")
                print(f"  Using model id: {model} @ {api_base}\n")
                
                res = _run_one_model(
                    model_id,
                    api_base,
                    api_key,
                    args.example,
                    args.n,
                    args.verbose,
                    args.debug_usage,
                    not args.no_bust_cache,
                    args.judge,
                    args.gold_model,
                )
                model_summaries.append(res["summary"])
                
                # Add model_id to each detail for tracking
                for d in res["details"]:
                    d["model_id"] = model_id
                all_details.extend(res["details"])
                
                out_path = _out_path(args)
                if out_path:
                    _write_results(out_path, model_summaries)
                    _write_details(out_path, all_details)
                    
                m = res["summary"]
                print(f"Done: {m['openrouter_id']}  avg_correctness={m['avg_correctness']:.3f}  cost=${m['total_cost_usd']:.4f}  ({len(model_summaries)}/{len(model_ids)} models)")
        else:
            # Parallel: worker processes, progress prints interleaved; save after each model
            out_path = _out_path(args)
            with ThreadPoolExecutor(max_workers=jobs) as pool:
                futures = {
                    pool.submit(
                        _run_one_model,
                        model_id,
                        api_base,
                        api_key,
                        args.example,
                        args.n,
                        args.verbose,
                        args.debug_usage,
                        not args.no_bust_cache,
                        args.judge,
                        args.gold_model,
                    ): model_id
                    for model_id in model_ids
                }
                for future in as_completed(futures):
                    model_id = futures[future]
                    try:
                        res = future.result()
                        model_summaries.append(res["summary"])
                        
                        # Add model_id to each detail for tracking
                        for d in res["details"]:
                            d["model_id"] = model_id
                        all_details.extend(res["details"])
                        
                        if out_path:
                            _write_results(out_path, model_summaries)
                            _write_details(out_path, all_details)
                        
                        m = res["summary"]
                        print(f"Done: {m['openrouter_id']}  avg_correctness={m['avg_correctness']:.3f}  cost=${m['total_cost_usd']:.4f}  ({len(model_summaries)}/{len(model_ids)} models)")
                    except Exception as e:
                        print(f"Model {model_id} failed: {e}", file=sys.stderr)
                        cfg = MODEL_BY_ID[model_id]
                        model_summaries.append({
                            "openrouter_id": cfg.openrouter_id,
                            "display_name": cfg.display_name,
                            "context_window_tokens": cfg.context_window_tokens,
                            "input_cost_per_million": cfg.input_cost_per_million,
                            "output_cost_per_million": cfg.output_cost_per_million,
                            "avg_correctness": 0.0,
                            "avg_metric_score": 0.0,
                            "total_tokens": 0,
                            "total_cost_usd": 0.0,
                            "avg_cost_per_example": 0.0,
                            "intelligence_per_dollar_correctness": 0.0,
                            "intelligence_per_dollar_metric": 0.0,
                        })
                        if out_path:
                            _write_results(out_path, model_summaries)
    finally:
        _tools_lo.LOBackend.stop()

    # Print sorted summary
    if not model_summaries:
        print("No models were evaluated.")
        return 0

    model_summaries.sort(
        key=lambda m: m["intelligence_per_dollar_correctness"],
        reverse=True,
    )

    print("=" * 60)
    print("INTELLIGENCE PER DOLLAR (higher is better)")
    print("=" * 60)
    print(
        f"{'Rank':<4}  {'Model':<32}  {'AvgCorr':>7}  {'AvgScore':>8}  "
        f"{'AvgToks':>10}  {'AvgCost($)':>11}  {'Value(C²/$)':>11}"
    )
    for idx, m in enumerate(model_summaries, start=1):
        print(
            f"{idx:<4}  {m['openrouter_id']:<32}  "
            f"{m['avg_correctness']:>7.3f}  "
            f"{m['avg_metric_score']:>8.3f}  "
            f"{m['total_tokens']/len(examples):>10.1f}  "
            f"{m['avg_cost_per_example']:>11.5f}  "
            f"{m['intelligence_per_dollar_correctness']:>11.3f}"
        )

    # Write final results (JSON or CSV by extension); sequential run writes here, parallel already wrote incrementally
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

