#!/usr/bin/env python3
"""Probe: Calc setFormula / setFormulaArray date-time intake.

Playground only. Confirms whether the UNO input interpreter converts ISO strings
to serials AND whether it applies a date/time NumberFormat (Eike's interactive
input claim may not hold for setFormula).

Usage:
    python3 scripts/playground/probe_calc_setformula_datetime.py
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("WRITERAGENT_TESTING", "1")

# com.sun.star.util.NumberFormat (IDL)
_DEFINED = 1
_DATE = 2
_TIME = 4
_PERCENT = 16
_TEXT = 256
# DURATION was added later; LO uses 0x2000 in recent builds
_DURATION = 0x2000


def _bootstrap():
    import officehelper

    program_dir = Path(officehelper.__file__).resolve().parent
    soffice = program_dir / ("soffice.exe" if sys.platform.startswith("win") else "soffice")
    if not soffice.exists():
        soffice = Path(os.environ.get("UNO_PATH", "/usr/lib/libreoffice/program")) / "soffice"
    profile_url = Path(tempfile.mkdtemp(prefix="wa-dt-probe-profile-")).as_uri()
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
    # PyUNO Enum str looks like: <Enum instance com.sun.star.table.CellContentType ('VALUE')>
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
    if base & _DURATION and not (base & _DATE):
        return "duration"
    if base == (_DATE | _TIME):
        return "datetime"
    if base == _DATE:
        return "date"
    if base == _TIME:
        return "time"
    if base & _TEXT:
        return "text"
    if base == 0 or base == _PERCENT:  # General often 0; don't mislabel
        return "general"
    return f"other({base})"


def _inspect_cell(doc, cell) -> dict:
    ctype = _enum_name(cell.getType())
    formats = doc.getNumberFormats()
    fmt_key = int(cell.getPropertyValue("NumberFormat"))
    props = formats.getByKey(fmt_key)
    fmt_type_raw = props.getPropertyValue("Type")
    fmt_type = _fmt_int(fmt_type_raw)
    fmt_string = str(props.getPropertyValue("FormatString"))
    locale = props.getPropertyValue("Locale")
    locale_tag = f"{locale.Language}-{locale.Country}" if locale else "?"

    return {
        "type": ctype,
        "value": cell.getValue(),
        "string": cell.getString(),
        "formula": cell.getFormula(),
        "format_key": fmt_key,
        "format_type_raw": fmt_type,
        "format_category": _category(fmt_type_raw),
        "format_string": fmt_string,
        "format_locale": locale_tag,
    }


def _set_char_locale(doc, language: str, country: str):
    import uno

    loc = uno.createUnoStruct("com.sun.star.lang.Locale")
    loc.Language = language
    loc.Country = country
    doc.setPropertyValue("CharLocale", loc)
    return loc


def _force_format(doc, cell, locale, code: str) -> str:
    formats = doc.getNumberFormats()
    key = formats.queryKey(code, locale, False)
    if key == -1:
        key = formats.addNew(code, locale)
    cell.setPropertyValue("NumberFormat", key)
    return code


def _force_date_din(doc, cell, locale) -> str:
    formats = doc.getNumberFormats()
    key = formats.getFormatIndex(33, locale)  # DATE_DIN_YYYYMMDD
    cell.setPropertyValue("NumberFormat", key)
    return "DATE_DIN_YYYYMMDD"


def _print_table(title: str, rows: list[dict]):
    print(f"\n=== {title} ===", flush=True)
    cols = ("label", "input", "type", "value", "string", "format_category", "format_string", "format_key")
    widths = {c: len(c) for c in cols}
    for r in rows:
        for c in cols:
            widths[c] = max(widths[c], min(40, len(str(r.get(c, "")))))
    print(" | ".join(c.ljust(widths[c]) for c in cols), flush=True)
    print("-+-".join("-" * widths[c] for c in cols), flush=True)
    for r in rows:
        print(" | ".join(str(r.get(c, ""))[:40].ljust(widths[c]) for c in cols), flush=True)


CASES = [
    ("date", "2026-08-08"),
    ("date_unpadded", "2026-8-8"),
    ("time_hm", "08:00"),
    ("time_hms", "08:00:00"),
    ("time_unpadded", "8:00"),
    ("dt_T", "2026-08-08T08:00:00"),
    ("dt_space", "2026-08-08 08:00:00"),
    ("dt_T_hm", "2026-08-08T08:00"),
    ("dt_space_hm", "2026-08-08 08:00"),
    ("slash_us", "08/05/2026"),
    ("slash_eu", "05.08.2026"),
    ("ampm", "08:00 AM"),
    ("frac_sec", "08:00:00.500"),
    ("zulu", "2026-08-08T08:00:00Z"),
    ("offset", "2026-08-08T08:00:00-04:00"),
    ("midnight_24", "24:00"),
    ("durationish", "30:00"),
    ("apostrophe", "'2026-08-08"),
    ("prose", "Hello World"),
    ("number", "42"),
    ("formula", "=1+1"),
]


def main() -> int:
    print("Bootstrapping headless LibreOffice…", flush=True)
    ctx = _bootstrap()
    smgr = ctx.ServiceManager
    desktop = smgr.createInstanceWithContext("com.sun.star.frame.Desktop", ctx)
    doc = desktop.loadComponentFromURL("private:factory/scalc", "_blank", 0, ())
    sheet = doc.getSheets().getByIndex(0)
    locale = doc.getPropertyValue("CharLocale")

    null_date = doc.getNumberFormatSettings().getPropertyValue("NullDate")
    print(
        f"NullDate={null_date.Year:04d}-{null_date.Month:02d}-{null_date.Day:02d}  "
        f"CharLocale={locale.Language}-{locale.Country}",
        flush=True,
    )

    rows = []
    for i, (label, text) in enumerate(CASES):
        cell = sheet.getCellByPosition(0, i)
        cell.setPropertyValue("NumberFormat", 0)
        cell.setFormula(text)
        info = _inspect_cell(doc, cell)
        info["label"] = label
        info["input"] = text
        rows.append(info)
    _print_table("setFormula (default locale)", rows)

    # Pre-formatted destinations
    pre = []
    for row, text, prep, label in (
        (40, "2026-08-08", lambda d, c, loc: _force_format(d, c, loc, "@"), "@"),
        (41, "08:00", lambda d, c, loc: _force_format(d, c, loc, "[HH]:MM:SS"), "[HH]:MM:SS"),
        (42, "08:00", _force_date_din, "DATE_DIN"),
        (43, "2026-08-08T12:00:00", _force_date_din, "DATE_DIN+dt"),
        (44, "2026-08-08", lambda d, c, loc: _force_format(d, c, loc, "YYYY-MM-DD"), "YYYY-MM-DD"),
    ):
        cell = sheet.getCellByPosition(0, row)
        cell.setFormula("")
        prep(doc, cell, locale)
        cell.setFormula(text)
        info = _inspect_cell(doc, cell)
        info["label"] = label
        info["input"] = text
        pre.append(info)
    _print_table("setFormula into pre-formatted cells", pre)

    # setDataArray vs setFormulaArray
    a = sheet.getCellByPosition(0, 50)
    a.setPropertyValue("NumberFormat", 0)
    sheet.getCellRangeByPosition(0, 50, 0, 50).setDataArray((("2026-08-08",),))
    b = sheet.getCellByPosition(1, 50)
    b.setPropertyValue("NumberFormat", 0)
    sheet.getCellRangeByPosition(1, 50, 1, 50).setFormulaArray((("2026-08-08",),))
    mixed = {"setDataArray": _inspect_cell(doc, a), "setFormulaArray": _inspect_cell(doc, b)}
    print("\n=== setDataArray vs setFormulaArray on '2026-08-08' ===", flush=True)
    print(json.dumps(mixed, indent=2, default=str), flush=True)

    # NullDate 1904
    import uno

    settings = doc.getNumberFormatSettings()
    old = settings.getPropertyValue("NullDate")
    nd = uno.createUnoStruct("com.sun.star.util.Date")
    nd.Year, nd.Month, nd.Day = 1904, 1, 1
    settings.setPropertyValue("NullDate", nd)
    cell = sheet.getCellByPosition(0, 80)
    cell.setPropertyValue("NumberFormat", 0)
    cell.setFormula("2026-08-08")
    info_1904 = _inspect_cell(doc, cell)
    info_1904["label"] = "nulldate_1904"
    info_1904["input"] = "2026-08-08"
    _print_table("setFormula with NullDate=1904-01-01", [info_1904])
    settings.setPropertyValue("NullDate", old)

    # CharLocale de-DE (acceptance patterns may still be global)
    _set_char_locale(doc, "de", "DE")
    de_rows = []
    for i, (label, text) in enumerate(
        [("date", "2026-08-08"), ("time_hm", "08:00"), ("eu_dot", "05.08.2026"), ("dt_T", "2026-08-08T08:00:00")]
    ):
        cell = sheet.getCellByPosition(0, 60 + i)
        cell.setPropertyValue("NumberFormat", 0)
        cell.setFormula(text)
        info = _inspect_cell(doc, cell)
        info["label"] = label
        info["input"] = text
        de_rows.append(info)
    _print_table("setFormula after CharLocale=de-DE", de_rows)

    # Summary focused on the router question
    print("\n=== ROUTER SPIKE SUMMARY ===", flush=True)
    by = {r["label"]: r for r in rows}

    def val(label):
        return by[label]["type"] == "VALUE"

    def leaves_general(label):
        # §8.1: setFormula converts to VALUE but leaves General (Eike interactive claim is false on UNO).
        return by[label]["format_category"] not in ("date", "time", "datetime")

    checks = [
        ("ISO date → VALUE serial", val("date") and by["date"]["value"] == 46242.0),
        ("ISO date leaves General (Eike UNO claim false)", val("date") and leaves_general("date")),
        ("HH:MM → VALUE ~0.333", val("time_hm") and abs(by["time_hm"]["value"] - 1 / 3) < 1e-9),
        ("HH:MM leaves General", val("time_hm") and leaves_general("time_hm")),
        ("T-datetime → VALUE", val("dt_T")),
        ("T-datetime leaves General", val("dt_T") and leaves_general("dt_T")),
        ("space-datetime → VALUE", val("dt_space")),
        ("unpadded date → VALUE", val("date_unpadded")),
        ("US slash → VALUE (locale-dependent; bad for router)", val("slash_us")),
        ("EU dot 05.08.2026 → TEXT under en-US", by["slash_eu"]["type"] == "TEXT"),
        ("Zulu → TEXT", by["zulu"]["type"] == "TEXT"),
        ("offset → TEXT", by["offset"]["type"] == "TEXT"),
        ("apostrophe → TEXT", by["apostrophe"]["type"] == "TEXT"),
        ("24:00 → VALUE 1.0", val("midnight_24") and by["midnight_24"]["value"] == 1.0),
        ("30:00 → VALUE 1.25 (duration-like)", val("durationish") and by["durationish"]["value"] == 1.25),
        ("setDataArray leaves TEXT", mixed["setDataArray"]["type"] == "TEXT"),
        ("setFormulaArray makes VALUE", mixed["setFormulaArray"]["type"] == "VALUE"),
        # §8.3: @ does not block setFormula conversion (claim that it blocks is false).
        ("@ preformat does not block setFormula conversion", pre[0]["type"] == "VALUE"),
        ("[HH]:MM:SS preserved for 08:00", pre[1]["format_string"].startswith("[")),
        ("NullDate 1904 changes serial", info_1904["value"] == 44780.0),
    ]
    for name, ok in checks:
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}", flush=True)

    print("\nKey rows (compact JSON):", flush=True)
    keep = ["date", "time_hm", "dt_T", "dt_space", "slash_us", "zulu", "offset", "apostrophe", "midnight_24"]
    print(json.dumps({k: by[k] for k in keep}, indent=2, default=str), flush=True)
    print("\nPreformatted:", flush=True)
    print(json.dumps(pre, indent=2, default=str), flush=True)

    # Hard exit — doc.close() can hang the bootstrap pipe; kill soffice instead.
    try:
        desktop.terminate()
    except Exception as e:
        print(f"desktop.terminate: {e}", flush=True)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as e:
        print(f"ERROR: {type(e).__name__}: {e}", file=sys.stderr, flush=True)
        import traceback

        traceback.print_exc()
        raise SystemExit(1)
