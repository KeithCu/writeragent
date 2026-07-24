# SPDX-License-Identifier: GPL-3.0-or-later
"""Optional ``py_code_<Sheet>`` bank sheets for long Excel→``=PY`` scripts.

Microsoft keeps Python in ``xl/pythonScripts.xml`` and cells only hold a short
``_xlws.PY(…)`` formula. Calc formula string symbols are capped near
``MAXSTRLEN`` (1024), so **rewritten code longer than**
``INLINE_CODE_MAX_CHARS`` (1000) is parked on a visible sheet **per source
worksheet** at the **same A1** as the caller (``Pivots!H4`` →
``py_code_Pivots!H4``) and the formula becomes ``=PY(py_code_Pivots.H4; …)``.

Shorter scripts stay **inline** as ``=PY("…"; ranges)``. One bank sheet per
source sheet avoids collisions when two worksheets both use the same A1 with
different long scripts. Raising Calc ``MAXSTRLEN`` later can retire the bank.
"""

from __future__ import annotations

import ast
import logging
import re
from typing import TYPE_CHECKING, Any

from plugin.calc.python.formula_edit import (
    rebuild_python_formula_with_code_ref,
    rebuild_python_formula_with_data,
)

if TYPE_CHECKING:
    from plugin.calc.excel_py_convert.models import ConversionReport, ConvertedCell

log = logging.getLogger(__name__)

CODE_SHEET_PREFIX = "py_code_"
# Calc / Excel sheet title practical max.
_CODE_SHEET_MAX_LEN = 31
CODE_SHEET_NOTE_CELL = "ZZ1"
CODE_SHEET_NOTE = (
    "Python script bank (from Excel pythonScripts). "
    "Used only for scripts longer than 1000 characters (Calc formula string limit). "
    "Each cell here holds the Python for the =PY formula at the same address "
    "on the matching data sheet (e.g. H4 here ↔ H4 on the source sheet). "
    "Enable shared-kernel session mode for multi-cell Excel scripts."
)

INLINE_CODE_MAX_CHARS = 1000

_XL_CALL_RE = re.compile(r"\bxl\s*\(", re.IGNORECASE)
_RE_A1 = re.compile(r"^\$?([A-Za-z]+)\$?(\d+)$")
_SAFE_SHEET = re.compile(r"[^\w]+", re.UNICODE)


def code_sheet_name_for(source_sheet: str) -> str:
    """Bank sheet name for *source_sheet* (``Pivots`` → ``py_code_Pivots``)."""
    raw = (source_sheet or "Sheet1").strip() or "Sheet1"
    safe = _SAFE_SHEET.sub("_", raw).strip("_") or "Sheet"
    name = f"{CODE_SHEET_PREFIX}{safe}"
    if len(name) > _CODE_SHEET_MAX_LEN:
        name = name[:_CODE_SHEET_MAX_LEN]
    return name


def normalize_bank_a1(cell: str) -> str:
    """Strip ``$`` / sheet prefix; return uppercase A1 (``H4``)."""
    raw = (cell or "").replace("$", "").strip()
    if "!" in raw:
        raw = raw.split("!", 1)[1]
    if "." in raw and not _RE_A1.match(raw):
        left, _, right = raw.partition(".")
        if right and _RE_A1.match(right) and not _RE_A1.match(left):
            raw = right
    m = _RE_A1.match(raw)
    if not m:
        raise ValueError(f"invalid bank A1 address: {cell!r}")
    return f"{m.group(1).upper()}{m.group(2)}"


def _col_letters_to_index(col: str) -> int:
    n = 0
    for ch in col.upper():
        n = n * 26 + (ord(ch) - 64)
    return n


def _index_to_col_letters(n: int) -> str:
    letters = []
    while n > 0:
        n, rem = divmod(n - 1, 26)
        letters.append(chr(65 + rem))
    return "".join(reversed(letters))


def iter_a1_span(ref: str) -> list[str]:
    """Expand ``A1:B2`` (no sheet) into cell coordinates; single cell → [cell].

    Used by openpyxl / UNO spill clear before rewriting a converted PY anchor.
    """
    # Spill refs are sheet-local A1; strip $ and optional Sheet! prefix only.
    cell_re = re.compile(r"^([A-Za-z]+)(\d+)$")
    raw = (ref or "").replace("$", "")
    if "!" in raw:
        raw = raw.split("!", 1)[1]
    if ":" not in raw:
        return [raw] if cell_re.match(raw) else []
    left, right = raw.split(":", 1)
    m1, m2 = cell_re.match(left), cell_re.match(right)
    if not m1 or not m2:
        return []
    c1, r1 = _col_letters_to_index(m1.group(1)), int(m1.group(2))
    c2, r2 = _col_letters_to_index(m2.group(1)), int(m2.group(2))
    if c1 > c2:
        c1, c2 = c2, c1
    if r1 > r2:
        r1, r2 = r2, r1
    out: list[str] = []
    for r in range(r1, r2 + 1):
        for c in range(c1, c2 + 1):
            out.append(f"{_index_to_col_letters(c)}{r}")
    return out


def code_bank_ref(source_sheet: str, cell_a1: str, *, excel_bang: bool = False) -> str:
    """Sheet-qualified ref: ``py_code_Pivots.H4`` or ``py_code_Pivots!H4``."""
    a1 = normalize_bank_a1(cell_a1)
    sheet = code_sheet_name_for(source_sheet)
    sep = "!" if excel_bang else "."
    return f"{sheet}{sep}{a1}"


def should_inline_code(code: str) -> bool:
    """True when *code* fits in ``=PY(\"…\")`` under Calc ``MAXSTRLEN`` (budget 1000)."""
    return len(code or "") <= INLINE_CODE_MAX_CHARS


def formula_for_converted_cell(
    cell: ConvertedCell,
    *,
    separator: str = ";",
    excel_escape: bool = False,
    use_script_bank: bool = True,
) -> str:
    """Build ``=PY`` for a converted cell.

    Inline when ``len(converted_code) <= 1000``; otherwise reference
    ``py_code_<Sheet>.A1`` (same address as the caller).
    """
    args = list(cell.data_args) + list(cell.ordering_args)
    if use_script_bank and cell.cell and cell.sheet and not should_inline_code(cell.converted_code):
        try:
            return rebuild_python_formula_with_code_ref(
                code_bank_ref(cell.sheet, cell.cell, excel_bang=(separator == "," or excel_escape)),
                args,
                separator=separator,
                excel_ranges=excel_escape or separator == ",",
            )
        except ValueError:
            pass
    return rebuild_python_formula_with_data(
        cell.converted_code,
        args,
        separator=separator,
        excel_escape=excel_escape,
    )


def collect_script_bank(report: ConversionReport) -> tuple[dict[str, dict[str, str]], list[str]]:
    """Map ``code_sheet_name → {A1 → code}`` for cells that need banking (>1000 chars)."""
    banks: dict[str, dict[str, str]] = {}
    owners: dict[tuple[str, str], str] = {}
    warnings: list[str] = []
    for cell in report.cells:
        if not cell.converted or not cell.converted_code or not cell.cell or not cell.sheet:
            continue
        if should_inline_code(cell.converted_code):
            continue
        try:
            a1 = normalize_bank_a1(cell.cell)
        except ValueError as exc:
            warnings.append(f"{cell.sheet}!{cell.cell}: {exc}")
            continue
        code_sheet = code_sheet_name_for(cell.sheet)
        bank = banks.setdefault(code_sheet, {})
        key = (code_sheet, a1)
        prev = bank.get(a1)
        if prev is None:
            bank[a1] = cell.converted_code
            owners[key] = f"{cell.sheet}!{cell.cell}"
            continue
        if prev == cell.converted_code:
            continue
        warnings.append(
            f"script-bank collision at {code_sheet}!{a1}: "
            f"{owners[key]} vs {cell.sheet}!{cell.cell} (keeping first)"
        )
    return banks, warnings


def collect_safety_warnings(code: str) -> list[str]:
    """Advisory checks: leftover ``xl()``, imports outside the venv whitelist."""
    warnings: list[str] = []
    src = code or ""
    if _XL_CALL_RE.search(src):
        warnings.append("leftover xl() call (venv xl is Calc helpers, not Excel sheet bridge)")

    try:
        tree = ast.parse(src)
    except SyntaxError as exc:
        warnings.append(f"syntax error: {exc.msg}")
        return warnings

    try:
        from plugin.scripting.import_policy import venv_authorized_top_level_modules

        authorized = set(venv_authorized_top_level_modules())
    except Exception:
        authorized = {"numpy", "pandas", "matplotlib", "seaborn", "scipy", "sklearn", "sympy"}

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                root = (alias.name or "").split(".", 1)[0]
                if root and root not in authorized:
                    warnings.append(f"import not in venv whitelist: {alias.name}")
        elif isinstance(node, ast.ImportFrom) and node.module:
            root = node.module.split(".", 1)[0]
            if root and root not in authorized:
                warnings.append(f"import not in venv whitelist: {node.module}")
        elif isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Name) and func.id == "xl":
                warnings.append("leftover xl() call (venv xl is Calc helpers, not Excel sheet bridge)")
    return list(dict.fromkeys(warnings))


def report_safety_warnings(report: ConversionReport) -> list[str]:
    """Flatten per-cell safety warnings for logging (does not fail conversion)."""
    out: list[str] = []
    for cell in report.cells:
        if not cell.converted or not cell.converted_code:
            continue
        for w in collect_safety_warnings(cell.converted_code):
            out.append(f"{cell.sheet}!{cell.cell}: {w}")
    return out


def ensure_code_sheet_openpyxl(wb: Any, code_sheet: str) -> Any:
    """Return (creating if needed) a visible bank worksheet named *code_sheet*."""
    if code_sheet in wb.sheetnames:
        ws = wb[code_sheet]
    else:
        ws = wb.create_sheet(code_sheet)
    ws[CODE_SHEET_NOTE_CELL] = CODE_SHEET_NOTE
    return ws


def write_script_bank_openpyxl(wb: Any, banks: dict[str, dict[str, str]]) -> None:
    """Write per-source-sheet banks onto ``py_code_<Sheet>`` worksheets."""
    for code_sheet, bank in sorted(banks.items()):
        if not bank:
            continue
        ws = ensure_code_sheet_openpyxl(wb, code_sheet)
        for a1, code in sorted(bank.items()):
            ws[a1] = code


def ensure_code_sheet_uno(doc: Any, code_sheet: str) -> Any:
    """Return (creating if needed) a visible bank sheet named *code_sheet*."""
    sheets = doc.getSheets()
    if sheets.hasByName(code_sheet):
        sheet = sheets.getByName(code_sheet)
    else:
        sheets.insertNewByName(code_sheet, sheets.getCount())
        sheet = sheets.getByName(code_sheet)
    try:
        sheet.getCellRangeByName(CODE_SHEET_NOTE_CELL).setString(CODE_SHEET_NOTE)
    except Exception:
        log.debug("excel_py script bank: could not write discoverability note", exc_info=True)
    return sheet


def write_script_bank_uno(doc: Any, banks: dict[str, dict[str, str]]) -> None:
    for code_sheet, bank in sorted(banks.items()):
        if not bank:
            continue
        sheet = ensure_code_sheet_uno(doc, code_sheet)
        for a1, code in sorted(bank.items()):
            sheet.getCellRangeByName(a1).setString(code)
