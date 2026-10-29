#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Round-trip PythonExcelSamples: Excel → DAG → Excel and compare script fidelity.

Usage::

    .venv/bin/python scripts/roundtrip_excel_py_samples.py \\
        --samples PythonExcelSamples \\
        --out build/excel_py_roundtrip

Exit 1 if normalized script text or return_type differs, or if ordering-only
deps leak into the round-trip ``_xlws.PY`` formula. Table/ANCHORARRAY → A1
snapshot dep diffs are warnings only.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

# Repo root on sys.path when run as scripts/...
_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


def _norm_code(s: str) -> str:
    s = (s or "").replace("\r\n", "\n").strip()
    s = re.sub(r"\n?# excel_py: returnType=1.*?\nresult = None\s*$", "", s, flags=re.S)
    # Cosmetic: MS samples usually omit the space after the comma in headers=.
    s = re.sub(r",\s*headers=", ",headers=", s)
    return s


def _is_excel_structured_token(dep: str) -> bool:
    d = (dep or "").strip()
    return "[#" in d or "ANCHORARRAY" in d.upper()


def _deps_acceptable(orig_deps: list[str], back_deps: list[str], ordering: list[str]) -> tuple[bool, str]:
    """Return (ok, message).

    Table/ANCHORARRAY tokens must round-trip exactly (export fidelity via excel_deps).
    Sheet punctuation (. vs !) may warn. Ordering leaks fail.
    """
    ord_set = {a.replace("$", "") for a in ordering}
    leaked = [d for d in back_deps if d.replace("$", "") in ord_set]
    if leaked:
        return False, f"ordering deps leaked into export: {leaked}"
    if orig_deps == back_deps:
        return True, ""
    if len(orig_deps) == len(back_deps):
        warns: list[str] = []
        for o, b in zip(orig_deps, back_deps, strict=True):
            if o == b:
                continue
            if _is_excel_structured_token(o) and o != b:
                return False, f"structured dep lost: {o!r} → {b!r}"
            if o.replace(".", "!") == b.replace(".", "!"):
                warns.append(f"{o!r}→{b!r}")
                continue
            return True, f"warn dep morph {o!r} → {b!r}"
        if warns:
            return True, "warn: deps remapped (sheet sep)"
        return True, ""
    if len(back_deps) > len(orig_deps):
        extra = back_deps[len(orig_deps) :]
        return False, f"extra deps on round-trip: {extra}"
    return True, f"warn: dep count {len(orig_deps)} → {len(back_deps)}"


def roundtrip_one(src: Path, out_dir: Path) -> tuple[bool, list[str]]:
    from plugin.calc.excel_py_convert.convert import convert_to_dag
    from plugin.calc.excel_py_convert.xlsx_write import (
        write_dag_formulas_xlsx,
        write_excel_python_xlsx,
    )
    from plugin.calc.excel_py_convert.auto_open import has_excel_python_xlsx
    from plugin.calc.excel_py_convert.parse_excel_ooxml import load_excel_model
    from plugin.calc.excel_py_convert.to_dag import convert_model_to_dag
    from plugin.calc.excel_py_convert.to_excel import convert_dag_report_to_excel

    lines: list[str] = []
    ok = True
    stem = src.stem
    dag_path = out_dir / f"{stem}.dag.xlsx"
    excel_path = out_dir / f"{stem}.excel.xlsx"
    report_path = out_dir / f"{stem}.dag.json"

    orig = load_excel_model(src)
    dag_report = convert_to_dag(src)
    report_path.write_text(
        __import__("json").dumps(dag_report.to_dict(), indent=2) + "\n",
        encoding="utf-8",
    )
    if not dag_report.ok:
        return False, [f"DAG convert failed: {dag_report.issues}"]

    write_dag_formulas_xlsx(src, dag_report, dag_path)
    excel_report = convert_dag_report_to_excel(dag_report)
    write_excel_python_xlsx(dag_path, excel_report, excel_path)
    if not has_excel_python_xlsx(excel_path):
        return False, ["round-trip xlsx missing pythonScripts / _xlws.PY"]

    back = load_excel_model(excel_path)
    again = convert_model_to_dag(back)
    if not again.ok:
        ok = False
        lines.append(f"reimport DAG not ok: {again.issues}")

    orig_by = {(c.sheet, c.cell): c for c in orig.cells}
    back_by = {(c.sheet, c.cell): c for c in back.cells}
    excel_by = {(c.sheet, c.cell): c for c in excel_report.cells}

    missing = set(orig_by) - set(back_by)
    if missing:
        ok = False
        lines.append(f"missing cells: {sorted(missing)[:8]}")

    code_fail = 0
    rt_fail = 0
    dep_fail = 0
    dep_warn = 0
    for key, oc in orig_by.items():
        bc = back_by.get(key)
        if not bc:
            continue
        ocode = _norm_code(orig.scripts[oc.script_index] if 0 <= oc.script_index < len(orig.scripts) else "")
        bcode = _norm_code(back.scripts[bc.script_index] if 0 <= bc.script_index < len(back.scripts) else "")
        if ocode != bcode:
            code_fail += 1
            ok = False
            if code_fail <= 3:
                lines.append(f"CODE {key[0]}!{key[1]}: {ocode[:100]!r} → {bcode[:100]!r}")
        if oc.return_type != bc.return_type:
            rt_fail += 1
            ok = False
            if rt_fail <= 3:
                lines.append(f"returnType {key[0]}!{key[1]}: {oc.return_type} → {bc.return_type}")
        ec = excel_by.get(key)
        ordering = list(ec.ordering_args) if ec else []
        # Prefer comparing against original deps with snapshot tolerance using back deps.
        dep_ok, dep_msg = _deps_acceptable(oc.deps, bc.deps, ordering)
        if not dep_ok:
            dep_fail += 1
            ok = False
            if dep_fail <= 3:
                lines.append(f"DEPS {key[0]}!{key[1]}: {dep_msg}")
        elif dep_msg.startswith("warn"):
            dep_warn += 1
            if dep_warn <= 2:
                lines.append(f"DEPS {key[0]}!{key[1]}: {dep_msg}")

    lines.insert(
        0,
        f"cells={len(orig.cells)} code_fail={code_fail} rt_fail={rt_fail} "
        f"dep_fail={dep_fail} dep_warn={dep_warn} reimport_ok={again.ok}",
    )
    return ok, lines


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--samples",
        type=Path,
        default=_ROOT / "PythonExcelSamples",
        help="Directory of Excel Python-in-Excel .xlsx demos",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=_ROOT / "build" / "excel_py_roundtrip",
        help="Output directory for dag/excel artifacts",
    )
    parser.add_argument(
        "--soffice",
        action="store_true",
        help="Optional: also smoke-open with soffice --headless (skip if missing)",
    )
    args = parser.parse_args(argv)

    samples = sorted(args.samples.glob("*.xlsx"))
    if not samples:
        print(f"no .xlsx under {args.samples}", file=sys.stderr)
        return 2
    args.out.mkdir(parents=True, exist_ok=True)

    all_ok = True
    for src in samples:
        print(f"\n=== {src.name} ===")
        try:
            ok, lines = roundtrip_one(src, args.out)
        except Exception as exc:
            all_ok = False
            print(f"ERROR: {type(exc).__name__}: {exc}")
            continue
        for line in lines:
            print(f"  {line}")
        print(f"  {'PASS' if ok else 'FAIL'}")
        all_ok = all_ok and ok

    if args.soffice:
        import shutil
        import subprocess

        soffice = shutil.which("soffice")
        if not soffice:
            print("\n--soffice: soffice not on PATH (skipped)")
        else:
            smoke = args.out / "soffice_smoke"
            smoke.mkdir(exist_ok=True)
            for src in samples[:2]:  # light smoke on first two
                print(f"\n=== soffice smoke {src.name} ===")
                cmd = [
                    soffice,
                    "--headless",
                    "--convert-to",
                    "xlsx",
                    "--outdir",
                    str(smoke),
                    str(src),
                ]
                proc = subprocess.run(cmd, capture_output=True, text=True, timeout=180, check=False)
                print(f"  returncode={proc.returncode}")
                if proc.returncode != 0:
                    print(proc.stderr[:500])
                    all_ok = False

    print("\n" + ("ALL PASS" if all_ok else "FAILURES — see above"))
    return 0 if all_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
