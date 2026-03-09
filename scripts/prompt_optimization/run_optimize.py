#!/usr/bin/env python3
"""
Run DSPy MIPROv2 to optimize the Writer system prompt.
Uses mock tools and fixed examples; 0-shot instruction-only.

Defaults to OpenRouter with qwen/qwen3-coder-next (cheap and fast).
Override model or endpoint via env or CLI.

Usage:
  cd scripts/prompt_optimization
  pip install dspy-ai
  export OPENROUTER_API_KEY="your-key"   # or OPENAI_API_KEY
  python run_optimize.py
  python run_optimize.py --model google/gemini-3.1-flash-lite-preview
  python run_optimize.py --model qwen/qwen3-coder-next --api-base https://openrouter.ai/api/v1
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

# Repo root for imports
SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import dspy
from dspy.teleprompt import MIPROv2

from dataset import ALL_EXAMPLES, get_trainset_valset, to_dspy_examples
from program import build_program
from metric import writer_assistant_metric

# OpenRouter defaults
DEFAULT_API_BASE = "https://openrouter.ai/api/v1"
DEFAULT_MODEL = "qwen/qwen3-coder-next"


def parse_args():
    p = argparse.ArgumentParser(description="Optimize Writer system prompt with DSPy MIPROv2 (OpenRouter by default).")
    p.add_argument("--model", "-m", default=os.environ.get("OPENAI_MODEL", DEFAULT_MODEL),
                   help=f"Model id (default: {DEFAULT_MODEL}). OpenRouter model e.g. qwen/qwen3-coder-next, google/gemini-3.1-flash-lite-preview.")
    p.add_argument("--api-base", default=os.environ.get("OPENAI_API_BASE", DEFAULT_API_BASE),
                   help=f"API base URL (default: OpenRouter {DEFAULT_API_BASE}).")
    p.add_argument("--api-key", "-k", default=os.environ.get("OPENROUTER_API_KEY") or os.environ.get("OPENAI_API_KEY", ""),
                   help="API key (default: OPENROUTER_API_KEY or OPENAI_API_KEY env).")
    p.add_argument("--jobs", "-j", type=int, default=4,
                   help="Parallel jobs for MIPROv2 valset evals (default: 4).") 
    p.add_argument("--auto", choices=("light", "medium", "heavy"), default="light",
                   help="Exploration level: light (fewer trials), medium, or heavy (more trials). Default: light.")
    p.add_argument("--trials", "-t", type=int, default=None,
                   help="Explicit number of Bayesian optimization trials. If set, overrides --auto and uses more exploration.")
    return p.parse_args()


def main():
    args = parse_args()
    api_base = args.api_base
    api_key = args.api_key
    model = args.model

    if not api_key and "openrouter" in api_base.lower():
        print("Warning: OPENROUTER_API_KEY (or OPENAI_API_KEY) not set. Set it for OpenRouter.", file=sys.stderr)

    # LiteLLM (used by DSPy) needs provider prefix for OpenRouter: openrouter/model-id
    if "openrouter" in api_base.lower() and not model.startswith("openrouter/"):
        model = "openrouter/" + model

    print(f"Using model: {model} @ {api_base}")
    lm = dspy.LM(
        model=model,
        api_key=api_key,
        api_base=api_base,
        model_type="chat",
    )
    dspy.configure(lm=lm)

    # Dataset: convert to dspy.Example with inputs
    trainset_raw, valset_raw = get_trainset_valset(split=0.8, seed=42)
    trainset = to_dspy_examples(trainset_raw, with_inputs=True)
    valset = to_dspy_examples(valset_raw, with_inputs=True)

    if len(trainset) < 2 or len(valset) < 1:
        # Fallback: use all as both train and val for tiny runs
        all_ex = to_dspy_examples(ALL_EXAMPLES, with_inputs=True)
        trainset = all_ex
        valset = all_ex[: max(1, len(all_ex) // 2)]

    print(f"Trainset: {len(trainset)}, Valset: {len(valset)}")

    # Program and optimizer: 0-shot instruction-only
    program = build_program(instruction=None, tool_names=None)
    use_explicit_trials = args.trials is not None
    if use_explicit_trials:
        # auto=None requires num_candidates; num_trials passed to compile()
        teleprompter = MIPROv2(
            metric=writer_assistant_metric,
            auto=None,
            num_candidates=max(10, min(25, args.trials // 2)),
            max_bootstrapped_demos=0,
            max_labeled_demos=0,
            num_threads=args.jobs,
        )
        compile_kw = {"num_trials": args.trials}
        run_desc = f"instruction-only, {args.trials} trials, {args.jobs} jobs"
    else:
        teleprompter = MIPROv2(
            metric=writer_assistant_metric,
            auto=args.auto,
            max_bootstrapped_demos=0,
            max_labeled_demos=0,
            num_threads=args.jobs,
        )
        compile_kw = {}
        run_desc = f"instruction-only, auto={args.auto}, {args.jobs} jobs"

    print(f"Running MIPROv2 ({run_desc})...")
    with dspy.settings.context(lm=lm, track_usage=True, cache=False):
        optimized = teleprompter.compile(
            program,
            trainset=trainset,
            valset=valset,
            **compile_kw,
        )

    # Save and print winning instruction
    out_path = SCRIPT_DIR / "optimized_writer_prompt.json"
    optimized.save(str(out_path))
    print(f"Saved optimized program to {out_path}")

    # Try to print the winning instruction (may be on the ReAct's predictor signature)
    try:
        if hasattr(optimized, "react") and hasattr(optimized.react, "extended_signature"):
            sig = optimized.react.extended_signature
            if hasattr(sig, "instructions"):
                print("\n--- Optimized instruction (preview) ---")
                print(sig.instructions[: 800] + "..." if len(str(sig.instructions)) > 800 else sig.instructions)
    except Exception as e:
        print(f"(Could not print instruction: {e})")

    return 0


if __name__ == "__main__":
    sys.exit(main())

