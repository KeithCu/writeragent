#!/usr/bin/env python3
"""
Run the Writer assistant on the dataset (no optimization).
Shows per-example: task_id, expected/reject checks, correctness, tokens, score, and doc snippet.

Usage:
  export OPENROUTER_API_KEY="your-key"   # or OPENAI_API_KEY
  cd scripts/prompt_optimization
  python run_eval.py                    # run all examples
  python run_eval.py --example table_from_mess   # run one task_id
  python run_eval.py -n 2               # run first 2 examples only
  python run_eval.py -v                 # verbose: print every tool call
  python run_eval.py --compare-with optimized_writer_prompt.json   # run both prompts, report diff
  python run_eval.py --no-bust-cache   # disable cache-busting (default: enabled)
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import uuid
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import dspy

from dataset import ALL_EXAMPLES, to_dspy_examples
from program import build_program
from metric import TOKEN_PENALTY_LAMBDA
from eval_core import ExampleEval, run_eval_on_examples, summarize_results
import tools_lo

DEFAULT_API_BASE = "https://openrouter.ai/api/v1"
DEFAULT_MODEL = "nvidia/llama-3.3-nemotron-super-49b-v1.5"


def _load_prompt_from_json(path: Path) -> str:
    """Load instruction from a DSPy saved program JSON (optimized_writer_prompt.json)."""
    data = json.loads(path.read_text())
    # react.react.signature.instructions is the main ReAct predictor's system prompt
    try:
        return data["react.react"]["signature"]["instructions"]
    except KeyError:
        raise ValueError(f"Could not find react.react.signature.instructions in {path}")


def main():
    p = argparse.ArgumentParser(description="Eval Writer assistant on dataset (no MIPROv2).")
    p.add_argument("--model", "-m", default=os.environ.get("OPENAI_MODEL", DEFAULT_MODEL))
    p.add_argument("--api-base", default=os.environ.get("OPENAI_API_BASE", DEFAULT_API_BASE))
    p.add_argument("--api-key", "-k", default=os.environ.get("OPENROUTER_API_KEY") or os.environ.get("OPENAI_API_KEY", ""))
    p.add_argument("--example", "-e", metavar="TASK_ID", help="Run only this task_id (e.g. table_from_mess).")
    p.add_argument("-n", type=int, default=None, help="Run only first N examples.")
    p.add_argument("--verbose", "-v", action="store_true", help="Print every tool call as it runs.")
    p.add_argument("--compare-with", metavar="JSON", help="Compare: run with current prompt, then with prompt from JSON file, report both scores.")
    p.add_argument("--debug-usage", action="store_true", help="Print raw get_lm_usage() when tokens=0 to debug token extraction.")
    p.add_argument("--no-bust-cache", action="store_true", help="Disable cache-busting (default: enabled for accurate token counts with OpenRouter).")
    args = p.parse_args()

    api_key = args.api_key
    api_base = args.api_base
    model = args.model
    if "openrouter" in api_base.lower() and not model.startswith("openrouter/"):
        model = "openrouter/" + model

    if not api_key and "openrouter" in api_base.lower():
        print("Warning: OPENROUTER_API_KEY (or OPENAI_API_KEY) not set.", file=sys.stderr)

    print(f"Model: {model} @ {api_base}\n")
    lm = dspy.LM(model=model, api_key=api_key, api_base=api_base, model_type="chat")
    dspy.configure(lm=lm)

    examples = to_dspy_examples(ALL_EXAMPLES, with_inputs=True)
    if args.example:
        examples = [ex for ex in examples if getattr(ex, "task_id", "") == args.example]
        if not examples:
            print(f"No example with task_id={args.example!r}. Valid: {[getattr(e, 'task_id', '') for e in to_dspy_examples(ALL_EXAMPLES)]}")
            return 1
    if args.n is not None:
        examples = examples[: args.n]

    tools_lo.VERBOSE = args.verbose
    n = len(examples)
    print(f"Running {n} example(s). Each can take 15–60+ seconds (multiple API calls). Total often 2–10 min.\n")
    sys.stdout.flush()

    tools_lo.LOBackend.start()
    try:
        if args.compare_with:
            # Compare mode: run both prompts and report
            compare_path = Path(args.compare_with)
            if not compare_path.is_absolute():
                compare_path = SCRIPT_DIR / compare_path
            if not compare_path.exists():
                print(f"Error: --compare-with file not found: {compare_path}", file=sys.stderr)
                return 1
            try:
                alt_instruction = _load_prompt_from_json(compare_path)
            except ValueError as e:
                print(f"Error: {e}", file=sys.stderr)
                return 1

            print("=" * 60)
            print("PROMPT A: Current (from core/constants.py)")
            print("=" * 60)
            program_a = build_program(instruction=None, tool_names=None)
            results_a = run_eval_on_examples(
                program_a,
                examples,
                verbose=args.verbose,
                debug_usage=args.debug_usage,
                bust_cache=not args.no_bust_cache,
            )
            summary_a = summarize_results(results_a)

            print("=" * 60)
            print(f"PROMPT B: From {compare_path.name}")
            print("=" * 60)
            program_b = build_program(instruction=alt_instruction, tool_names=None)
            results_b = run_eval_on_examples(
                program_b,
                examples,
                verbose=args.verbose,
                debug_usage=args.debug_usage,
                bust_cache=not args.no_bust_cache,
            )
            summary_b = summarize_results(results_b)

            avg_a = summary_a["avg_metric_score"]
            total_tokens_a = summary_a["total_tokens"]
            avg_b = summary_b["avg_metric_score"]
            total_tokens_b = summary_b["total_tokens"]

            print("=" * 60)
            print("COMPARISON")
            print("=" * 60)
            print(f"  Current (git):  avg score = {avg_a:.3f}  total tokens = {total_tokens_a}")
            print(f"  Optimized:      avg score = {avg_b:.3f}  total tokens = {total_tokens_b}")
            diff = avg_b - avg_a
            if diff > 0:
                print(f"  -> Optimized is {diff:.3f} better (higher score)")
            elif diff < 0:
                print(f"  -> Current is {-diff:.3f} better (higher score)")
            else:
                print("  -> Tie")
            return 0

        program = build_program(instruction=None, tool_names=None)
        results = run_eval_on_examples(
            program,
            examples,
            verbose=args.verbose,
            debug_usage=args.debug_usage,
            bust_cache=not args.no_bust_cache,
        )
        summary = summarize_results(results)
        if results:
            print(
                f"Average score: {summary['avg_metric_score']:.3f} "
                f"({len(results)} examples)"
            )
        return 0
    finally:
        tools_lo.LOBackend.stop()


if __name__ == "__main__":
    sys.exit(main())
