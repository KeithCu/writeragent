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
from typing import TYPE_CHECKING, Any, Callable

from plugin.calc.excel_py_convert.parse_excel_ooxml import letters_to_col_index
from plugin.calc.python.formula_edit import rebuild_python_formula_with_code_ref, rebuild_python_formula_with_data

if TYPE_CHECKING:
    from plugin.calc.excel_py_convert.models import ConversionReport, ConvertedCell

log = logging.getLogger(__name__)

CODE_SHEET_PREFIX = "py_code_"
BANK_REF_RE = re.compile(rf"^({re.escape(CODE_SHEET_PREFIX)}[^.!]+)[.!](\$?[A-Za-z]+\$?\d+)$", re.IGNORECASE)
# Calc / Excel sheet title practical max.
_CODE_SHEET_MAX_LEN = 31
CODE_SHEET_NOTE_CELL = "ZZ1"
INLINE_CODE_MAX_CHARS = 1000
CODE_SHEET_NOTE = (
    "Python script bank (from Excel pythonScripts). "
    f"Used only for scripts longer than {INLINE_CODE_MAX_CHARS} characters (Calc formula string limit). "
    "Each cell here holds the Python for the =PY formula at the same address "
    "on the matching data sheet (e.g. H4 here ↔ H4 on the source sheet). "
    "Enable shared-kernel session mode for multi-cell Excel scripts."
)

_RE_A1 = re.compile(r"^\$?([A-Za-z]+)\$?(\d+)$")
_A1_SPAN_CELL_RE = re.compile(r"^([A-Za-z]+)(\d+)$")
_SAFE_SHEET = re.compile(r"[^\w]+", re.UNICODE)
# Binding-only sandbox xl accepts "%Pn%" strings (same as excel_xl.make_xl).
_P_TOKEN_RE = re.compile(r"^%P(\d+)%$", re.IGNORECASE)


def code_sheet_name_for(source_sheet: str) -> str:
    """Bank sheet name for *source_sheet* (``Pivots`` → ``py_code_Pivots``).

    Sanitizes non-word characters and truncates to 31 chars. Callers like
    ``collect_script_bank`` detect collisions when distinct source sheets
    map to the same bank sheet name.
    """
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
        left, _unused, right = raw.partition(".")
        if right and _RE_A1.match(right) and not _RE_A1.match(left):
            raw = right
    m = _RE_A1.match(raw)
    if not m:
        raise ValueError(f"invalid bank A1 address: {cell!r}")
    return f"{m.group(1).upper()}{m.group(2)}"


def resolve_bank_cell_reference(
    code: str,
    lookup_fn: Callable[[str, str], str | None],
) -> str | None:
    """If *code* references a script bank cell (py_code_Sheet.A1), resolve via *lookup_fn(sheet, a1)*.

    Shares bank reference pattern matching and A1 address normalization between
    offline OOXML extraction and live UNO Calc snapshotting.
    """
    m = BANK_REF_RE.match((code or "").strip())
    if not m:
        return None
    sheet = m.group(1)
    try:
        a1 = normalize_bank_a1(m.group(2))
    except ValueError:
        return None
    return lookup_fn(sheet, a1)


def a1_span_bounds(ref: str) -> tuple[tuple[int, int], tuple[int, int]] | None:
    """Return ((min_col, min_row), (max_col, max_row)) for *ref* (1-based), or None."""
    raw = (ref or "").replace("$", "").strip()
    if "!" in raw:
        raw = raw.split("!", 1)[1].strip()
    if not raw:
        return None
    if ":" not in raw:
        m = _A1_SPAN_CELL_RE.match(raw)
        if not m:
            return None
        c, r = letters_to_col_index(m.group(1)), int(m.group(2))
        return ((c, r), (c, r))
    left, right = raw.split(":", 1)
    m1, m2 = _A1_SPAN_CELL_RE.match(left.strip()), _A1_SPAN_CELL_RE.match(right.strip())
    if not m1 or not m2:
        return None
    c1, r1 = letters_to_col_index(m1.group(1)), int(m1.group(2))
    c2, r2 = letters_to_col_index(m2.group(1)), int(m2.group(2))
    return ((min(c1, c2), min(r1, r2)), (max(c1, c2), max(r1, r2)))


def code_bank_ref(source_sheet: str, cell_a1: str, *, excel_bang: bool = False) -> str:
    """Sheet-qualified ref: ``py_code_Pivots.H4`` or ``py_code_Pivots!H4``."""
    a1 = normalize_bank_a1(cell_a1)
    sheet = code_sheet_name_for(source_sheet)
    sep = "!" if excel_bang else "."
    return f"{sheet}{sep}{a1}"


def should_inline_code(code: str) -> bool:
    """True when *code* fits in ``=PY(\"…\")`` under Calc ``MAXSTRLEN`` (budget 1000)."""
    return len(code or "") <= INLINE_CODE_MAX_CHARS


def formula_for_converted_cell(cell: ConvertedCell, *, separator: str = ";", excel_escape: bool = False) -> str:
    """Build ``=PY`` for a converted cell.

    Inline when ``len(converted_code) <= 1000``; otherwise reference
    ``py_code_<Sheet>.A1`` (same address as the caller).
    """
    # Data bindings only — ordering_args are legacy/empty (never formula args).
    args = list(cell.data_args)
    excel_syntax = excel_escape or separator == ","
    if cell.cell and cell.sheet and not should_inline_code(cell.converted_code):
        try:
            return rebuild_python_formula_with_code_ref(code_bank_ref(cell.sheet, cell.cell, excel_bang=excel_syntax), args, separator=separator, excel_ranges=excel_syntax)
        except ValueError as exc:
            if len(cell.converted_code) > INLINE_CODE_MAX_CHARS:
                err = f"script too long ({len(cell.converted_code)} chars) and bank ref failed: {exc}"
                raise ValueError(err) from exc
    if len(cell.converted_code) > INLINE_CODE_MAX_CHARS:
        err = f"script too long ({len(cell.converted_code)} chars) to inline"
        raise ValueError(err)
    return rebuild_python_formula_with_data(cell.converted_code, args, separator=separator, excel_escape=excel_escape)


def collect_script_bank(report: ConversionReport) -> tuple[dict[str, dict[str, str]], list[str]]:
    """Map ``code_sheet_name → {A1 → code}`` for cells that need banking (>1000 chars)."""
    banks: dict[str, dict[str, str]] = {}
    owners: dict[tuple[str, str], str] = {}
    warnings: list[str] = []
    code_sheet_sources: dict[str, str] = {}
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
        # Two source sheets that sanitize to the same py_code_* bank name
        # fail the conversion. A per-cell warning that keeps the first
        # script drops the second.
        existing_sheet = code_sheet_sources.setdefault(code_sheet, cell.sheet)
        if existing_sheet != cell.sheet:
            err = f"script-bank sheet name collision: {existing_sheet!r} and {cell.sheet!r} both map to {code_sheet!r}"
            report.issues.append(err)
            raise ValueError(err)

        bank = banks.setdefault(code_sheet, {})
        key = (code_sheet, a1)
        prev = bank.get(a1)
        if prev is None:
            bank[a1] = cell.converted_code
            owners[key] = f"{cell.sheet}!{cell.cell}"
            continue
        if prev == cell.converted_code:
            continue
        err = f"script-bank collision at {code_sheet}!{a1}: {owners[key]} vs {cell.sheet}!{cell.cell}"
        report.issues.append(err)
        raise ValueError(err)
    return banks, warnings


def _is_binding_xl_call(node: ast.Call) -> bool:
    """True when ``xl("%Pn%")`` — intentional sandbox bridge, not a live sheet read."""
    if not (isinstance(node.func, ast.Name) and node.func.id == "xl"):
        return False
    if not node.args:
        return False
    ref = node.args[0]
    if isinstance(ref, ast.Constant) and isinstance(ref.value, str):
        return bool(_P_TOKEN_RE.match(ref.value.strip()))
    return False


def collect_safety_warnings(code: str) -> list[str]:
    """Advisory checks: non-binding ``xl()``, imports blocked by the sandbox.

    ``xl("%Pn%")`` is the converted Excel shape (sandbox injects binding-only ``xl``).
    Warn only on A1 / dynamic / non-string forms that are not DAG-safe.
    """
    warnings: list[str] = []
    src = code or ""

    try:
        tree = ast.parse(src)
    except SyntaxError as exc:
        warnings.append(f"syntax error: {exc.msg}")
        return warnings

    try:
        from plugin.scripting.sandbox import build_sandbox_policy, import_authorized, policy_blocked_imports

        blocked = policy_blocked_imports(build_sandbox_policy())

        def is_blocked(name: str) -> bool:
            return not import_authorized(name, (), blocked)
    except ImportError:
        _FALLBACK_BLOCKED = (
            "subprocess",
            "socket",
            "shutil",
            "tempfile",
            "sqlite3",
            "pickle",
            "importlib",
            "ctypes",
        )

        def is_blocked(name: str) -> bool:
            root = (name or "").split(".", 1)[0]
            return root in _FALLBACK_BLOCKED

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name and is_blocked(alias.name):
                    warnings.append(f"import is blocked by the Python sandbox: {alias.name}")
        elif isinstance(node, ast.ImportFrom) and node.module:
            if is_blocked(node.module):
                warnings.append(f"import is blocked by the Python sandbox: {node.module}")
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "xl":
            if not _is_binding_xl_call(node):
                warnings.append('non-binding xl() call (sandbox xl only resolves "%Pn%" formula bindings; no live sheet reads)')
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


def bank_defined_name_for(code_sheet: str) -> str:
    """Workbook defined name identifying *code_sheet* as a script bank sheet."""
    return f"_py_code_bank_{code_sheet}"


def defined_name_targets_sheet(name: str, val: str, sheet_title: str) -> bool:
    """True if defined name *name* or its formula targets *sheet_title* exactly."""
    target_l = sheet_title.lower()
    if name.strip().lower() == bank_defined_name_for(sheet_title).lower():
        return True
    if "!" in val:
        token = val.split("!", 1)[0].strip()
        if len(token) >= 2 and token[0] in ("'", '"') and token[-1] == token[0]:
            token = token[1:-1]
        token = token.replace("''", "'")
        if token.lower() == target_l:
            return True
    return False


def _uno_sheet_is_bank(doc: Any, code_sheet: str, sheet: Any) -> bool:  # pyright: ignore[reportUnusedFunction]  # imported by apply_calc.py
    expected_def = bank_defined_name_for(code_sheet)
    named_ranges = getattr(doc, "NamedRanges", None)
    if named_ranges is not None:
        try:
            if hasattr(named_ranges, "hasByName") and named_ranges.hasByName(expected_def):
                return True
        except Exception:
            log.debug("script bank named-range probe failed for %s", expected_def, exc_info=True)
    try:
        val = sheet.getCellRangeByName(CODE_SHEET_NOTE_CELL).getString()
        if val == CODE_SHEET_NOTE:
            return True
    except Exception:
        log.debug("script bank note-cell probe failed for %s", code_sheet, exc_info=True)
    return False


