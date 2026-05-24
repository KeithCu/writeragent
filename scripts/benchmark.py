#!/usr/bin/env python3
# WriterAgent - simple benchmark CLI (repo root).
# Copyright (c) 2026 KeithCu
# SPDX-License-Identifier: GPL-3.0-or-later
"""
Run WriterAgent benchmarks from the repository root.

Usage:
    export OPENROUTER_API_KEY=sk-…
    python scripts/benchmark.py --model qwen/qwen3-coder-next -n 2
    python scripts/benchmark.py --endpoint http://127.0.0.1:11434/v1 --model llama3.2 --allow-unknown-model -n 1
    make run_eval EVAL_ARGS="--models qwen/qwen3-coder-next -n 1 -j 1"
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
PO_DIR = SCRIPT_DIR / "prompt_optimization"
RUN_EVAL_MULTI = PO_DIR / "run_eval_multi.py"


def build_eval_argv(
    *,
    model: str | None = None,
    models: str | None = None,
    api_key: str | None = None,
    endpoint: str | None = None,
    output: str | None = None,
    backend: str = "string",
    examples: int | None = None,
    parallel: int = 4,
    verbose: bool = False,
    allow_unknown_model: bool = False,
    no_judge: bool = False,
    task: str | None = None,
    document: str | None = None,
    category: str | None = None,
    extra: list[str] | None = None,
) -> list[str]:
    """Build argv for run_eval_multi.py (testable without subprocess)."""
    if task or document:
        raise ValueError("Custom --task/--document not implemented yet; use run_eval_multi -e TASK_ID")

    cmd = [sys.executable, str(RUN_EVAL_MULTI)]
    if model:
        cmd.extend(["--models", model])
    elif models:
        cmd.extend(["--models", models])
    if api_key:
        cmd.extend(["--api-key", api_key])
    if endpoint:
        cmd.extend(["--api-base", endpoint])
    if output:
        cmd.extend(["--out", output])
    cmd.extend(["--backend", backend])
    if examples is not None:
        cmd.extend(["-n", str(examples)])
    if verbose:
        cmd.extend(["--verbose", "-j", "1"])
    else:
        cmd.extend(["-j", str(parallel)])
    if allow_unknown_model:
        cmd.append("--allow-unknown-model")
    if no_judge:
        cmd.append("--no-judge")
    if extra:
        cmd.extend(extra)
    return cmd


def main() -> int:
    # Resolve credentials before spawning child (clear errors at repo root).
    if str(PO_DIR) not in sys.path:
        sys.path.insert(0, str(PO_DIR))
    from eval_auth import require_api_key, resolve_api_base, resolve_api_key

    parser = argparse.ArgumentParser(description="WriterAgent benchmark CLI")
    parser.add_argument("--model", "-m", help="Single model to benchmark")
    parser.add_argument("--models", help="Comma-separated list of models")
    parser.add_argument("--api-key", "-k", default=None, help="API key")
    parser.add_argument(
        "--endpoint",
        default=None,
        help="API base URL (alias for run_eval_multi --api-base)",
    )
    parser.add_argument("--output", "-o", default="benchmark_results.json", help="Output .json or .csv")
    parser.add_argument("--backend", choices=["string", "lo"], default="string")
    parser.add_argument("--examples", "-n", type=int, default=None, help="Number of examples")
    parser.add_argument("--parallel", "-j", type=int, default=4, help="Parallel model jobs")
    parser.add_argument("--verbose", "-v", action="store_true")
    parser.add_argument("--allow-unknown-model", action="store_true")
    parser.add_argument("--no-judge", action="store_true", help="Skip LLM judge")
    parser.add_argument("--task", help="Custom task prompt (Phase 2)")
    parser.add_argument("--document", help="Custom document content (Phase 2)")
    parser.add_argument("--category", choices=["structural", "creative"], default="structural")
    args, extra = parser.parse_known_args()

    api_base = resolve_api_base(cli_base=args.endpoint)
    api_key = resolve_api_key(cli_key=args.api_key)
    require_api_key(api_key, api_base)

    if args.task or args.document:
        print("Custom --task/--document: not implemented yet.", file=sys.stderr)
        return 1

    cmd = build_eval_argv(
        model=args.model,
        models=args.models,
        api_key=api_key,
        endpoint=api_base,
        output=args.output,
        backend=args.backend,
        examples=args.examples,
        parallel=args.parallel,
        verbose=args.verbose,
        allow_unknown_model=args.allow_unknown_model,
        no_judge=args.no_judge,
        extra=extra,
    )
    return subprocess.run(cmd, cwd=PO_DIR, check=False).returncode


if __name__ == "__main__":
    sys.exit(main())
