# SPDX-License-Identifier: GPL-3.0-or-later
"""Scan a workbook for DAG-style ``=PY`` / ``=PYTHON`` formulas (stdlib ZipFile)."""

from __future__ import annotations

import re
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET  # nosemgrep: use-defused-xml  # local .xlsx ZIP parts

from plugin.calc.excel_py_convert.parse_excel_ooxml import _findall, _find_child, _local, _unescape_xml, _workbook_sheets
from plugin.calc.excel_py_convert.script_bank import CODE_SHEET_PREFIX, normalize_bank_a1
from plugin.calc.python.formula_edit import parse_python_formula

BANK_REF_RE = re.compile(rf"^({re.escape(CODE_SHEET_PREFIX)}[^.!]+)[.!](\$?[A-Za-z]+\$?\d+)$", re.IGNORECASE)


def _shared_strings(zf: zipfile.ZipFile) -> list[str]:
    try:
        root = ET.fromstring(zf.read("xl/sharedStrings.xml"))
    except KeyError:
        return []
    out: list[str] = []
    for si in list(root):
        if _local(si.tag) != "si":
            continue
        parts: list[str] = []
        for t in si.iter():
            if _local(t.tag) == "t" and t.text:
                parts.append(t.text)
        out.append("".join(parts))
    return out


def _cell_string_value(c: ET.Element, shared: list[str]) -> str:
    """Read a cell's display/string value (shared string, inlineStr, or ``v``)."""
    t = (c.attrib.get("t") or "").lower()
    if t == "inlineStr":
        is_el = _find_child(c, "is")
        if is_el is None:
            return ""
        parts: list[str] = []
        for el in is_el.iter():
            if _local(el.tag) == "t" and el.text:
                parts.append(el.text)
        return "".join(parts)
    v = _find_child(c, "v")
    if v is None or v.text is None:
        return ""
    raw = v.text
    if t == "s":
        try:
            return shared[int(raw)]
        except (ValueError, IndexError):
            return ""
    return raw


def _sheet_cell_map(ws_root: ET.Element, shared: list[str]) -> dict[str, tuple[str, str]]:
    """Map A1 → (formula_or_empty, string_value)."""
    out: dict[str, tuple[str, str]] = {}
    for c in _findall(ws_root, "c"):
        a1 = (c.attrib.get("r") or "").replace("$", "")
        if not a1:
            continue
        f = _find_child(c, "f")
        formula = _unescape_xml("".join(f.itertext()).strip()) if f is not None else ""
        if formula and not formula.startswith("="):
            formula = "=" + formula
        out[a1] = (formula, _cell_string_value(c, shared))
    return out


def resolve_code_bank_ref(code: str, sheet_cells: dict[str, dict[str, tuple[str, str]]]) -> str | None:
    """If *code* is ``py_code_Sheet.A1``, return that cell's string; else None."""
    m = BANK_REF_RE.match((code or "").strip())
    if not m:
        return None
    sheet = m.group(1)
    try:
        a1 = normalize_bank_a1(m.group(2))
    except ValueError:
        return None
    cell_map = sheet_cells.get(sheet)
    if cell_map is None:
        # Case-insensitive sheet title match
        lower = {k.lower(): v for k, v in sheet_cells.items()}
        cell_map = lower.get(sheet.lower())
    if not cell_map:
        return None
    _formula, value = cell_map.get(a1, ("", ""))
    return value if value else None


def iter_dag_py_formulas_xlsx(path: str | Path) -> list[tuple[str, str, str]]:
    """Return ``(sheet_title, cell, formula)`` for PY/PYTHON cells in an ``.xlsx``.

    Resolves ``=PY(py_code_Sheet.A1; …)`` bank refs to inline ``=PY("…"; …)`` so
    reverse export banks real Python into ``pythonScripts.xml``.
    """
    path = Path(path)
    out: list[tuple[str, str, str]] = []
    with zipfile.ZipFile(path, "r") as zf:
        sheets = _workbook_sheets(zf)
        shared = _shared_strings(zf)
        sheet_cells: dict[str, dict[str, tuple[str, str]]] = {}
        for sh in sheets:
            if not sh.part_name:
                continue
            try:
                ws_root = ET.fromstring(zf.read(sh.part_name))
            except KeyError:
                continue
            sheet_cells[sh.title] = _sheet_cell_map(ws_root, shared)

        for sh in sheets:
            # Skip Calc script-bank sheets as formula hosts (they hold source text only).
            if sh.title.startswith(CODE_SHEET_PREFIX):
                continue
            cell_map = sheet_cells.get(sh.title) or {}
            for a1, (formula, _value) in cell_map.items():
                if not formula:
                    continue
                parts = parse_python_formula(formula)
                if parts is None:
                    continue
                resolved = resolve_code_bank_ref(parts.code, sheet_cells)
                if resolved is not None:
                    # Excel escape only — do not run Calc formula sanitizer on banked source.
                    from plugin.calc.python.formula_edit import CALC_PYTHON_FN, escape_code_for_excel_formula

                    formula = f'={CALC_PYTHON_FN}("{escape_code_for_excel_formula(resolved)}"{parts.data_suffix}'
                out.append((sh.title, a1, formula))
    return out
