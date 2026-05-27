#!/usr/bin/env python3
# WriterAgent - manual A/B serialization round-trip runner (outside LibreOffice).
"""Compare force=always (split_grid) vs force=never (nested list).

Example:
  .venv/bin/python scripts/run_serialization_ab.py --list
  .venv/bin/python scripts/run_serialization_ab.py --grid numeric_4x4 --transform sum
  .venv/bin/python scripts/run_serialization_ab.py --grid mixed_zip --transform echo --subprocess
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

project_root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(project_root))

from plugin.scripting.venv_worker import PythonWorkerManager
from tests.scripting.serialization_ab_support import (
    VENV_TRANSFORMS,
    all_codec_ab_cases,
    assert_codec_split_vs_nosplit_parity,
    assert_venv_always_never_parity,
    prepare_grid,
)


def _default_encoder(obj: Any) -> Any:
    import numpy as np

    if isinstance(obj, np.ndarray):
        return obj.tolist()
    raise TypeError(repr(obj))


def main() -> int:
    parser = argparse.ArgumentParser(description="Manual always vs never serialization A/B runner")
    parser.add_argument("--list", action="store_true", help="List named grid fixture ids")
    parser.add_argument("--grid", default="numeric_4x4", help="Fixture id from --list")
    parser.add_argument(
        "--transform",
        choices=tuple(VENV_TRANSFORMS.keys()),
        default="echo",
        help="Worker transform (maps to venv code)",
    )
    parser.add_argument("--subprocess", action="store_true", help="Use PythonWorkerManager (full pickle IPC)")
    parser.add_argument("--codec-only", action="store_true", help="Only compare codec decode (no venv)")
    parser.add_argument(
        "--force",
        choices=("both", "always", "never"),
        default="both",
        help="Run one path or compare always vs never (default: both)",
    )
    args = parser.parse_args()

    cases = {c.id: c for c in all_codec_ab_cases()}
    if args.list:
        for cid in sorted(cases):
            print(cid)
        return 0

    if args.grid not in cases:
        print(f"Unknown grid {args.grid!r}. Use --list.", file=sys.stderr)
        return 1

    case = cases[args.grid]
    grid = prepare_grid(case)
    code = VENV_TRANSFORMS[args.transform]

    if args.codec_only:
        try:
            assert_codec_split_vs_nosplit_parity(grid, label=args.grid)
            print("codec always/never: OK")
        except AssertionError as e:
            print(f"codec always/never: FAIL — {e}", file=sys.stderr)
            return 1
        return 0

    results: dict[str, Any] = {}
    try:
        if args.force == "both":
            always, never = assert_venv_always_never_parity(
                grid,
                code,
                use_subprocess=args.subprocess,
                label=args.grid,
            )
            results = {"always": always, "never": never}
            print("venv always/never: OK")
        else:
            from tests.scripting.serialization_ab_support import run_venv_roundtrip

            results[args.force] = run_venv_roundtrip(
                grid,
                code,
                pack_force=args.force,
                use_subprocess=args.subprocess,
            )
        print(json.dumps(results, default=_default_encoder, indent=2))
    except AssertionError as e:
        print(f"FAIL — {e}", file=sys.stderr)
        print(json.dumps(results, default=_default_encoder, indent=2), file=sys.stderr)
        return 1
    finally:
        if args.subprocess:
            PythonWorkerManager.shutdown_all()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
