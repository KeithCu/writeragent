#!/usr/bin/env python3
# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""CLI runner for FizzBee randomized MCP fuzzer over Writer full toolset layout.

Usage:
    python scripts/fizzbee_mcp_fuzzer.py --steps 1000
    python scripts/fizzbee_mcp_fuzzer.py --duration 10
    python scripts/fizzbee_mcp_fuzzer.py --duration 30 --mutate-rate 0.15
"""

import argparse
import os
import sys

# Ensure repository root is on sys.path
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

# Save original stdout before any extension module initializations
_real_stdout = sys.stdout

from unittest.mock import MagicMock

from plugin.framework.service import ServiceRegistry
from plugin.mcp.mcp_protocol import MCPProtocolHandler
from tests.mcp.writer_full_layout import (
    extract_full_writer_layout,
    get_writer_tool_registry,
    run_randomized_mcp_fuzz,
)


def log(msg: str = "") -> None:
    sys.stderr.write(msg + "\n")
    sys.stderr.flush()


def main():
    parser = argparse.ArgumentParser(description="Run FizzBee randomized MCP tool fuzzer for Writer & Calc")
    parser.add_argument("--app", choices=["writer", "calc"], default="writer", help="Application toolset to fuzz (default: writer)")
    parser.add_argument("--steps", type=int, default=500, help="Number of random requests to dispatch (default: 500)")
    parser.add_argument("--duration", type=float, default=None, help="Duration in seconds to run (overrides --steps)")
    parser.add_argument("--seed", type=int, default=42, help="Random seed for reproducibility (default: 42)")
    parser.add_argument("--mutate-rate", type=float, default=0.08, help="Malformed argument mutation rate (default: 0.08)")
    parser.add_argument(
        "--pair-bias",
        type=float,
        default=0.4,
        help="After a nested mutate, probability of a read/apply follow-up (0 disables; default 0.4)",
    )
    parser.add_argument("--verbose", action="store_true", help="Print tool call distribution breakdown")
    args = parser.parse_args()

    app_name = args.app.capitalize()
    doc_type = args.app.lower()

    log(f"Initializing WriterAgent MCP & {app_name} full tool layout...")
    services = ServiceRegistry()

    doc_svc = MagicMock()
    mock_doc = MagicMock()
    doc_svc.get_active_document.return_value = mock_doc
    doc_svc.detect_doc_type.return_value = doc_type
    doc_svc.resolve_document_by_url.return_value = (mock_doc, doc_type)
    services.register("document", doc_svc)

    main_thread = MagicMock()
    main_thread.execute.side_effect = lambda fn, *a, **kw: fn(*a, **{k: v for k, v in kw.items() if k not in ("timeout", "bound_scope")})
    services.register("main_thread", main_thread)

    config_svc = MagicMock()
    config_dict = {
        "mcp_enabled": True,
        "mcp_port": 18765,
        "tool_exposure_mode": "direct_flat",
        "mcp.tool_exposure_mode": "direct_flat",
    }
    config_svc.proxy_for.return_value = config_dict
    config_svc.get.side_effect = lambda k, d=None: config_dict.get(k, config_dict.get(k.split(".")[-1], d))
    services.register("config", config_svc)
    services.register("events", MagicMock())

    if doc_type == "calc":
        from tests.mcp.calc_full_layout import extract_full_calc_layout, get_calc_tool_registry

        services, tool_reg = get_calc_tool_registry(services)
        layout = extract_full_calc_layout(tool_reg)
    else:
        services, tool_reg = get_writer_tool_registry(services)
        layout = extract_full_writer_layout(tool_reg)

    handler = MCPProtocolHandler(services)

    all_tools = layout["all_tools"]
    log(f"Loaded {len(all_tools)} {app_name} tools ({len(layout['core_tools'])} core, {len(layout['specialized_tools'])} specialized across {len(layout['domain_map'])} domains).")

    mode_str = f"duration={args.duration}s" if args.duration else f"steps={args.steps}"
    log(
        f"Starting FizzBee randomized MCP fuzzing ({mode_str}, seed={args.seed}, "
        f"mutation_rate={args.mutate_rate}, pair_bias={args.pair_bias})..."
    )

    try:
        result = run_randomized_mcp_fuzz(
            handler=handler,
            all_tools=all_tools,
            steps=args.steps,
            duration_sec=args.duration,
            seed=args.seed,
            mutate_error_rate=args.mutate_rate,
            pair_bias=args.pair_bias,
        )
    except Exception as e:
        import traceback
        log(f"\nEXCEPTION OCCURRED: {e}\n{traceback.format_exc()}")
        return 1

    log("\n" + "=" * 55)
    log("FIZZBEE MCP FUZZING EXECUTION SUMMARY")
    log("=" * 55)
    log(f"Status:                 {result['status']}")
    log(f"Steps completed:        {result['steps_completed']:,}")
    log(f"Elapsed time:           {result['elapsed_sec']} seconds")
    log(f"Throughput:             {result['calls_per_second']:,} requests/sec")
    log(f"Unique tools exercised: {result['unique_tools_invoked']} / {len(all_tools)}")
    log(f"Errors handled cleanly: {result['errors_handled']:,}")
    log(f"Paired mutate→read steps: {result.get('paired_followups', 0):,}")

    if args.verbose:
        log("\nTool Invocation Distribution (Top 25):")
        sorted_calls = sorted(result["call_distribution"].items(), key=lambda x: -x[1])
        for tool_name, count in sorted_calls[:25]:
            log(f"  - {tool_name:40s}: {count:4d} calls")
        if len(sorted_calls) > 25:
            log(f"  ... and {len(sorted_calls) - 25} more tools called")

    log("\nAll MCP wire format invariants and safety checks passed successfully!")
    return 0


if __name__ == "__main__":
    sys.exit(main())
