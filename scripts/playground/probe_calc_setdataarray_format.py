#!/usr/bin/env python3
"""Probe M3: does setDataArray preserve NumberFormat for floats?

String setDataArray of a number-like ISO date is already known to force `@`
(see probe_calc_setformula_datetime.py / calc-date-time-handling.md §8.3).
This probe measures the float path on a pre-formatted date cell, plus controls.

Usage:
    python3 scripts/playground/probe_calc_setdataarray_format.py
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("WRITERAGENT_TESTING", "1")

_DEFINED = 1
_DATE = 2
_TIME = 4
_TEXT = 256


def _bootstrap():
    import officehelper

    program_dir = Path(officehelper.__file__).resolve().parent
    soffice = program_dir / ("soffice.exe" if sys.platform.startswith("win") else "soffice")
    if not soffice.exists():
        soffice = Path(os.environ.get("UNO_PATH", "/usr/lib/libreoffice/program")) / "soffice"
    profile_url = Path(tempfile.mkdtemp(prefix="wa-sda-fmt-probe-")).as_uri()
    cmd = (
        f'"{soffice}" --headless --norestore --nofirststartwizard --nocrashreport '
        f"-env:UserInstallation={profile_url}"
    )
    ctx = officehelper.bootstrap(soffice=cmd)
    if ctx is None:
        raise RuntimeError("officehelper.bootstrap returned None")
    return ctx


def _enum_name(v) -> str:
    name = getattr(v, "value", None)
    if isinstance(name, str):
        return name
    s = str(v)
    if "'" in s:
        return s.split("'")[1]
    return s


def _fmt_int(v) -> int:
    if hasattr(v, "value") and not isinstance(v.value, str):
        try:
            return int(v.value)
        except (TypeError, ValueError):
            pass
    try:
        return int(v)
    except (TypeError, ValueError):
        return -1


def _category(fmt_type) -> str:
    base = _fmt_int(fmt_type) & ~_DEFINED
    if base < 0:
        return f"unknown({fmt_type!r})"
    if base == (_DATE | _TIME):
        return "datetime"
    if base == _DATE:
        return "date"
    if base == _TIME:
        return "time"
    if base & _TEXT:
        return "text"
    if base == 0:
        return "general"
    return f"other({base})"


def _make_locale(lang: str = "en", country: str = "US"):
    import uno

    loc = uno.createUnoStruct("com.sun.star.lang.Locale")
    loc.Language = lang
    loc.Country = country
    return loc


def _inspect(doc, cell) -> dict:
    formats = doc.getNumberFormats()
    fmt_key = int(cell.getPropertyValue("NumberFormat"))
    props = formats.getByKey(fmt_key)
    fmt_type = props.getPropertyValue("Type")
    return {
        "type": _enum_name(cell.getType()),
        "value": cell.getValue(),
        "string": cell.getString(),
        "formula": cell.getFormula(),
        "format_key": fmt_key,
        "format_category": _category(fmt_type),
        "format_string": str(props.getPropertyValue("FormatString")),
        "format_type": _fmt_int(fmt_type),
    }


def _force_format(doc, cell, locale, code: str) -> int:
    formats = doc.getNumberFormats()
    key = formats.queryKey(code, locale, False)
    if key == -1:
        key = formats.addNew(code, locale)
    cell.setPropertyValue("NumberFormat", int(key))
    return int(key)


def _case(doc, sheet, row: int, label: str, prep_code: str | None, payload, locale) -> dict:
    """prep_code: format to apply before setDataArray, or None to leave General (key 0)."""
    cell = sheet.getCellByPosition(0, row)
    rng = sheet.getCellRangeByPosition(0, row, 0, row)
    cell.setFormula("")
    cell.setPropertyValue("NumberFormat", 0)
    before_key = 0
    if prep_code is not None:
        before_key = _force_format(doc, cell, locale, prep_code)
    before = _inspect(doc, cell)
    # One-cell setDataArray; payload is a Python float or str
    rng.setDataArray(((payload,),))
    after = _inspect(doc, cell)
    preserved = after["format_key"] == before_key
    return {
        "label": label,
        "prep_format": prep_code or "General(0)",
        "payload_repr": repr(payload),
        "payload_py_type": type(payload).__name__,
        "before_key": before_key,
        "after_key": after["format_key"],
        "key_preserved": preserved,
        "before": before,
        "after": after,
    }


def main() -> int:
    ctx = _bootstrap()
    desktop = ctx.ServiceManager.createInstanceWithContext("com.sun.star.frame.Desktop", ctx)
    doc = desktop.loadComponentFromURL("private:factory/scalc", "_blank", 0, ())
    sheet = doc.getSheets().getByIndex(0)
    locale = _make_locale("en", "US")

    # 46242.0 == 2026-08-08 under the usual NullDate 1899-12-30
    serial = 46242.0

    cases = [
        # M3 question: float into a date-formatted cell
        ("float_into_date_fmt", "YYYY-MM-DD", serial),
        # Same float into General
        ("float_into_general", None, serial),
        # Control: string ISO into date-formatted cell (expect @)
        ("string_iso_into_date_fmt", "YYYY-MM-DD", "2026-08-08"),
        # Control: string into General (expect @)
        ("string_iso_into_general", None, "2026-08-08"),
        # Extra: float into time format (clock serial)
        ("float_time_into_time_fmt", "HH:MM:SS", 1.0 / 3.0),
        # Extra: empty string into date format (source suggests format kept)
        ("empty_string_into_date_fmt", "YYYY-MM-DD", ""),
        # Extra: int serial (UNO accepts integer Anys via SetValue path)
        ("int_into_date_fmt", "YYYY-MM-DD", 46242),
    ]

    rows = []
    for i, (label, prep, payload) in enumerate(cases):
        rows.append(_case(doc, sheet, i, label, prep, payload, locale))

    print("=== M3 setDataArray NumberFormat probe ===", flush=True)
    print(json.dumps(rows, indent=2, default=str), flush=True)

    checks = [
        (
            "float into YYYY-MM-DD preserves format key",
            rows[0]["key_preserved"] and rows[0]["after"]["type"] == "VALUE",
        ),
        (
            "float into General stays VALUE (key may stay 0)",
            rows[1]["after"]["type"] == "VALUE" and rows[1]["key_preserved"],
        ),
        (
            "string ISO into YYYY-MM-DD forces text/@",
            (not rows[2]["key_preserved"]) and rows[2]["after"]["format_category"] == "text",
        ),
        (
            "string ISO into General forces text/@",
            rows[3]["after"]["format_category"] == "text",
        ),
        (
            "float time into HH:MM:SS preserves format key",
            rows[4]["key_preserved"] and rows[4]["after"]["type"] == "VALUE",
        ),
        (
            "empty string into YYYY-MM-DD preserves format key",
            rows[5]["key_preserved"],
        ),
        (
            "int into YYYY-MM-DD preserves format key",
            rows[6]["key_preserved"] and rows[6]["after"]["type"] == "VALUE",
        ),
    ]
    print("\n=== CHECKS ===", flush=True)
    all_ok = True
    for name, ok in checks:
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}", flush=True)
        all_ok = all_ok and ok

    print(f"\nM3 float-preserve conclusion: {'YES' if checks[0][1] else 'NO'}", flush=True)

    try:
        desktop.terminate()
    except Exception as e:
        print(f"desktop.terminate: {e}", flush=True)
    return 0 if all_ok else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception:
        import traceback

        traceback.print_exc()
        raise SystemExit(2)
