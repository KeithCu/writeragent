#!/usr/bin/env python3
"""Probe 2: the four questions probe 1 could not answer.

Q1 Locale — parse ISO/near-miss strings under several locales WITHOUT touching
   global settings, using XNumberFormatter.detectNumberFormat /
   convertStringToNumber, which parse in the locale of the key passed in.
Q2 Pristine cells — does auto-format apply when NumberFormat was never set?
Q3 Round trip — what does WriterAgent's own CellInspector return for cells
   written each candidate way?
Q4 DURATION — do elapsed-time formats report TIME (4) or DURATION (8196)?

Usage (from repo root):
    PYTHONPATH=. python3 scripts/playground/probe_calc_datetime_locale.py
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
from glob import glob
from pathlib import Path

os.environ.setdefault("WRITERAGENT_TESTING", "1")

# com.sun.star.util.NumberFormat
NF_DEFINED = 1
NF_DATE = 2
NF_TIME = 4
NF_CURRENCY = 8
NF_NUMBER = 16
NF_TEXT = 256
NF_DURATION = 8196  # 0x2004 == TIME | 0x2000

LOCALES = [("en", "US"), ("de", "DE"), ("fr", "FR"), ("sv", "SE"), ("hu", "HU")]

STRINGS = [
    "2026-08-08",
    "2026-8-8",
    "08:00",
    "08:00:00",
    "2026-08-08T08:00:00",
    "2026-08-08 08:00:00",
    "08/05/2026",
    "05.08.2026",
    "08:00 AM",
    "2026-08-08T08:00:00Z",
    "24:00",
    "30:00",
]


def bootstrap():
    import officehelper

    program_dir = Path(officehelper.__file__).resolve().parent
    soffice = program_dir / ("soffice.exe" if sys.platform.startswith("win") else "soffice")
    if not soffice.exists():
        soffice = Path(os.environ.get("UNO_PATH", "/usr/lib/libreoffice/program")) / "soffice"
    profile = tempfile.mkdtemp(prefix="wa-dt2-profile-")
    cmd = (
        f'"{soffice}" --headless --norestore --nofirststartwizard --nocrashreport '
        f"-env:UserInstallation={Path(profile).as_uri()}"
    )
    ctx = officehelper.bootstrap(soffice=cmd)
    if ctx is None:
        raise RuntimeError("bootstrap returned None")
    return ctx, profile


def as_int(v) -> int:
    inner = getattr(v, "value", v)
    try:
        return int(inner)
    except (TypeError, ValueError):
        return -1


def enum_name(v) -> str:
    s = str(v)
    return s.split("'")[1] if "'" in s else s


def category(fmt_type) -> str:
    """Mirror plugin/calc/inspector.py::_format_category_from_type, plus extras it lacks."""
    base = as_int(fmt_type) & ~NF_DEFINED
    if base == NF_DURATION & ~NF_DEFINED or base == NF_DURATION:
        return "DURATION"
    if base == (NF_DATE | NF_TIME):
        return "datetime"
    if base == NF_DATE:
        return "date"
    if base == NF_TIME:
        return "time"
    if base == NF_TEXT:
        return "text"
    if base == NF_NUMBER:
        return "number/General"
    return f"other({base})"


def plugin_category(fmt_type):
    """The real production classifier, so we test what actually ships."""
    from plugin.calc.inspector import _format_category_from_type

    return _format_category_from_type(as_int(fmt_type))


def make_locale(lang, country):
    import uno

    loc = uno.createUnoStruct("com.sun.star.lang.Locale")
    loc.Language = lang
    loc.Country = country
    return loc


def lo_version(ctx) -> str:
    import uno

    try:
        smgr = ctx.ServiceManager
        cp = smgr.createInstanceWithContext("com.sun.star.configuration.ConfigurationProvider", ctx)
        arg = uno.createUnoStruct("com.sun.star.beans.PropertyValue")
        arg.Name = "nodepath"
        arg.Value = "/org.openoffice.Setup/Product"
        node = cp.createInstanceWithArguments("com.sun.star.configuration.ConfigurationAccess", (arg,))
        return f"{node.getByName('ooName')} {node.getByName('ooSetupVersionAboutBox')}"
    except Exception as e:
        return f"unknown ({e})"


def hr(title):
    print(f"\n{'=' * 78}\n{title}\n{'=' * 78}", flush=True)


def q1_locale(ctx, doc):
    """Locale-explicit parsing without changing any global setting."""
    hr("Q1  Locale-explicit parsing via XNumberFormatter (no global config change)")

    smgr = ctx.ServiceManager
    formatter = smgr.createInstanceWithContext("com.sun.star.util.NumberFormatter", ctx)
    formatter.attachNumberFormatsSupplier(doc)
    formats = doc.getNumberFormats()

    header = f"{'input':<26}" + "".join(f"{lang}-{ctry:<12}" for lang, ctry in LOCALES)
    print(header, flush=True)
    print("-" * len(header), flush=True)

    detail = {}
    for s in STRINGS:
        cells = []
        for lang, ctry in LOCALES:
            loc = make_locale(lang, ctry)
            try:
                std_key = formats.getStandardIndex(loc)
            except Exception as e:
                cells.append(f"ERR:{e}"[:14])
                continue
            try:
                det_key = formatter.detectNumberFormat(std_key, s)
                value = formatter.convertStringToNumber(std_key, s)
                props = formats.getByKey(det_key)
                cat = category(props.getPropertyValue("Type"))
                cells.append(f"{cat}:{value:g}"[:14])
                detail[(s, lang)] = {
                    "value": value,
                    "category": cat,
                    "format_string": props.getPropertyValue("FormatString"),
                    "plugin_category": plugin_category(props.getPropertyValue("Type")),
                }
            except Exception as e:
                name = type(e).__name__
                cells.append("TEXT" if "NotNumeric" in name else f"ERR:{name}"[:14])
        print(f"{s:<26}" + "".join(f"{c:<15}" for c in cells), flush=True)

    hr("Q1b  Detected format string + production classifier (ISO date & time only)")
    for s in ("2026-08-08", "08:00", "2026-08-08T08:00:00", "2026-08-08 08:00:00"):
        for lang, _ in LOCALES:
            d = detail.get((s, lang))
            if d:
                print(
                    f"  {s:<24} {lang}: value={d['value']:<18g} detected='{d['format_string']}'"
                    f"  category={d['category']}  plugin={d['plugin_category']}",
                    flush=True,
                )
        print(flush=True)


def q2_pristine(doc, sheet):
    hr("Q2  Pristine cell (NumberFormat never set) vs explicitly reset to 0")
    formats = doc.getNumberFormats()

    def show(label, cell, text):
        cell.setFormula(text)
        key = int(cell.getPropertyValue("NumberFormat"))
        props = formats.getByKey(key)
        print(
            f"  {label:<34} input={text:<22} type={enum_name(cell.getType()):<8} "
            f"value={cell.getValue():<16g} key={key:<6} fmt='{props.getPropertyValue('FormatString')}' "
            f"cat={category(props.getPropertyValue('Type'))}  display='{cell.getString()}'",
            flush=True,
        )

    # Column H is untouched by every other probe section.
    show("pristine (never formatted)", sheet.getCellByPosition(7, 0), "2026-08-08")
    show("pristine time", sheet.getCellByPosition(7, 1), "08:00")
    show("pristine datetime", sheet.getCellByPosition(7, 2), "2026-08-08T08:00:00")

    c = sheet.getCellByPosition(7, 4)
    c.setPropertyValue("NumberFormat", 0)
    show("explicitly set NumberFormat=0", c, "2026-08-08")


def q3_roundtrip(doc, sheet):
    hr("Q3  Round trip through WriterAgent CellInspector.read_range(include_format_info=True)")
    try:
        from plugin.calc.bridge import CalcBridge
        from plugin.calc.inspector import CellInspector
    except Exception as e:
        print(f"  SKIP: cannot import plugin ({type(e).__name__}: {e})", flush=True)
        print("  Run with PYTHONPATH=. from the repo root.", flush=True)
        return

    formats = doc.getNumberFormats()
    locale = make_locale("en", "US")

    # Candidate B: setFormula only (no format pass)
    b = sheet.getCellByPosition(9, 0)  # J1
    b.setPropertyValue("NumberFormat", 0)
    b.setFormula("2026-08-08")

    # Candidate C: convertStringToNumber + detected key
    import uno  # noqa: F401

    smgr = doc.getCurrentController().getFrame().getController() if False else None  # placeholder
    c = sheet.getCellByPosition(9, 1)  # J2
    c.setPropertyValue("NumberFormat", 0)

    # Candidate A: hand-computed serial + DATE_DIN_YYYYMMDD
    a = sheet.getCellByPosition(9, 2)  # J3
    a.setValue(46242.0)
    a.setPropertyValue("NumberFormat", formats.getFormatIndex(33, locale))

    inspector = CellInspector(CalcBridge(doc))
    rows = inspector.read_range("J1:J3", include_format_info=True)
    labels = ["B: setFormula only", "C: (filled below)", "A: setValue + DATE_DIN"]
    for label, cell_info in zip(labels, rows[0]):
        print(f"  {label:<26} -> {cell_info}", flush=True)

    print(
        "\n  Interpretation: a good round trip has ISO in value plus type/format_category\n"
        "  of date/time/datetime (or duration / PT… for elapsed). A bare numeric value\n"
        "  with type 'value' means no temporal format was applied (e.g. setFormula-only).",
        flush=True,
    )


def q3b_candidate_c(ctx, doc, sheet):
    hr("Q3b  Candidate C end to end: detectNumberFormat + convertStringToNumber + setValue")
    try:
        from plugin.calc.bridge import CalcBridge
        from plugin.calc.inspector import CellInspector
    except Exception as e:
        print(f"  SKIP: {e}", flush=True)
        return

    smgr = ctx.ServiceManager
    formatter = smgr.createInstanceWithContext("com.sun.star.util.NumberFormatter", ctx)
    formatter.attachNumberFormatsSupplier(doc)
    formats = doc.getNumberFormats()
    std_key = formats.getStandardIndex(make_locale("en", "US"))

    cases = ["2026-08-08", "08:00", "2026-08-08T08:00:00"]
    for i, s in enumerate(cases):
        cell = sheet.getCellByPosition(11, i)  # L1:L3
        try:
            det = formatter.detectNumberFormat(std_key, s)
            val = formatter.convertStringToNumber(std_key, s)
            cell.setValue(val)
            cell.setPropertyValue("NumberFormat", det)
        except Exception as e:
            print(f"  {s}: {type(e).__name__}: {e}", flush=True)

    inspector = CellInspector(CalcBridge(doc))
    rows = inspector.read_range("L1:L3", include_format_info=True)
    for s, info in zip(cases, rows[0]):
        cell = sheet.getCellByPosition(11, cases.index(s))
        print(f"  {s:<24} display='{cell.getString():<22}' read_range -> {info}", flush=True)


def q4_duration(doc, sheet):
    hr("Q4  Elapsed-time formats: TIME (4) or DURATION (8196)?")
    formats = doc.getNumberFormats()
    locale = make_locale("en", "US")

    codes = ["[HH]:MM:SS", "[H]:MM", "[MM]:SS", "HH:MM:SS"]
    for code in codes:
        key = formats.queryKey(code, locale, False)
        created = False
        if key == -1:
            try:
                key = formats.addNew(code, locale)
                created = True
            except Exception as e:
                print(f"  {code:<14} addNew failed: {e}", flush=True)
                continue
        props = formats.getByKey(key)
        t = props.getPropertyValue("Type")
        print(
            f"  {code:<14} key={key:<8} Type={as_int(t):<8} probe_cat={category(t):<16} "
            f"plugin_cat={plugin_category(t)!r:<10} {'(created)' if created else '(existing)'}",
            flush=True,
        )

    # Built-in duration index: NumberFormatIndex.TIME_HH_MMSS00 == 43, duration-ish entries 41-44
    for idx in (41, 42, 43, 44):
        try:
            key = formats.getFormatIndex(idx, locale)
            props = formats.getByKey(key)
            t = props.getPropertyValue("Type")
            print(
                f"  formatindex {idx:<3} key={key:<8} Type={as_int(t):<8} "
                f"fmt='{props.getPropertyValue('FormatString')}' probe_cat={category(t)} "
                f"plugin_cat={plugin_category(t)!r}",
                flush=True,
            )
        except Exception as e:
            print(f"  formatindex {idx}: {type(e).__name__}", flush=True)

    # Does a >24h value in an elapsed format read back wrong?
    hr("Q4b  30-hour value in [HH]:MM:SS through the production read path")
    try:
        from plugin.calc.bridge import CalcBridge
        from plugin.calc.inspector import CellInspector

        cell = sheet.getCellByPosition(13, 0)  # N1
        cell.setValue(1.25)  # 30 hours
        key = formats.queryKey("[HH]:MM:SS", locale, False)
        if key == -1:
            key = formats.addNew("[HH]:MM:SS", locale)
        cell.setPropertyValue("NumberFormat", key)
        info = CellInspector(CalcBridge(doc)).read_range("N1", include_format_info=True)[0][0]
        print(f"  cell displays '{cell.getString()}'  read_range -> {info}", flush=True)
        if info.get("value") == "06:00:00" or info.get("type") == "time":
            print("  ^^ WRONG: 30 hours reported as clock time (whole day silently dropped)", flush=True)
        elif info.get("value") == "PT30H" and info.get("type") == "duration":
            print("  OK: duration wire PT30H", flush=True)
        else:
            print(f"  unexpected readback: value={info.get('value')!r} type={info.get('type')!r}", flush=True)
    except Exception as e:
        print(f"  SKIP: {type(e).__name__}: {e}", flush=True)


def main() -> int:
    ctx, profile = bootstrap()
    print(f"LibreOffice: {lo_version(ctx)}", flush=True)
    smgr = ctx.ServiceManager
    desktop = smgr.createInstanceWithContext("com.sun.star.frame.Desktop", ctx)
    doc = desktop.loadComponentFromURL("private:factory/scalc", "_blank", 0, ())
    sheet = doc.getSheets().getByIndex(0)

    try:
        q1_locale(ctx, doc)
        q2_pristine(doc, sheet)
        q3_roundtrip(doc, sheet)
        q3b_candidate_c(ctx, doc, sheet)
        q4_duration(doc, sheet)
    finally:
        try:
            desktop.terminate()
        except Exception:
            pass
        shutil.rmtree(profile, ignore_errors=True)
        for stale in glob("/tmp/wa-dt-probe-profile-*") + glob("/tmp/wa-dt2-profile-*"):
            shutil.rmtree(stale, ignore_errors=True)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except Exception:
        import traceback

        traceback.print_exc()
        raise SystemExit(1)
