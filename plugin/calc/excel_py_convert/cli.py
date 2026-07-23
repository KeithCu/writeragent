# SPDX-License-Identifier: GPL-3.0-or-later
"""CLI: convert Excel Python-in-Excel scripts ↔ DAG-style ``=PY(code; ranges)``.

``--to excel`` is a script/dependency export (not native ``pythonScripts.xml``).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path", type=Path, help="Input .xlsx or .json fixture / dag report")
    parser.add_argument("--to", choices=("dag", "excel"), required=True, dest="direction")
    parser.add_argument("-o", "--report", type=Path, help="Write JSON conversion report")
    parser.add_argument(
        "--write-xlsx",
        type=Path,
        help="(dag only) Write workbook copy with DAG =PY formulas applied",
    )
    parser.add_argument(
        "--best-effort",
        action="store_true",
        help="Emit partial conversions even when some cells fail (default: fail-closed)",
    )
    args = parser.parse_args(argv)

    from plugin.calc.excel_py_convert.convert import convert_path, write_dag_formulas_xlsx

    report = convert_path(
        args.path,
        direction=args.direction,
        out_report=args.report,
        best_effort=args.best_effort,
    )
    if args.write_xlsx:
        if args.direction != "dag":
            print("--write-xlsx only valid with --to dag", file=sys.stderr)
            return 2
        if args.path.suffix.lower() != ".xlsx":
            print("--write-xlsx requires an .xlsx source", file=sys.stderr)
            return 2
        try:
            write_dag_formulas_xlsx(args.path, report, args.write_xlsx)
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            return 1
        print(f"wrote {args.write_xlsx}")

    if args.report:
        print(f"wrote {args.report}")
    else:
        print(json.dumps(report.to_dict(), indent=2))

    if not report.ok and not args.best_effort:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
