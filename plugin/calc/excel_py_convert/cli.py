# SPDX-License-Identifier: GPL-3.0-or-later
"""CLI: convert Excel Python-in-Excel scripts ↔ DAG-style ``=PY(code; ranges)``.

``--to excel --write-xlsx`` writes native ``pythonScripts.xml`` / ``_xlws.PY``.
Pass ``--from-report dag.json`` with ``--to excel`` to preserve return_type /
data_args from a DAG conversion report.
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
    parser.add_argument("--from-report", type=Path, help="DAG conversion JSON for --to excel (preserves return_type / data_args)")
    parser.add_argument("--write-xlsx", type=Path, help="Write workbook: DAG =PY (--to dag) or native Excel PY package (--to excel)")
    parser.add_argument("--best-effort", action="store_true", help="Emit partial conversions even when some cells fail (default: fail-closed)")
    args = parser.parse_args(argv)

    from plugin.calc.excel_py_convert.convert import convert_path, write_dag_formulas_xlsx, write_excel_python_xlsx

    if args.from_report and args.direction != "excel":
        print("--from-report only valid with --to excel", file=sys.stderr)
        return 2

    report = convert_path(args.path, direction=args.direction, out_report=args.report, best_effort=args.best_effort, from_report=args.from_report)
    if args.write_xlsx:
        if args.path.suffix.lower() != ".xlsx":
            print("--write-xlsx requires an .xlsx source (package shell)", file=sys.stderr)
            return 2
        try:
            if args.direction == "dag":
                write_dag_formulas_xlsx(args.path, report, args.write_xlsx)
            else:
                write_excel_python_xlsx(args.path, report, args.write_xlsx)
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
