#!/usr/bin/env python3
"""
Run the Writer assistant across multiple models and compare intelligence per dollar.

This reuses the same dataset and metric as run_eval.py (LlmClient tool loop;
default in-memory `--backend string`), but iterates over model configurations
(see model_configs.py) and estimates cost using list prices (USD per 1M tokens).

Usage:
  export OPENROUTER_API_KEY="your-key"   # or OPENAI_API_KEY / WRITERAGENT_API_KEY
  cd scripts/prompt_optimization
  python run_eval_multi.py
  python run_eval_multi.py --backend lo  # LibreOffice instead of string simulator
  python run_eval_multi.py --models openai/gpt-oss-120b,openai/gpt-4o-mini
  python run_eval_multi.py -n 2
  python run_eval_multi.py -j 8   # 8 models in parallel (default)
  python run_eval_multi.py -j 1   # sequential, verbose per-example output
  python run_eval_multi.py --allow-unknown-model --models llama3.2
"""
from __future__ import annotations

import argparse
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Iterable, Sequence

from dataset import ALL_EXAMPLES, to_dspy_examples
from eval_auth import (
    require_api_key,
    resolve_api_base,
    resolve_api_key,
    resolve_judge_model,
)
from eval_core import ExampleEval, run_eval_on_examples_llm
from model_configs import MODEL_BY_ID, ModelConfig, get_default_models
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
    return [s.strip() for s in arg.split(",") if s.strip()]


def _model_id_for_llm_client(model_id: str) -> str:
    """Strip ``openrouter/`` for ``LlmClient`` (OpenRouter HTTP API uses ``provider/model``)."""
    m = model_id
    if m.startswith("openrouter/"):
        m = m[len("openrouter/") :]
    return m


def _model_config_for_id(model_id: str, *, allow_unknown: bool) -> ModelConfig:
    if model_id in MODEL_BY_ID:
        return MODEL_BY_ID[model_id]
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
    judge_model_id: str | None,
    gold_model_id: str | None,
    backend: str,
    allow_unknown: bool,
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
    )
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
            "pricing_known": pricing_known,
            "avg_correctness": summary["avg_correctness"],
            "avg_metric_score": summary["avg_metric_score"],
            "total_tokens": summary["total_tokens"],
            "total_cost_usd": total_cost,
            "avg_cost_per_example": avg_cost_per_example,
            "intelligence_per_dollar_correctness": ipd_correctness,
            "intelligence_per_dollar_metric": ipd_metric,
        },
        "details": details,
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
        help="Judge model id (default: Grok on OpenRouter; else first --models id on other endpoints).",
    )
    p.add_argument(
        "--no-judge",
        action="store_true",
        help="Skip LLM judge; use expected_contains/reject_contains only.",
    )
    p.add_argument(
        "--gold-model",
        metavar="ID",
        default="anthropic/claude-sonnet-4.6",
        help="Model id for gold standard generation (default: anthropic/claude-sonnet-4.6).",
    )
    p.add_argument(
        "--generate-golds",
        action="store_true",
        help=(
            "Generate gold answers with --gold-model (default Sonnet; costly with tool-calling). "
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
        default=8,
        help="Number of models to run in parallel (default: 8). Use 1 for sequential (verbose) run.",
    )
    p.add_argument(
        "--backend",
        choices=("string", "lo"),
        default="string",
        help=(
            "Document backend: 'string' (in-memory HTML, default) or "
            "'lo' (headless Writer)."
        ),
    )
    args = p.parse_args()

    api_base = resolve_api_base(cli_base=args.api_base)
    api_key = resolve_api_key(cli_key=args.api_key)
    require_api_key(api_key, api_base)

    model_summaries: list[dict[str, Any]] = []
    all_details: list[dict[str, Any]] = []
    model_ids = _parse_model_ids(args.models)
    unknown = [mid for mid in model_ids if mid not in MODEL_BY_ID]
    if unknown and not args.allow_unknown_model:
        print(f"Unknown model id(s): {unknown}", file=sys.stderr)
        print(f"Known ids: {sorted(MODEL_BY_ID.keys())}", file=sys.stderr)
        print("Pass --allow-unknown-model for local/custom endpoints.", file=sys.stderr)
        return 1

    judge_model_id: str | None = None
    if not args.no_judge:
        judge_model_id = resolve_judge_model(
            cli_judge=args.judge,
            endpoint=api_base,
            model_ids=model_ids,
        )
        print(f"Judge model: {judge_model_id} @ {api_base}")

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

        from llm_chat_eval import run_llm_chat_eval
        from plugin.framework.prompts import get_writer_eval_chat_system_prompt

        if len(examples) > 1 and not args.yes_multi_gold:
            print(
                "Refusing: --generate-golds would run multiple examples (multiple costly --gold-model calls). "
                "Run one task: add -e <task_id> or -n 1, or pass --yes-multi-gold to generate many in one go.",
                file=sys.stderr,
            )
            return 1
        print(f"Generating gold standards for {len(examples)} examples using {args.gold_model}...")
        gm = _model_id_for_llm_client(args.gold_model)
        inst = get_writer_eval_chat_system_prompt()

        gold_map: dict[str, str] = {}
        if args.backend == "lo":
            _tools_lo.LOBackend.start()
        try:
            for i, ex in enumerate(examples):
                tid = getattr(ex, "task_id", f"example_{i}")
                print(f"  [{i+1}/{len(examples)}] Generating gold for {tid}...")
                html, _, gerr = run_llm_chat_eval(
                    system_prompt=inst,
                    document_content=ex.document_content,
                    user_question=ex.user_question,
                    endpoint=api_base,
                    api_key=api_key,
                    model=gm,
                    backend=args.backend,
                    verbose=args.verbose,
                )
                if gerr:
                    print(f"  Warning: gold error for {tid}: {gerr}", file=sys.stderr)
                gold_map[tid] = html
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
        print(f"\nDone! Saved {len(gold_map)} gold standard(s) to {out_p} (merged with existing keys).")
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
        gold_model_id=args.gold_model,
        backend=args.backend,
        allow_unknown=args.allow_unknown_model,
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
        if args.backend == "lo":
            _tools_lo.LOBackend.stop()

    if not model_summaries:
        print("No models were evaluated.")
        return 0

    any_pricing = any(m.get("pricing_known") for m in model_summaries)
    if any_pricing:
        model_summaries.sort(
            key=lambda m: m["intelligence_per_dollar_correctness"],
            reverse=True,
        )
    else:
        model_summaries.sort(key=lambda m: m["avg_correctness"], reverse=True)

    print("=" * 60)
    if any_pricing:
        print("INTELLIGENCE PER DOLLAR (higher is better)")
    else:
        print("RESULTS (sorted by avg correctness; cost/IpD n/a — unknown pricing)")
    print("=" * 60)
    print(
        f"{'Rank':<4}  {'Model':<32}  {'AvgCorr':>7}  {'AvgScore':>8}  "
        f"{'AvgToks':>10}  {'AvgCost($)':>11}  {'Value(C²/$)':>11}"
    )
    n_ex = max(len(examples), 1)
    for idx, m in enumerate(model_summaries, start=1):
        if m.get("pricing_known"):
            cost_col = f"{m['avg_cost_per_example']:>11.5f}"
            ipd_col = f"{m['intelligence_per_dollar_correctness']:>11.3f}"
        else:
            cost_col = f"{'n/a':>11}"
            ipd_col = f"{'n/a':>11}"
        print(
            f"{idx:<4}  {m['openrouter_id']:<32}  "
            f"{m['avg_correctness']:>7.3f}  "
            f"{m['avg_metric_score']:>8.3f}  "
            f"{m['total_tokens']/n_ex:>10.1f}  "
            f"{cost_col}  "
            f"{ipd_col}"
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
