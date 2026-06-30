#!/usr/bin/env python3
# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Compare ppt-master PPTX import fidelity (PPTX vs ODP PDF raster diff).

Automated fidelity loop for agents improving ``uno_pptx_import``:

1. Locate or build ``exports/*.pptx`` for the project.
2. Import each slide via ``import_pptx_slide_to_odp`` → one-slide ODP.
3. Export **reference** PDF: ``soffice --convert-to pdf`` on the full PPTX deck.
4. Export **imported** PDF: same on each one-slide ODP.
5. Rasterize PPTX page N vs ODP page 1, pixel-diff → ``diff.png`` + metrics.
6. Write ``report.json`` and ``SUMMARY.md`` under the work dir.

Usage (from repo root; needs ``uno`` Python + ``soffice`` + ``pdftoppm`` or ImageMagick):

    python scripts/ppt_master_import_fidelity.py ppt-master/examples/ppt169_attention_is_all_you_need
    python scripts/ppt_master_import_fidelity.py path/to/project --slides 01_cover 02_body
    python scripts/ppt_master_import_fidelity.py path/to/project --threshold 0.08 --work-dir /tmp/fidelity
    python scripts/ppt_master_import_fidelity.py path/to/project --structural-only

Exit codes:
    0 — all slides passed threshold
    1 — one or more slides failed or runtime error
    2 — LibreOffice UNO bootstrap failed
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


def _bootstrap_ctx():
    try:
        import officehelper
    except ImportError as exc:
        print("ERROR: officehelper not available; use LibreOffice's Python or install UNO bindings.", file=sys.stderr)
        raise SystemExit(2) from exc
    os.environ.setdefault("WRITERAGENT_TESTING", "1")
    try:
        ctx = officehelper.bootstrap()
    except Exception as exc:
        print(f"ERROR: LibreOffice UNO bootstrap failed: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc
    if ctx is None:
        print("ERROR: officehelper.bootstrap() returned None", file=sys.stderr)
        raise SystemExit(2)
    return ctx


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Compare ppt-master PPTX→ODP import fidelity (PDF/PNG diff).")
    parser.add_argument("project", type=Path, help="ppt-master project folder (contains svg_final/ and/or exports/*.pptx)")
    parser.add_argument(
        "--work-dir",
        type=Path,
        default=None,
        help="Output dir (default: <project>/.import_fidelity/)",
    )
    parser.add_argument(
        "--slides",
        nargs="*",
        default=None,
        help="Limit to slide stems or filenames (e.g. 01_cover or 01_cover.svg)",
    )
    parser.add_argument(
        "--threshold",
        type=float,
        default=0.12,
        help="Max diff_fraction to pass (0=identical, 0.12=12%% pixels differ)",
    )
    parser.add_argument("--dpi", type=int, default=150, help="PDF rasterization DPI")
    parser.add_argument(
        "--structural-only",
        action="store_true",
        help="Skip PDF/PNG visual compare; only shape/text counts",
    )
    parser.add_argument("--json", action="store_true", help="Print full report JSON to stdout")
    args = parser.parse_args(argv)

    project = args.project.expanduser().resolve()
    if not project.is_dir():
        print(f"ERROR: not a directory: {project}", file=sys.stderr)
        return 1

    try:
        from plugin.ppt_master.fidelity import run_project_fidelity, write_agent_summary
    except ImportError as exc:
        print(f"ERROR: import plugin failed ({exc}). Run from repo root; try: make manifest", file=sys.stderr)
        return 1

    ctx = _bootstrap_ctx()
    try:
        report = run_project_fidelity(
            ctx,
            project,
            work_dir=args.work_dir,
            slide_names=args.slides,
            threshold=args.threshold,
            dpi=args.dpi,
            skip_visual=args.structural_only,
        )
    except RuntimeError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    summary_path = Path(report.work_dir) / "SUMMARY.md"
    write_agent_summary(report, summary_path)

    print(json.dumps(report.to_dict()["summary"], indent=2))
    print(f"\nArtifacts: {report.work_dir}", file=sys.stderr)
    print(f"Summary:   {summary_path}", file=sys.stderr)
    if args.json:
        print(json.dumps(report.to_dict(), indent=2))

    failed = report.failed_count
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
