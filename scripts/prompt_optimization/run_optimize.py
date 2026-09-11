#!/usr/bin/env python3
"""
Run DSPy MIPROv2 to optimize a named prompt/tool slice (offline only).

Default student is the live string harness (``llm_chat_eval`` / LlmClient
tool loop — same as ``run_eval.py``). ``--student react-mock`` keeps the
legacy DSPy ReAct + ``tools_lo`` path for comparison.

Instruction-only: max_bootstrapped_demos=0 / max_labeled_demos=0.
Does not write into plugin/framework/prompts.py or cells.py.

Usage:
  cd scripts/prompt_optimization
  export OPENROUTER_API_KEY="<your_api_key_here>"
  python run_optimize.py --auto light -j 1 -e data_sorting,tax_column --slice calc_core
  python run_optimize.py --student react-mock
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

# Repo root for imports
SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import dspy
from dspy.teleprompt import MIPROv2

from dataset import (
    ALL_EXAMPLES,
    filter_examples,
    get_trainset_valset,
    parse_task_id_filter,
    to_dspy_examples,
)
from eval_auth import OPENROUTER_DEFAULT_JUDGE, resolve_api_base, resolve_api_key
from metric import (
    SLICE_LENGTH_MAX_RATIO,
    SLICE_LENGTH_PENALTY_LAMBDA,
    make_judge_metric,
)
from model_configs import DEFAULT_EVAL_STUDENT_MODEL
from optimize_slices import DEFAULT_LLM_SLICE, list_slice_names

# OpenRouter defaults
DEFAULT_API_BASE = "https://openrouter.ai/api/v1"
DEFAULT_MODEL = DEFAULT_EVAL_STUDENT_MODEL


def _make_lm(model_id: str, api_base: str, api_key: str) -> dspy.LM:
    if "openrouter" in api_base.lower() and not model_id.startswith("openrouter/"):
        model_id = "openrouter/" + model_id
    return dspy.LM(
        model=model_id,
        api_key=api_key,
        api_base=api_base,
        model_type="chat",
    )


def parse_args(argv: list[str] | None = None):
    p = argparse.ArgumentParser(
        description=(
            "Optimize a named prompt/tool slice with DSPy MIPROv2. "
            "Default student is the live llm_chat_eval harness."
        )
    )
    p.add_argument(
        "--student",
        choices=("llm", "react-mock"),
        default="llm",
        help=(
            "llm (default): live LlmClient tool loop. "
            "react-mock: legacy DSPy ReAct + tools_lo mocks."
        ),
    )
    p.add_argument(
        "--slice",
        default=DEFAULT_LLM_SLICE,
        help=(
            f"Named slice to optimize (llm student). "
            f"One of: {', '.join(list_slice_names())}. Default: {DEFAULT_LLM_SLICE}."
        ),
    )
    p.add_argument("--model", "-m", default=os.environ.get("OPENAI_MODEL", DEFAULT_MODEL),
                   help=f"Student / proposer model id (default: {DEFAULT_MODEL}).")
    p.add_argument("--judge", "-J", default=os.environ.get("WRITERAGENT_JUDGE_MODEL", OPENROUTER_DEFAULT_JUDGE),
                   help=f"Judge model for grading (default: {OPENROUTER_DEFAULT_JUDGE}). Same as run_eval_multi.")
    p.add_argument("--api-base", default=None,
                   help=f"API base URL (default: OpenRouter {DEFAULT_API_BASE}).")
    p.add_argument("--api-key", "-k", default=None,
                   help="API key (CLI/env via eval_auth).")
    p.add_argument("--jobs", "-j", type=int, default=4,
                   help="Parallel jobs for MIPROv2 valset evals (default: 4). Use 1 for a cheap smoke.")
    p.add_argument("--auto", choices=("light", "medium", "heavy"), default="light",
                   help="Exploration level: light (fewer trials), medium, or heavy. Default: light.")
    p.add_argument("--trials", "-t", type=int, default=None,
                   help="Explicit number of Bayesian optimization trials. Overrides --auto.")
    p.add_argument(
        "--example",
        "-e",
        metavar="TASK_ID",
        default=None,
        help="Comma-separated task_id filter (e.g. data_sorting,tax_column).",
    )
    p.add_argument("-n", type=int, default=None, help="Cap examples after -e filter.")
    p.add_argument(
        "--backend",
        choices=("string", "lo"),
        default="string",
        help="Live-student document backend (default: string). Ignored for react-mock.",
    )
    p.add_argument("--verbose", "-v", action="store_true", help="Print live-student tool calls.")
    p.add_argument(
        "--no-length-penalty",
        action="store_true",
        help="Disable the ~2× slice-length penalty.",
    )
    p.add_argument(
        "--out",
        default=None,
        help="Output JSON path (default: optimized_slice.json or optimized_writer_prompt.json).",
    )
    return p.parse_args(argv)


def _winning_instruction(program: Any) -> str:
    getter = getattr(program, "get_slice_text", None)
    if callable(getter):
        return str(getter() or "")
    try:
        if hasattr(program, "react") and hasattr(program.react, "extended_signature"):
            sig = program.react.extended_signature
            if hasattr(sig, "instructions"):
                return str(sig.instructions or "")
        if hasattr(program, "propose") and hasattr(program.propose, "signature"):
            return str(getattr(program.propose.signature, "instructions", "") or "")
    except Exception:
        return ""
    return str(getattr(program, "instruction", "") or "")


def _build_student(args: argparse.Namespace, api_base: str, api_key: str, model: str) -> Any:
    if args.student == "react-mock":
        from program import build_program

        return build_program(instruction=None, tool_names=None)
    from program_llm import build_live_program

    prompt_task_id = ""
    task_ids = parse_task_id_filter(args.example)
    if task_ids:
        prompt_task_id = task_ids[0]
    return build_live_program(
        slice_name=args.slice,
        endpoint=api_base,
        api_key=api_key,
        model=model,
        backend=args.backend,
        verbose=args.verbose,
        prompt_task_id=prompt_task_id,
    )


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    api_base = resolve_api_base(cli_base=args.api_base)
    api_key = resolve_api_key(cli_key=args.api_key)
    model = args.model

    if not api_key and "openrouter" in api_base.lower():
        print("Warning: OPENROUTER_API_KEY (or OPENAI_API_KEY) not set. Set it for OpenRouter.", file=sys.stderr)

    lm = _make_lm(model, api_base, api_key)
    judge_lm = _make_lm(args.judge, api_base, api_key)
    length_weight = 0.0 if args.no_length_penalty else SLICE_LENGTH_PENALTY_LAMBDA
    metric = make_judge_metric(
        judge_lm,
        slice_length_max_ratio=SLICE_LENGTH_MAX_RATIO,
        slice_length_penalty_lambda=length_weight,
    )

    print(f"Student: {args.student}  slice={args.slice if args.student == 'llm' else 'react-instruction'}")
    print(f"Using model: {model} @ {api_base}")
    print(f"Judge: {args.judge}")
    dspy.configure(lm=lm)

    filtered = filter_examples(ALL_EXAMPLES, parse_task_id_filter(args.example), args.n)
    if args.example and not filtered:
        print(
            f"No examples matched -e {args.example!r}. "
            f"Valid: {[ex.get('task_id', '') for ex in ALL_EXAMPLES]}",
            file=sys.stderr,
        )
        return 1

    if args.example or args.n is not None:
        # Small targeted runs: same rows for train and val (smoke / Calc slice).
        trainset_raw = filtered
        valset_raw = filtered
    else:
        trainset_raw, valset_raw = get_trainset_valset(split=0.8, seed=42, examples=filtered)

    trainset = to_dspy_examples(trainset_raw, with_inputs=True)
    valset = to_dspy_examples(valset_raw, with_inputs=True)

    if len(trainset) < 2 or len(valset) < 1:
        all_ex = to_dspy_examples(filtered or ALL_EXAMPLES, with_inputs=True)
        trainset = all_ex
        valset = all_ex[: max(1, len(all_ex) // 2)]

    print(f"Trainset: {len(trainset)}, Valset: {len(valset)}")

    program = _build_student(args, api_base, api_key, model)
    use_explicit_trials = args.trials is not None
    if use_explicit_trials:
        teleprompter = MIPROv2(
            metric=metric,
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
            metric=metric,
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

    default_name = (
        "optimized_writer_prompt.json" if args.student == "react-mock" else "optimized_slice.json"
    )
    out_path = Path(args.out) if args.out else SCRIPT_DIR / default_name
    if not out_path.is_absolute():
        out_path = SCRIPT_DIR / out_path
    optimized.save(str(out_path))
    print(f"Saved optimized program to {out_path}")

    winning = _winning_instruction(optimized)
    sidecar = {
        "student": args.student,
        "slice": args.slice if args.student == "llm" else "react-instruction",
        "instruction": winning,
        "baseline_len": len(getattr(program, "baseline_slice_text", "") or getattr(program, "instruction", "") or ""),
        "optimized_len": len(winning),
    }
    sidecar_path = out_path.with_name(out_path.stem + "_slice.json")
    sidecar_path.write_text(json.dumps(sidecar, indent=2) + "\n", encoding="utf-8")
    print(f"Saved slice text to {sidecar_path}")
    if winning:
        print("\n--- Optimized slice (preview) ---")
        print(winning[:800] + "..." if len(winning) > 800 else winning)
        print("Copy into prompts.py / cells.py by hand after a ranking re-run. Do not auto-merge.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
