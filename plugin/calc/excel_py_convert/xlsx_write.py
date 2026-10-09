# SPDX-License-Identifier: GPL-3.0-or-later
"""Write Excel OOXML packages for DAG =PY and native Python-in-Excel (_xlws.PY).

Handles atomic ZIP writes, worksheet XML patching, script bank emission,
cell/row ordering in sheetData, and spill range clearing.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import TYPE_CHECKING, Any
from xml.etree import ElementTree as ET  # nosemgrep: use-defused-xml  # local .xlsx ZIP parts
import zipfile

from plugin.calc.excel_py_convert.ooxml_util import (
    CONTENT_TYPES_NS,
    PACKAGE_REL_NS,
    SSML_NS,
    find_all,
    find_child,
    local_name,
    parse_and_extract_namespaces,
    rewrite_zip,
    serialize_xml_preserving_namespaces,
    workbook_sheets,
)
from plugin.calc.excel_py_convert.script_bank import (
    CODE_SHEET_PREFIX,
    collect_script_bank,
    formula_for_converted_cell,
    iter_a1_span,
    report_safety_warnings,
    write_script_bank_openpyxl,
)
from plugin.calc.excel_py_convert.to_excel import (
    assign_script_bank,
    deps_for_xlws_export,
    python_scripts_xml,
    xlws_py_formula,
)

if TYPE_CHECKING:
    from plugin.calc.excel_py_convert.models import ConversionReport, ConvertedCell

log = logging.getLogger(__name__)

_RE_A1 = re.compile(r"^([A-Za-z]+)(\d+)$")


def _xlsx_formula_for_cell(cell: ConvertedCell) -> str:
    """Render a comma-separated OOXML ``=PY`` formula (script bank ref, no Calc sanitizer)."""
    return formula_for_converted_cell(cell, separator=",", excel_escape=True, use_script_bank=True)


def non_anchor_spill_cells(anchor: str, array_ref: str) -> list[str]:
    """Return all coordinates in *array_ref* except the *anchor* cell."""
    if not array_ref:
        return []
    clean_anchor = anchor.replace("$", "").upper()
    return [c for c in iter_a1_span(array_ref) if c.replace("$", "").upper() != clean_anchor]


def _clear_spill_range(ws: Any, anchor: str, array_ref: str) -> None:
    """Clear cached/array result cells in *array_ref*, keeping the anchor for rewrite."""
    for coord in non_anchor_spill_cells(anchor, array_ref):
        try:
            ws[coord].value = None
        except Exception:
            continue


def _strip_content_types_python(data: bytes) -> bytes:
    """Remove Python-in-Excel Override elements from [Content_Types].xml."""
    # Parse with ElementTree, remove only the matching Override elements,
    # and write the package namespaces plus the XML declaration. Excel and
    # LibreOffice emit [Content_Types].xml as one line, so dropping every
    # line that mentions a python part deletes the file.
    try:
        root, ns_map = parse_and_extract_namespaces(data)
    except ET.ParseError:
        return data
    changed = False
    for child in list(root):
        if local_name(child.tag) == "Override":
            part_name = child.attrib.get("PartName", "")
            if "pythonScripts" in part_name or "pythonScript" in part_name or part_name.startswith("/xl/python"):
                root.remove(child)
                changed = True
    if not changed:
        return data
    ET.register_namespace("", CONTENT_TYPES_NS)
    return serialize_xml_preserving_namespaces(root, ns_map)


def _strip_rels_python(data: bytes) -> bytes:
    """Remove Python-in-Excel Relationship elements from .rels files."""
    # Parse with ElementTree, remove only the matching Relationship
    # elements, and write the package namespaces plus the XML declaration.
    # A single-line .rels file loses its whole body when every line that
    # mentions pythonScripts is dropped.
    try:
        root, ns_map = parse_and_extract_namespaces(data)
    except ET.ParseError:
        return data
    changed = False
    for child in list(root):
        if local_name(child.tag) == "Relationship":
            target = child.attrib.get("Target", "")
            if "pythonScripts" in target or "pythonScript" in target or target.startswith("python") or target.startswith("/xl/python"):
                root.remove(child)
                changed = True
    if not changed:
        return data
    ET.register_namespace("", PACKAGE_REL_NS)
    return serialize_xml_preserving_namespaces(root, ns_map)


def _strip_python_in_excel_parts(out_path: Path) -> None:
    """Remove obsolete Python-in-Excel package parts after formula rewrite."""
    drop_prefixes = ("xl/python",)
    drop_exact = {"xl/pythonScripts.xml"}

    def _transform(zin: zipfile.ZipFile, zout: zipfile.ZipFile) -> None:
        for info in zin.infolist():
            name = info.filename
            if name in drop_exact or any(name.startswith(p) for p in drop_prefixes):
                continue
            data = zin.read(name)
            if name == "[Content_Types].xml":
                data = _strip_content_types_python(data)
            elif name.endswith(".rels"):
                data = _strip_rels_python(data)
            zout.writestr(info, data)

    rewrite_zip(out_path, out_path, _transform, temp_suffix=".tmpstrip")


def write_dag_formulas_xlsx(source_xlsx: str | Path, report: ConversionReport, out_path: str | Path, *, strip_python_parts: bool = True) -> None:
    """Copy *source_xlsx* and replace successfully converted PY cells with DAG formulas.

    Note: openpyxl may alter or drop unsupported package parts and drawing markup on save.
    Where package fidelity is essential, prefer direct XML patching via write_excel_python_xlsx.

    - Parks rewritten Python on visible ``py_code_<Sheet>`` sheets at the **same A1**
      as each caller when ``len(code) > 1000``; shorter scripts stay inline in ``=PY("…")``.
    - OOXML formulas use **comma** separators (not Calc ``;``).
    - Clears the source array/spill ``ref`` range (except the anchor) so old
      cached results do not block the new spill.
    - Fails closed on unmapped sheet titles (no silent first-sheet fallback).
    - Optionally strips ``xl/pythonScripts.xml`` and related package parts.
    """
    from openpyxl import load_workbook
    from openpyxl.utils.exceptions import IllegalCharacterError

    source_xlsx = Path(source_xlsx)
    out_path = Path(out_path)
    wb = load_workbook(source_xlsx)
    try:
        sheet_by_key = {ws.title: ws for ws in wb.worksheets}
        errors: list[str] = []

        bank, bank_warnings = collect_script_bank(report)
        for w in bank_warnings:
            log.warning("excel_py convert: %s", w)
        for w in report_safety_warnings(report):
            log.warning("excel_py convert: %s", w)
        write_script_bank_openpyxl(wb, bank)

        for cell in report.cells:
            if not cell.converted or not cell.converted_code:
                continue
            ws = sheet_by_key.get(cell.sheet)
            if ws is None:
                # Accept sheet1 → first sheet only when the report used fixture aliases
                # AND there is exactly one worksheet — still prefer exact titles.
                lower = {t.lower(): w for t, w in sheet_by_key.items()}
                ws = lower.get(cell.sheet.lower())
            if ws is None:
                errors.append(f"unmapped sheet {cell.sheet!r} for cell {cell.cell}")
                continue
            if cell.array_ref:
                _clear_spill_range(ws, cell.cell, cell.array_ref)
            formula = _xlsx_formula_for_cell(cell)
            try:
                ws[cell.cell] = formula
            except IllegalCharacterError as exc:
                errors.append(f"{cell.sheet}!{cell.cell}: {exc}")

        if errors:
            raise ValueError("write_dag_formulas_xlsx failed:\n" + "\n".join(errors))

        wb.save(out_path)
    finally:
        # Cleanup: close the openpyxl workbook in try/finally on all errors.
        wb.close()
    if strip_python_parts:
        _strip_python_in_excel_parts(out_path)


def _a1_row_col(a1: str) -> tuple[int, int] | None:
    """Return (row_1_based, col_1_based) for an A1 reference."""
    m = _RE_A1.match((a1 or "").replace("$", "").strip())
    if not m:
        return None
    col_str, row_str = m.group(1).upper(), m.group(2)
    col = 0
    for ch in col_str:
        col = col * 26 + (ord(ch) - 64)
    return int(row_str), col


def _ensure_sheet_data(ws_root: ET.Element) -> ET.Element:
    for child in list(ws_root):
        if local_name(child.tag) == "sheetData":
            return child
    tag = f"{{{SSML_NS}}}sheetData" if ws_root.tag.startswith("{") else "sheetData"
    el = ET.SubElement(ws_root, tag)
    return el


def _ensure_cell(ws_root: ET.Element, a1: str) -> ET.Element:
    """Return ``<c r="A1">``, creating row/cell under sheetData in ascending order when missing.

    Walk rows and cells, tracking the implicit 1-based index when ``r`` is
    omitted. Insert a missing row or cell at that sorted position.
    ElementTree's SubElement always appends, which breaks OOXML's ascending
    order, and a lookup that only sees an explicit ``r`` misses those cells
    and inserts a duplicate.
    """
    parsed = _a1_row_col(a1)
    if parsed is None:
        raise ValueError(f"invalid A1: {a1!r}")
    target_row, target_col = parsed
    sheet_data = _ensure_sheet_data(ws_root)

    # 1. Locate or insert <row> in ascending order
    row_el: ET.Element | None = None
    insert_row_idx = len(sheet_data)
    current_row_num = 0

    for idx, row in enumerate(list(sheet_data)):
        if local_name(row.tag) != "row":
            continue
        r_attr = row.attrib.get("r")
        if r_attr is not None:
            try:
                current_row_num = int(r_attr)
            except ValueError:
                current_row_num += 1
        else:
            current_row_num += 1

        if current_row_num == target_row:
            row_el = row
            break
        elif current_row_num > target_row:
            insert_row_idx = idx
            break

    if row_el is None:
        row_tag = f"{{{SSML_NS}}}row" if sheet_data.tag.startswith("{") else "row"
        row_el = ET.Element(row_tag, attrib={"r": str(target_row)})
        sheet_data.insert(insert_row_idx, row_el)

    # 2. Locate or insert <c> in ascending order within row_el
    col_str = ""
    n = target_col
    while n > 0:
        n, rem = divmod(n - 1, 26)
        col_str = chr(65 + rem) + col_str
    a1_canonical = f"{col_str}{target_row}"

    insert_col_idx = len(row_el)
    current_col_num = 0

    for idx, c in enumerate(list(row_el)):
        if local_name(c.tag) != "c":
            continue
        c_r = c.attrib.get("r")
        if c_r:
            parsed_c = _a1_row_col(c_r)
            if parsed_c is not None:
                current_col_num = parsed_c[1]
            else:
                current_col_num += 1
        else:
            current_col_num += 1

        if current_col_num == target_col:
            return c
        elif current_col_num > target_col:
            insert_col_idx = idx
            break

    c_tag = f"{{{SSML_NS}}}c" if row_el.tag.startswith("{") else "c"
    cell = ET.Element(c_tag, attrib={"r": a1_canonical})
    row_el.insert(insert_col_idx, cell)
    return cell


def _set_cell_xlws_formula(ws_root: ET.Element, a1: str, formula: str, array_ref: str = "") -> None:
    """Set formula on cell, clearing stale si, ca, v, is, and t attributes.

    Delete ``si`` and ``ca`` from ``<f>``. A former shared-formula cell
    keeps them, and they are invalid on an array formula or any formula
    that is no longer shared.
    """
    cell = _ensure_cell(ws_root, a1)
    # Drop cached value / type so Excel recalculates from the formula.
    for child in list(cell):
        if local_name(child.tag) in ("v", "is"):
            cell.remove(child)
    if "t" in cell.attrib:
        del cell.attrib["t"]
    f = find_child(cell, "f")
    if f is None:
        f_tag = f"{{{SSML_NS}}}f" if cell.tag.startswith("{") else "f"
        f = ET.SubElement(cell, f_tag)
    body = formula[1:] if formula.startswith("=") else formula
    f.text = body
    if "si" in f.attrib:
        del f.attrib["si"]
    if "ca" in f.attrib:
        del f.attrib["ca"]
    if array_ref:
        f.set("t", "array")
        f.set("ref", array_ref.replace("$", ""))
    else:
        if "t" in f.attrib:
            del f.attrib["t"]
        if "ref" in f.attrib:
            del f.attrib["ref"]


def _clear_spill_xml(ws_root: ET.Element, anchor: str, array_ref: str, cell_map: dict[str, ET.Element] | None = None) -> None:
    """Clear cached/array result cells in *array_ref* from XML, keeping the anchor.

    Build one coordinate-to-cell dict. Scanning every ``<c>`` again for
    each spill coordinate is quadratic in the sheet size.
    """
    if not array_ref:
        return
    if cell_map is None:
        cell_map = {
            (c.attrib.get("r") or "").replace("$", "").upper(): c
            for c in find_all(ws_root, "c")
            if c.attrib.get("r")
        }
    for coord in non_anchor_spill_cells(anchor, array_ref):
        c = cell_map.get(coord.replace("$", "").upper())
        if c is not None:
            for child in list(c):
                if local_name(child.tag) in ("f", "v", "is"):
                    c.remove(child)
            if "t" in c.attrib:
                del c.attrib["t"]


def _patch_content_types(data: bytes, drop_parts: set[str] | None = None) -> bytes:
    """Ensure pythonScripts.xml Override is present and remove Overrides for drop_parts."""
    # TODO (Review Item 4): Verify pythonScripts.xml relationship and content type against
    # real Excel files if a fixture becomes available. Currently application/xml is used.
    try:
        root, ns_map = parse_and_extract_namespaces(data)
    except ET.ParseError:
        return data
    norm_drop = {p.lstrip("/") for p in drop_parts} if drop_parts else set()
    for child in list(root):
        if local_name(child.tag) == "Override":
            pn = (child.attrib.get("PartName") or "").lstrip("/")
            if pn in norm_drop:
                root.remove(child)
    has_py = any(
        local_name(c.tag) == "Override" and c.attrib.get("PartName") == "/xl/pythonScripts.xml"
        for c in root
    )
    if not has_py:
        tag = f"{{{CONTENT_TYPES_NS}}}Override" if root.tag.startswith("{") else "Override"
        ET.SubElement(
            root,
            tag,
            attrib={"PartName": "/xl/pythonScripts.xml", "ContentType": "application/xml"},
        )
    ET.register_namespace("", CONTENT_TYPES_NS)
    return serialize_xml_preserving_namespaces(root, ns_map)


def _drop_py_code_sheets_from_workbook(wb_xml: bytes, rels_xml: bytes, drop_rids: set[str]) -> tuple[bytes, bytes]:
    """Remove ``py_code_*`` sheet entries from workbook.xml + workbook rels."""
    if not drop_rids:
        return wb_xml, rels_xml
    wb, wb_ns = parse_and_extract_namespaces(wb_xml)
    for sheets_el in list(wb):
        if local_name(sheets_el.tag) != "sheets":
            continue
        for sh in list(sheets_el):
            if local_name(sh.tag) != "sheet":
                continue
            rid = ""
            for k, v in sh.attrib.items():
                if k.endswith("}id") or k in ("r:id", "id"):
                    rid = v
                    break
            title = sh.attrib.get("name") or ""
            if rid in drop_rids or title.startswith(CODE_SHEET_PREFIX):
                sheets_el.remove(sh)
    rels, rels_ns = parse_and_extract_namespaces(rels_xml)
    for rel in list(rels):
        if local_name(rel.tag) != "Relationship":
            continue
        if (rel.attrib.get("Id") or "") in drop_rids:
            rels.remove(rel)
    from plugin.calc.excel_py_convert.ooxml_util import OFFICE_DOC_REL_NS

    ET.register_namespace("", SSML_NS)
    ET.register_namespace("r", OFFICE_DOC_REL_NS)
    wb_bytes = serialize_xml_preserving_namespaces(wb, wb_ns)
    ET.register_namespace("", PACKAGE_REL_NS)
    rels_bytes = serialize_xml_preserving_namespaces(rels, rels_ns)
    return wb_bytes, rels_bytes


def write_excel_python_xlsx(source_xlsx: str | Path, report: ConversionReport, out_path: str | Path) -> None:
    """Write native Excel Python-in-Excel package (stdlib ZipFile only).

    - Banks ``converted_code`` into ``xl/pythonScripts.xml`` (``xl(%Pn%)`` bodies)
    - Sets each cell to ``_xlfn._xlws.PY(scriptIndex, returnType, deps…)``
    - Patches ``[Content_Types].xml``; strips Calc ``py_code_*`` sheets from the package
    """
    source_xlsx = Path(source_xlsx)
    out_path = Path(out_path)
    if report.direction != "excel":
        raise ValueError("write_excel_python_xlsx requires report.direction == 'excel'")
    scripts, _cells = assign_script_bank(report.cells)
    if not scripts or not any(c.converted and c.script_index >= 0 for c in report.cells):
        raise ValueError("write_excel_python_xlsx: no converted Excel PY cells to write")

    # sheet title → list of ConvertedCell
    by_sheet: dict[str, list[ConvertedCell]] = {}
    for cell in report.cells:
        if not cell.converted or cell.script_index < 0:
            continue
        by_sheet.setdefault(cell.sheet, []).append(cell)

    errors: list[str] = []

    with zipfile.ZipFile(source_xlsx, "r") as zin:
        sheets = workbook_sheets(zin)
        title_to_part = {s.title: s.part_name for s in sheets if s.part_name}
        title_to_part_l = {s.title.lower(): s.part_name for s in sheets if s.part_name}
        # Deduplicated py_code sheet discovery via SheetInfo.rel_id:
        code_sheets = [s for s in sheets if s.title.startswith(CODE_SHEET_PREFIX)]
        drop_parts = {s.part_name for s in code_sheets if s.part_name}
        drop_rids = {s.rel_id for s in code_sheets if s.rel_id}

        # Pre-parse worksheet roots we will patch
        patched: dict[str, ET.Element] = {}
        original_namespaces: dict[str, dict[str, str]] = {}
        for sheet_title, cells in by_sheet.items():
            part = title_to_part.get(sheet_title) or title_to_part_l.get(sheet_title.lower(), "")
            if not part:
                errors.append(f"unmapped sheet {sheet_title!r}")
                continue
            root = patched.get(part)
            if root is None:
                try:
                    root, ns_map = parse_and_extract_namespaces(zin.read(part))
                    original_namespaces[part] = ns_map
                except KeyError:
                    errors.append(f"missing worksheet part {part!r} for {sheet_title!r}")
                    continue
            cell_map = {
                (c.attrib.get("r") or "").replace("$", "").upper(): c
                for c in find_all(root, "c")
                if c.attrib.get("r")
            }
            for cell in cells:
                try:
                    if cell.array_ref:
                        _clear_spill_xml(root, cell.cell, cell.array_ref, cell_map=cell_map)
                    formula = xlws_py_formula(cell.script_index, cell.return_type, deps_for_xlws_export(cell))
                    _set_cell_xlws_formula(root, cell.cell, formula, array_ref=cell.array_ref)
                except Exception as exc:
                    errors.append(f"{cell.sheet}!{cell.cell}: {exc}")
            patched[part] = root

        if errors:
            raise ValueError("write_excel_python_xlsx failed:\n" + "\n".join(errors))

        scripts_bytes = python_scripts_xml(scripts)
        wb_xml = zin.read("xl/workbook.xml")
        rels_xml = zin.read("xl/_rels/workbook.xml.rels")
        if drop_rids:
            wb_xml, rels_xml = _drop_py_code_sheets_from_workbook(wb_xml, rels_xml, drop_rids)

    def _transform(zin: zipfile.ZipFile, zout: zipfile.ZipFile) -> None:
        written_scripts = False
        for info in zin.infolist():
            name = info.filename
            if name in drop_parts:
                continue
            if name.startswith("xl/worksheets/_rels/") and any(p.split("/")[-1] in name for p in drop_parts):
                # Drop sheet rels for removed py_code sheets when path matches.
                base = name.rsplit("/", 1)[-1].replace(".rels", "")
                if any(p.endswith(base) for p in drop_parts):
                    continue
            if name == "xl/pythonScripts.xml":
                zout.writestr(info, scripts_bytes)
                written_scripts = True
                continue
            if name == "[Content_Types].xml":
                data = _patch_content_types(zin.read(name), drop_parts=drop_parts)
                zout.writestr(info, data)
                continue
            if name == "xl/workbook.xml":
                zout.writestr(info, wb_xml)
                continue
            if name == "xl/_rels/workbook.xml.rels":
                zout.writestr(info, rels_xml)
                continue
            if name in patched:
                ET.register_namespace("", SSML_NS)
                data = serialize_xml_preserving_namespaces(patched[name], original_namespaces.get(name))
                zout.writestr(info, data)
                continue
            zout.writestr(info, zin.read(name))
        if not written_scripts:
            zout.writestr("xl/pythonScripts.xml", scripts_bytes)

    rewrite_zip(source_xlsx, out_path, _transform, temp_suffix=".tmpexcelpy")
