# SPDX-License-Identifier: GPL-3.0-or-later
"""Write Excel OOXML packages for DAG =PY and native Python-in-Excel (_xlws.PY).

Handles atomic ZIP writes, worksheet XML patching, script bank emission,
cell/row ordering in sheetData, and spill range clearing.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import TYPE_CHECKING
from xml.etree import ElementTree as ET  # nosemgrep: use-defused-xml  # local .xlsx ZIP parts
import zipfile

from plugin.calc.excel_py_convert.ooxml_util import (
    CALC_CHAIN_REL_TYPE_SUFFIX,
    CONTENT_TYPES_NS,
    OFFICE_DOC_REL_NS,
    PACKAGE_REL_NS,
    PYTHON_SCRIPT_REL_TYPE,
    SSML_NS,
    find_all,
    find_child,
    local_name,
    parse_and_extract_namespaces,
    rewrite_zip,
    serialize_xml_preserving_namespaces,
    workbook_sheets,
    xml_namespace_lock,
)
from plugin.calc.excel_py_convert.parse_dag_formulas import _shared_strings
from plugin.calc.excel_py_convert.parse_excel_ooxml import (
    _col_row,
    _shared_formula_map,
    _sheet_rels_path,
    col_index_to_letters,
    shift_xlws_py_formula,
)
from plugin.calc.formula_fill import adjust_a1_formula
from plugin.calc.excel_py_convert.script_bank import (
    CODE_SHEET_NOTE,
    CODE_SHEET_NOTE_CELL,
    CODE_SHEET_PREFIX,
    a1_span_bounds,
    bank_defined_name_for,
    collect_script_bank,
    defined_name_targets_sheet,
    formula_for_converted_cell,
    report_safety_warnings,
)
from plugin.calc.excel_py_convert.to_excel import (
    assign_script_bank,
    deps_for_xlws_export,
    python_scripts_xml,
    xlws_py_formula,
)

if TYPE_CHECKING:
    from plugin.calc.excel_py_convert.models import ConversionReport, ConvertedCell, SheetInfo

log = logging.getLogger(__name__)

_ILLEGAL_XML_CHARS_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")


def _xlsx_formula_for_cell(cell: ConvertedCell) -> str:
    """Render a comma-separated OOXML ``=PY`` formula (script bank ref, no Calc sanitizer)."""
    return formula_for_converted_cell(cell, separator=",", excel_escape=True)


def _unshare_groups_touching(ws_root: ET.Element, sheet_title: str, write_addrs: set[str]) -> None:
    """Expand shared formula groups touched by write_addrs so unwritten cells preserve shifted formulas."""
    shared_groups: dict[str, list[tuple[str, ET.Element, ET.Element]]] = {}
    shared_masters: dict[str, tuple[str, str]] = {}
    for c in find_all(ws_root, "c"):
        a1 = c.attrib.get("r") or ""
        f = find_child(c, "f")
        if f is None or not a1:
            continue
        if (f.attrib.get("t") or "") == "shared":
            si = f.attrib.get("si") or ""
            body = "".join(f.itertext()).strip()
            if body:
                shared_masters[si] = (body, a1)
            shared_groups.setdefault(si, []).append((a1, c, f))

    for si, members in shared_groups.items():
        any_in_write = any(m_a1.replace("$", "").upper() in write_addrs for m_a1, _, _ in members)
        if not any_in_write:
            continue
        all_in_write = all(m_a1.replace("$", "").upper() in write_addrs for m_a1, _, _ in members)
        if all_in_write:
            for _m_a1, _m_c, m_f in members:
                if "si" in m_f.attrib:
                    del m_f.attrib["si"]
                if "t" in m_f.attrib:
                    del m_f.attrib["t"]
                if "ref" in m_f.attrib:
                    del m_f.attrib["ref"]
        else:
            master_info = shared_masters.get(si)
            if master_info is None:
                raise ValueError(f"Shared formula group si={si} in {sheet_title!r} has no master body")
            master_body, master_a1 = master_info
            m_row, m_col = _col_row(master_a1)
            for m_a1, _m_c, m_f in members:
                if m_a1.replace("$", "").upper() in write_addrs:
                    if "si" in m_f.attrib:
                        del m_f.attrib["si"]
                    if "t" in m_f.attrib:
                        del m_f.attrib["t"]
                    if "ref" in m_f.attrib:
                        del m_f.attrib["ref"]
                else:
                    c_row, c_col = _col_row(m_a1)
                    delta_r = c_row - m_row
                    delta_c = c_col - m_col
                    if "_xlws.py" in master_body.lower():
                        shifted = shift_xlws_py_formula(master_body, delta_r, delta_c)
                        if shifted is None:
                            raise ValueError(f"Cannot unshare formula for {sheet_title}!{m_a1} in si={si}: shifter refused {master_body!r}")
                        m_f.text = shifted[0].lstrip("=")
                    else:
                        master_clean = master_body.lstrip("=")
                        shifted_text = adjust_a1_formula(master_clean, delta_c, delta_r)
                        if "#REF!" in shifted_text and "#REF!" not in master_clean:
                            raise ValueError(f"Cannot unshare formula for {sheet_title}!{m_a1} in si={si}: out-of-bounds shift produced #REF!")
                        m_f.text = shifted_text
                    if "si" in m_f.attrib:
                        del m_f.attrib["si"]
                    if "t" in m_f.attrib:
                        del m_f.attrib["t"]
                    if "ref" in m_f.attrib:
                        del m_f.attrib["ref"]


def _patch_content_types_dag(data: bytes, new_overrides: list[str], *, strip_python: bool) -> bytes:
    """Patch [Content_Types].xml for DAG export: inject worksheet Overrides and optionally strip Python."""
    try:
        root, ns_map = parse_and_extract_namespaces(data)
    except ET.ParseError as exc:
        raise ValueError(f"malformed [Content_Types].xml: {exc}") from exc
    changed = False
    for child in list(root):
        if local_name(child.tag) == "Override":
            part_name = child.attrib.get("PartName", "")
            if part_name == "/xl/calcChain.xml" or (
                strip_python
                and (
                    part_name == "/xl/pythonScripts.xml"
                    or part_name.startswith("/xl/pythonScripts/")
                )
            ):
                root.remove(child)
                changed = True
    existing_parts = {child.attrib.get("PartName", "") for child in root if local_name(child.tag) == "Override"}
    for part in new_overrides:
        if part not in existing_parts:
            tag = f"{{{CONTENT_TYPES_NS}}}Override" if root.tag.startswith("{") else "Override"
            ET.SubElement(
                root,
                tag,
                attrib={"PartName": part, "ContentType": "application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"},
            )
            existing_parts.add(part)
            changed = True
    if not changed:
        return data
    return serialize_xml_preserving_namespaces(root, ns_map)


def _strip_content_types_python(data: bytes) -> bytes:
    """Remove Python-in-Excel Override elements from [Content_Types].xml."""
    # Parse the XML: Excel and LibreOffice write this file as one line, so line filtering deletes it.
    try:
        root, ns_map = parse_and_extract_namespaces(data)
    except ET.ParseError as exc:
        raise ValueError(f"malformed [Content_Types].xml: {exc}") from exc
    changed = False
    for child in list(root):
        if local_name(child.tag) == "Override":
            part_name = child.attrib.get("PartName", "")
            if (
                part_name in ("/xl/pythonScripts.xml", "/xl/calcChain.xml")
                or part_name.startswith("/xl/pythonScripts/")
            ):
                root.remove(child)
                changed = True
    if not changed:
        return data
    return serialize_xml_preserving_namespaces(root, ns_map)


def _strip_rels_python(data: bytes) -> bytes:
    """Remove Python-in-Excel Relationship elements from .rels files."""
    # Parse the XML: a single-line .rels file loses its whole body under line filtering.
    try:
        root, ns_map = parse_and_extract_namespaces(data)
    except ET.ParseError as exc:
        raise ValueError(f"malformed relationship XML (.rels): {exc}") from exc
    changed = False
    for child in list(root):
        if local_name(child.tag) == "Relationship":
            target = child.attrib.get("Target", "").replace("\\", "/").rstrip("/").split("/")[-1]
            rel_type = child.attrib.get("Type", "")
            if (
                rel_type == PYTHON_SCRIPT_REL_TYPE
                or target == "pythonScripts.xml"
                or rel_type.endswith(CALC_CHAIN_REL_TYPE_SUFFIX)
            ):
                root.remove(child)
                changed = True
    if not changed:
        return data
    return serialize_xml_preserving_namespaces(root, ns_map)


def _strip_python_in_excel_parts(out_path: Path) -> None:  # pyright: ignore[reportUnusedFunction]  # tests call _strip_python_in_excel_parts
    """Remove obsolete Python-in-Excel package parts after formula rewrite."""
    drop_prefixes = ("xl/pythonScripts/",)
    drop_exact = {"xl/pythonScripts.xml", "xl/calcChain.xml"}

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



def _a1_row_col(a1: str) -> tuple[int, int] | None:
    """Return (row_1_based, col_1_based) for an A1 reference."""
    row, col = _col_row(a1)
    if row == 0 and col == 0:
        return None
    return row, col


def _ensure_sheet_data(ws_root: ET.Element) -> ET.Element:
    for child in list(ws_root):
        if local_name(child.tag) == "sheetData":
            return child
    tag = f"{{{SSML_NS}}}sheetData" if ws_root.tag.startswith("{") else "sheetData"
    el = ET.SubElement(ws_root, tag)
    return el


class _WorksheetCellIndex:
    """Index of rows and cells in a worksheet for ascending-order lookup and insertion."""

    def __init__(self, ws_root: ET.Element) -> None:
        self.ws_root: ET.Element = ws_root
        self.sheet_data: ET.Element = _ensure_sheet_data(ws_root)
        self.rows: dict[int, ET.Element] = {}
        self.cells_by_row: dict[ET.Element, dict[int, ET.Element]] = {}
        self.max_row: int = 0
        self.max_col_by_row: dict[ET.Element, int] = {}
        self.has_non_row: bool = False
        self.has_non_c_by_row: dict[ET.Element, bool] = {}
        self._build_index()

    def _build_index(self) -> None:
        current_row_num = 0
        self.max_row = 0
        self.max_col_by_row = {}
        self.has_non_row = False
        self.has_non_c_by_row = {}
        for row in list(self.sheet_data):
            if local_name(row.tag) != "row":
                self.has_non_row = True
                continue
            r_attr = row.attrib.get("r")
            if r_attr is not None:
                try:
                    current_row_num = int(r_attr)
                except ValueError:
                    current_row_num += 1
            else:
                current_row_num += 1
            if current_row_num > self.max_row:
                self.max_row = current_row_num
            self.rows[current_row_num] = row
            row_cells: dict[int, ET.Element] = {}
            current_col_num = 0
            max_col = 0
            has_non_c = False
            for c in list(row):
                if local_name(c.tag) != "c":
                    has_non_c = True
                    continue
                c_r = c.attrib.get("r")
                if c_r:
                    parsed = _a1_row_col(c_r)
                    if parsed is not None:
                        current_col_num = parsed[1]
                    else:
                        current_col_num += 1
                else:
                    current_col_num += 1
                if current_col_num > max_col:
                    max_col = current_col_num
                row_cells[current_col_num] = c
            self.cells_by_row[row] = row_cells
            self.max_col_by_row[row] = max_col
            self.has_non_c_by_row[row] = has_non_c

    def ensure_cell(self, a1: str) -> ET.Element:
        parsed = _a1_row_col(a1)
        if parsed is None:
            raise ValueError(f"invalid A1: {a1!r}")
        target_row, target_col = parsed

        # 1. Locate or insert <row> in ascending order
        row_el = self.rows.get(target_row)
        if row_el is None:
            row_tag = f"{{{SSML_NS}}}row" if self.sheet_data.tag.startswith("{") else "row"
            row_el = ET.Element(row_tag, attrib={"r": str(target_row)})

            # Ascending inserts rescanned every sibling, so a new bank column was quadratic
            # (about 2s for 5000 rows); append is valid only when the parent has no extLst
            # or other non-cell/non-row children.
            if not self.has_non_row and target_row > self.max_row:
                self.sheet_data.append(row_el)
                self.max_row = target_row
            else:
                insert_row_idx = len(self.sheet_data)
                current_row_num = 0
                for idx, r in enumerate(list(self.sheet_data)):
                    if local_name(r.tag) != "row":
                        self.has_non_row = True
                        continue
                    r_attr = r.attrib.get("r")
                    if r_attr is not None:
                        try:
                            current_row_num = int(r_attr)
                        except ValueError:
                            current_row_num += 1
                    else:
                        current_row_num += 1
                    if current_row_num > target_row:
                        insert_row_idx = idx
                        break
                self.sheet_data.insert(insert_row_idx, row_el)
                if target_row > self.max_row:
                    self.max_row = target_row
            self.rows[target_row] = row_el
            self.cells_by_row[row_el] = {}
            self.max_col_by_row[row_el] = 0
            self.has_non_c_by_row[row_el] = False

        # 2. Locate or insert <c> in ascending order within row_el
        row_cells = self.cells_by_row[row_el]
        if target_col in row_cells:
            return row_cells[target_col]

        col_str = col_index_to_letters(target_col)
        a1_canonical = f"{col_str}{target_row}"

        c_tag = f"{{{SSML_NS}}}c" if row_el.tag.startswith("{") else "c"
        cell = ET.Element(c_tag, attrib={"r": a1_canonical})

        max_col = self.max_col_by_row.get(row_el, 0)
        has_non_c = self.has_non_c_by_row.get(row_el, False)

        # Ascending inserts rescanned every sibling, so a new bank column was quadratic
        # (about 2s for 5000 rows); append is valid only when the parent has no extLst
        # or other non-cell/non-row children.
        if not has_non_c and target_col > max_col:
            row_el.append(cell)
            self.max_col_by_row[row_el] = target_col
        else:
            insert_col_idx = len(row_el)
            current_col_num = 0
            for idx, c in enumerate(list(row_el)):
                if local_name(c.tag) != "c":
                    self.has_non_c_by_row[row_el] = True
                    insert_col_idx = idx
                    break
                c_r = c.attrib.get("r")
                if c_r:
                    parsed_c = _a1_row_col(c_r)
                    if parsed_c is not None:
                        current_col_num = parsed_c[1]
                    else:
                        current_col_num += 1
                else:
                    current_col_num += 1
                if current_col_num > target_col:
                    insert_col_idx = idx
                    break
            row_el.insert(insert_col_idx, cell)
            if target_col > max_col:
                self.max_col_by_row[row_el] = target_col
        row_cells[target_col] = cell
        return cell


def _ensure_cell(ws_root: ET.Element, a1: str, index: _WorksheetCellIndex | None = None) -> ET.Element:
    """Return ``<c r="A1">``, creating row/cell under sheetData in ascending order when missing."""
    if index is not None:
        return index.ensure_cell(a1)
    return _WorksheetCellIndex(ws_root).ensure_cell(a1)


def _set_cell_xlws_formula(
    ws_root: ET.Element,
    a1: str,
    formula: str,
    array_ref: str = "",
    index: _WorksheetCellIndex | None = None,
) -> None:
    """Set formula on cell, clearing stale si, ca, v, is, and t attributes."""
    cell = _ensure_cell(ws_root, a1, index=index)
    # Drop cached value / type so Excel recalculates from the formula.
    for child in list(cell):
        if local_name(child.tag) in ("v", "is"):
            cell.remove(child)
    if "t" in cell.attrib:
        del cell.attrib["t"]
    f = find_child(cell, "f")
    if f is None:
        f_tag = f"{{{SSML_NS}}}f" if cell.tag.startswith("{") else "f"
        f = ET.Element(f_tag)
        insert_idx = len(cell)
        for idx, child in enumerate(list(cell)):
            if local_name(child.tag) in ("v", "is", "extLst"):
                insert_idx = idx
                break
        cell.insert(insert_idx, f)
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


def _clear_spill_xml(ws_root: ET.Element, anchor: str, array_ref: str, index: _WorksheetCellIndex | None = None) -> None:
    """Clear cached/array result cells in *array_ref* from XML, keeping the anchor."""
    if not array_ref:
        return
    bounds = a1_span_bounds(array_ref)
    if bounds is None:
        return
    (col1, row1), (col2, row2) = bounds
    anchor_parsed = _a1_row_col(anchor)
    anchor_r, anchor_c = anchor_parsed if anchor_parsed is not None else (0, 0)

    if index is not None:
        for r, row_el in index.rows.items():
            if not (row1 <= r <= row2):
                continue
            row_cells = index.cells_by_row.get(row_el)
            if not row_cells:
                continue
            for col, c in row_cells.items():
                if not (col1 <= col <= col2) or (r, col) == (anchor_r, anchor_c):
                    continue
                for child in list(c):
                    if local_name(child.tag) in ("f", "v", "is"):
                        c.remove(child)
                if "t" in c.attrib:
                    del c.attrib["t"]
        return

    for c in find_all(ws_root, "c"):
        c_r = c.attrib.get("r")
        if not c_r:
            continue
        parsed = _a1_row_col(c_r)
        if parsed is None:
            continue
        r, col = parsed
        if col1 <= col <= col2 and row1 <= r <= row2:
            if (r, col) != (anchor_r, anchor_c):
                for child in list(c):
                    if local_name(child.tag) in ("f", "v", "is"):
                        c.remove(child)
                if "t" in c.attrib:
                    del c.attrib["t"]


def _patch_content_types(data: bytes, drop_parts: set[str] | None = None) -> bytes:
    """Ensure pythonScripts.xml Override is present and remove Overrides for drop_parts and calcChain."""
    try:
        root, ns_map = parse_and_extract_namespaces(data)
    except ET.ParseError as exc:
        raise ValueError(f"malformed [Content_Types].xml: {exc}") from exc
    norm_drop = {p.lstrip("/") for p in drop_parts} if drop_parts else set()
    norm_drop.add("xl/calcChain.xml")
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
    return serialize_xml_preserving_namespaces(root, ns_map)


def _is_py_code_bank_sheet(
    zin: zipfile.ZipFile,
    sh_title: str,
    sh_part_name: str,
    defined_names: list[tuple[str, str]],
    shared_strings: list[str],
) -> bool:
    """True if sh_title is a script bank sheet (has defined name or ZZ1 == CODE_SHEET_NOTE)."""
    if any(defined_name_targets_sheet(name, val, sh_title) for name, val in defined_names):
        return True
    try:
        ws_bytes = zin.read(sh_part_name)
        ws_root = ET.fromstring(ws_bytes)
    except Exception:
        return False
    for c in find_all(ws_root, "c"):
        if (c.attrib.get("r") or "").replace("$", "").upper() == CODE_SHEET_NOTE_CELL:
            is_el = find_child(c, "is")
            if is_el is not None:
                txt = "".join(is_el.itertext()).strip()
                if txt == CODE_SHEET_NOTE:
                    return True
            t = (c.attrib.get("t") or "").lower()
            v = find_child(c, "v")
            if v is not None and v.text:
                if t == "s":
                    try:
                        idx = int(v.text)
                        if idx < len(shared_strings) and shared_strings[idx] == CODE_SHEET_NOTE:
                            return True
                    except (ValueError, IndexError):
                        pass
                elif v.text.strip() == CODE_SHEET_NOTE:
                    return True
    return False


_WORKBOOK_AFTER_CALC_PR = (
    "oleSize",
    "customWorkbookViews",
    "pivotCaches",
    "smartTagPr",
    "smartTagTypes",
    "webPublishing",
    "fileRecoverPr",
    "webPublishObjects",
    "extLst",
)
_WORKBOOK_AFTER_DEFINED_NAMES = ("calcPr",) + _WORKBOOK_AFTER_CALC_PR


def _ensure_workbook_full_calc_on_load(wb: ET.Element) -> ET.Element:
    """Ensure workbook has calcPr with fullCalcOnLoad="1" in schema order."""
    calc_pr = find_child(wb, "calcPr")
    if calc_pr is not None:
        calc_pr.attrib["fullCalcOnLoad"] = "1"
        return calc_pr
    tag = f"{{{SSML_NS}}}calcPr" if wb.tag.startswith("{") else "calcPr"
    calc_pr = ET.Element(tag, attrib={"fullCalcOnLoad": "1"})
    insert_idx = len(wb)
    for idx, child in enumerate(list(wb)):
        if local_name(child.tag) in _WORKBOOK_AFTER_CALC_PR:
            insert_idx = idx
            break
    wb.insert(insert_idx, calc_pr)
    return calc_pr


def _drop_py_code_sheets_from_workbook(
    wb_xml: bytes,
    rels_xml: bytes,
    drop_rids: set[str],
    drop_titles: set[str] | None = None,
) -> tuple[bytes, bytes]:
    """Remove ``py_code_*`` sheet entries and definedNames from workbook.xml + workbook rels."""
    wb, wb_ns = parse_and_extract_namespaces(wb_xml)
    drop_titles_set = {t.lower() for t in drop_titles} if drop_titles else set()
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
            if rid in drop_rids or title.lower() in drop_titles_set:
                sheets_el.remove(sh)

    for dn_container in list(wb):
        if local_name(dn_container.tag) != "definedNames":
            continue
        for dn in list(dn_container):
            if local_name(dn.tag) != "definedName":
                continue
            name_attr = dn.attrib.get("name", "")
            val = "".join(dn.itertext()).strip()
            if any(defined_name_targets_sheet(name_attr, val, t) for t in drop_titles_set):
                dn_container.remove(dn)

    _ensure_workbook_full_calc_on_load(wb)

    rels, rels_ns = parse_and_extract_namespaces(rels_xml)

    for rel in list(rels):
        if local_name(rel.tag) != "Relationship":
            continue
        rid = rel.attrib.get("Id") or ""
        rel_type = rel.attrib.get("Type") or ""
        if rid in drop_rids or rel_type.endswith(CALC_CHAIN_REL_TYPE_SUFFIX):
            rels.remove(rel)

    has_py_rel = any(
        local_name(r.tag) == "Relationship"
        and (
            r.attrib.get("Type") == PYTHON_SCRIPT_REL_TYPE
            or r.attrib.get("Target", "").replace("\\", "/").rstrip("/").split("/")[-1] == "pythonScripts.xml"
        )
        for r in rels
    )
    if not has_py_rel:
        rids = [
            int(r.attrib["Id"][3:])
            for r in rels
            if local_name(r.tag) == "Relationship"
            and r.attrib.get("Id", "").startswith("rId")
            and r.attrib["Id"][3:].isdigit()
        ]
        next_rid = f"rId{max(rids, default=0) + 1}"
        tag = f"{{{PACKAGE_REL_NS}}}Relationship" if rels.tag.startswith("{") else "Relationship"
        ET.SubElement(
            rels,
            tag,
            attrib={
                "Id": next_rid,
                "Type": PYTHON_SCRIPT_REL_TYPE,
                "Target": "pythonScripts.xml",
            },
        )

    wb_bytes = serialize_xml_preserving_namespaces(wb, wb_ns)
    rels_bytes = serialize_xml_preserving_namespaces(rels, rels_ns)
    return wb_bytes, rels_bytes


def _require_workbook_sheets(zin: zipfile.ZipFile) -> list[SheetInfo]:
    names = set(zin.namelist())
    if "xl/workbook.xml" not in names:
        raise ValueError("package missing required part: xl/workbook.xml")
    if "xl/_rels/workbook.xml.rels" not in names:
        raise ValueError("package missing required part: xl/_rels/workbook.xml.rels")
    return workbook_sheets(zin)


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

    errors: list[str] = []

    with xml_namespace_lock(), zipfile.ZipFile(source_xlsx, "r") as zin:
        sheets = _require_workbook_sheets(zin)
        title_to_part = {s.title: s.part_name for s in sheets if s.part_name}
        title_to_part_l = {s.title.lower(): s.part_name for s in sheets if s.part_name}

        by_part: dict[str, list[ConvertedCell]] = {}
        seen_part_cells: set[tuple[str, str]] = set()
        for cell in report.cells:
            if not cell.converted or cell.script_index < 0:
                continue
            part = title_to_part.get(cell.sheet) or title_to_part_l.get(cell.sheet.lower(), "")
            if not part:
                errors.append(f"unmapped sheet {cell.sheet!r}")
                continue
            norm_a1 = cell.cell.replace("$", "").upper()
            if (part, norm_a1) in seen_part_cells:
                raise ValueError(f"duplicate cell {norm_a1} for part {part!r} ({cell.sheet!r})")
            seen_part_cells.add((part, norm_a1))
            by_part.setdefault(part, []).append(cell)

        # 1. Scan worksheet formulas for _xlws.PY not in report's write set
        report_write_set: set[tuple[str, str]] = {
            (c.sheet.lower(), c.cell.replace("$", "").upper())
            for c in report.cells
            if c.converted and c.script_index >= 0
        }
        unwritten_cells: list[str] = []
        for sh in sheets:
            if not sh.part_name or sh.title.startswith(CODE_SHEET_PREFIX):
                continue
            try:
                ws_bytes = zin.read(sh.part_name)
            except KeyError:
                continue
            if b"_xlws.PY" not in ws_bytes and b"_xlws.py" not in ws_bytes:
                continue
            ws_root = ET.fromstring(ws_bytes)
            shared_masters = _shared_formula_map(ws_root)
            for c_el in find_all(ws_root, "c"):
                a1 = c_el.attrib.get("r") or ""
                f_el = find_child(c_el, "f")
                if f_el is None or not a1:
                    continue
                body = "".join(f_el.itertext()).strip()
                if (f_el.attrib.get("t") or "") == "shared" and not body:
                    si = f_el.attrib.get("si") or ""
                    if si in shared_masters:
                        body = shared_masters[si][0]
                if "_xlws.py" in body.lower():
                    coord = a1.replace("$", "").upper()
                    if (sh.title.lower(), coord) not in report_write_set:
                        unwritten_cells.append(f"{sh.title}!{a1}")
        if unwritten_cells:
            raise ValueError(
                "Found _xlws.PY cells not in conversion report write set:\n"
                + "\n".join(sorted(unwritten_cells))
            )

        # 2. Bank sheet discovery via defined names / ZZ1 note
        wb_root = ET.fromstring(zin.read("xl/workbook.xml"))
        defined_names: list[tuple[str, str]] = []
        for dn in find_all(wb_root, "definedName"):
            name = dn.attrib.get("name") or ""
            val = "".join(dn.itertext()).strip()
            defined_names.append((name, val))
        shared_strings = _shared_strings(zin)

        code_sheets = [s for s in sheets if s.title.startswith(CODE_SHEET_PREFIX)]
        drop_parts: set[str] = set()
        drop_rids: set[str] = set()
        drop_titles: set[str] = set()
        for s in code_sheets:
            if not s.part_name:
                continue
            if _is_py_code_bank_sheet(zin, s.title, s.part_name, defined_names, shared_strings):
                drop_parts.add(s.part_name)
                drop_rids.add(s.rel_id)
                drop_titles.add(s.title)

        drop_sheet_rels = {_sheet_rels_path(p) for p in drop_parts}

        # 3. Pre-parse and patch worksheet roots
        patched: dict[str, ET.Element] = {}
        original_namespaces: dict[str, dict[str, str]] = {}
        for part, cells in by_part.items():
            cells.sort(key=lambda c: _a1_row_col(c.cell) or (0, 0))
            root = patched.get(part)
            if root is None:
                try:
                    root, ns_map = parse_and_extract_namespaces(zin.read(part))
                    original_namespaces[part] = ns_map
                except KeyError:
                    errors.append(f"missing worksheet part {part!r}")
                    continue

            ws_index = _WorksheetCellIndex(root)
            sheet_write_set = {cell.cell.replace("$", "").upper() for cell in cells}
            sheet_title = cells[0].sheet

            _unshare_groups_touching(root, sheet_title, sheet_write_set)

            for cell in cells:
                try:
                    if cell.array_ref:
                        _clear_spill_xml(root, cell.cell, cell.array_ref, index=ws_index)
                    formula = xlws_py_formula(cell.script_index, cell.return_type, deps_for_xlws_export(cell))
                    _set_cell_xlws_formula(root, cell.cell, formula, array_ref=cell.array_ref, index=ws_index)
                except Exception as exc:
                    errors.append(f"{cell.sheet}!{cell.cell}: {exc}")
            patched[part] = root

        if errors:
            raise ValueError("write_excel_python_xlsx failed:\n" + "\n".join(errors))

        scripts_bytes = python_scripts_xml(scripts)
        wb_xml = zin.read("xl/workbook.xml")
        rels_xml = zin.read("xl/_rels/workbook.xml.rels")
        wb_xml, rels_xml = _drop_py_code_sheets_from_workbook(wb_xml, rels_xml, drop_rids, drop_titles=drop_titles)

    def _transform(zin: zipfile.ZipFile, zout: zipfile.ZipFile) -> None:
        written_scripts = False
        for info in zin.infolist():
            name = info.filename
            if name in drop_parts or name == "xl/calcChain.xml":
                continue
            if name in drop_sheet_rels:
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
                data = serialize_xml_preserving_namespaces(patched[name], original_namespaces.get(name))
                zout.writestr(info, data)
                continue
            zout.writestr(info, zin.read(name))
        if not written_scripts:
            zout.writestr("xl/pythonScripts.xml", scripts_bytes)

    rewrite_zip(source_xlsx, out_path, _transform, temp_suffix=".tmpexcelpy")


def write_dag_formulas_xlsx(
    source_xlsx: str | Path,
    report: ConversionReport,
    out_path: str | Path,
    *,
    strip_python_parts: bool = True,
) -> None:
    """Copy *source_xlsx* and replace successfully converted PY cells with DAG formulas.

    - Parks rewritten Python on visible ``py_code_<Sheet>`` sheets at the **same A1**
      as each caller when ``len(code) > 1000``; shorter scripts stay inline in ``=PY("…")``.
    - OOXML formulas use **comma** separators (not Calc ``;``).
    - Clears the source array/spill ``ref`` range (except the anchor) so old
      cached results do not block the new spill.
    - Fails closed on unmapped sheet titles (no silent first-sheet fallback).
    - Optionally strips ``xl/pythonScripts.xml`` and related package parts.
    """
    source_xlsx = Path(source_xlsx)
    out_path = Path(out_path)
    if report.direction != "dag":
        raise ValueError("write_dag_formulas_xlsx requires report.direction == 'dag'")

    bank, bank_warnings = collect_script_bank(report)
    for w in bank_warnings:
        log.warning("excel_py convert: %s", w)
    for w in report_safety_warnings(report):
        log.warning("excel_py convert: %s", w)

    errors: list[str] = []

    with xml_namespace_lock(), zipfile.ZipFile(source_xlsx, "r") as zin:
        sheets = _require_workbook_sheets(zin)
        title_to_info = {s.title: s for s in sheets if s.part_name}
        title_to_info_l = {s.title.lower(): s for s in sheets if s.part_name}

        by_part: dict[str, list[ConvertedCell]] = {}
        for cell in report.cells:
            if not cell.converted or not cell.converted_code:
                continue
            sh_info = title_to_info.get(cell.sheet) or title_to_info_l.get(cell.sheet.lower())
            if sh_info is None:
                errors.append(f"unmapped sheet {cell.sheet!r} for cell {cell.cell}")
                continue
            by_part.setdefault(sh_info.part_name, []).append(cell)

        wb_root, wb_ns = parse_and_extract_namespaces(zin.read("xl/workbook.xml"))
        rels_root, rels_ns = parse_and_extract_namespaces(zin.read("xl/_rels/workbook.xml.rels"))

        defined_names: list[tuple[str, str]] = []
        for dn in find_all(wb_root, "definedName"):
            defined_names.append((dn.attrib.get("name") or "", "".join(dn.itertext()).strip()))
        shared_strings = _shared_strings(zin)

        # Collision check: existing sheet with bank prefix must be a verified script bank sheet.
        for code_sheet in bank:
            existing_sh = title_to_info.get(code_sheet) or title_to_info_l.get(code_sheet.lower())
            if existing_sh is not None:
                if not _is_py_code_bank_sheet(zin, existing_sh.title, existing_sh.part_name, defined_names, shared_strings):
                    raise ValueError(f"Bank sheet collision: existing sheet {code_sheet!r} is not a script bank sheet")

        patched: dict[str, ET.Element] = {}
        original_namespaces: dict[str, dict[str, str]] = {}

        for part, cells in by_part.items():
            cells.sort(key=lambda c: _a1_row_col(c.cell) or (0, 0))
            try:
                root, ns_map = parse_and_extract_namespaces(zin.read(part))
            except KeyError:
                errors.append(f"missing worksheet part {part!r}")
                continue
            original_namespaces[part] = ns_map
            ws_index = _WorksheetCellIndex(root)
            sheet_title = cells[0].sheet
            sheet_write_set = {c.cell.replace("$", "").upper() for c in cells}

            try:
                _unshare_groups_touching(root, sheet_title, sheet_write_set)
            except Exception as exc:
                errors.append(f"{sheet_title}: {exc}")

            for cell in cells:
                try:
                    if cell.array_ref:
                        _clear_spill_xml(root, cell.cell, cell.array_ref, index=ws_index)
                    formula = _xlsx_formula_for_cell(cell)
                    if _ILLEGAL_XML_CHARS_RE.search(formula):
                        raise ValueError(f"illegal XML character in formula: {formula!r}")
                    _set_cell_xlws_formula(root, cell.cell, formula, array_ref="", index=ws_index)
                except Exception as exc:
                    errors.append(f"{cell.sheet}!{cell.cell}: {exc}")

            patched[part] = root

        new_content_types_overrides: list[str] = []
        new_sheet_parts: dict[str, bytes] = {}

        sheets_el = find_child(wb_root, "sheets")
        existing_sheet_ids: set[int] = set()
        if sheets_el is not None:
            for sh in sheets_el:
                s_id = sh.attrib.get("sheetId", "")
                if s_id.isdigit():
                    existing_sheet_ids.add(int(s_id))
        next_sheet_id = max(existing_sheet_ids, default=0) + 1

        existing_rids: set[int] = set()
        for rel in rels_root:
            if local_name(rel.tag) == "Relationship":
                rid = rel.attrib.get("Id", "")
                if rid.startswith("rId") and rid[3:].isdigit():
                    existing_rids.add(int(rid[3:]))
        next_rid_num = max(existing_rids, default=0) + 1

        all_names = set(zin.namelist())
        next_sheet_num = 1

        for code_sheet, sheet_bank in sorted(bank.items()):
            if not sheet_bank:
                continue
            existing_sh = title_to_info.get(code_sheet) or title_to_info_l.get(code_sheet.lower())
            if existing_sh is not None:
                part_name = existing_sh.part_name
                if part_name in patched:
                    bank_ws_root = patched[part_name]
                    bank_ws_ns = original_namespaces.get(part_name)
                else:
                    bank_ws_root, bank_ws_ns = parse_and_extract_namespaces(zin.read(part_name))
                    patched[part_name] = bank_ws_root
                    original_namespaces[part_name] = bank_ws_ns
                bank_index = _WorksheetCellIndex(bank_ws_root)
            else:
                while f"xl/worksheets/sheet{next_sheet_num}.xml" in all_names or f"xl/worksheets/sheet{next_sheet_num}.xml" in new_sheet_parts:
                    next_sheet_num += 1
                part_name = f"xl/worksheets/sheet{next_sheet_num}.xml"
                next_sheet_num += 1

                bank_ws_root = ET.Element(f"{{{SSML_NS}}}worksheet")
                ET.SubElement(bank_ws_root, f"{{{SSML_NS}}}sheetData")
                bank_ws_ns = {"": SSML_NS, "r": OFFICE_DOC_REL_NS}
                bank_index = _WorksheetCellIndex(bank_ws_root)

                cur_sheet_id = next_sheet_id
                next_sheet_id += 1
                cur_rid = f"rId{next_rid_num}"
                next_rid_num += 1

                if sheets_el is None:
                    sheets_tag = f"{{{SSML_NS}}}sheets" if wb_root.tag.startswith("{") else "sheets"
                    sheets_el = ET.SubElement(wb_root, sheets_tag)
                sheet_tag = f"{{{SSML_NS}}}sheet" if sheets_el.tag.startswith("{") else "sheet"
                r_id_attr = f"{{{OFFICE_DOC_REL_NS}}}id" if wb_root.tag.startswith("{") else "r:id"
                sheets_el.append(ET.Element(sheet_tag, attrib={"name": code_sheet, "sheetId": str(cur_sheet_id), r_id_attr: cur_rid}))

                rel_tag = f"{{{PACKAGE_REL_NS}}}Relationship" if rels_root.tag.startswith("{") else "Relationship"
                rels_root.append(ET.Element(rel_tag, attrib={
                    "Id": cur_rid,
                    "Type": f"{OFFICE_DOC_REL_NS}/worksheet",
                    "Target": f"worksheets/{Path(part_name).name}",
                }))
                new_content_types_overrides.append(f"/{part_name}")

            # Insert definedName in workbook schema order (before calcPr/etc.).
            def_name = bank_defined_name_for(code_sheet)
            dn_container = find_child(wb_root, "definedNames")
            if dn_container is None:
                dn_tag = f"{{{SSML_NS}}}definedNames" if wb_root.tag.startswith("{") else "definedNames"
                dn_container = ET.Element(dn_tag)
                insert_idx = len(wb_root)
                for idx, child in enumerate(list(wb_root)):
                    if local_name(child.tag) in _WORKBOOK_AFTER_DEFINED_NAMES:
                        insert_idx = idx
                        break
                wb_root.insert(insert_idx, dn_container)
            has_dn = any(
                local_name(dn.tag) == "definedName" and (dn.attrib.get("name") or "").lower() == def_name.lower()
                for dn in dn_container
            )
            if not has_dn:
                dn_tag = f"{{{SSML_NS}}}definedName" if dn_container.tag.startswith("{") else "definedName"
                dn_el = ET.SubElement(dn_container, dn_tag, attrib={"name": def_name})
                dn_el.text = f"'{code_sheet}'!$A$1"

            bank_entries = dict(sheet_bank)
            if CODE_SHEET_NOTE_CELL not in bank_entries:
                bank_entries[CODE_SHEET_NOTE_CELL] = CODE_SHEET_NOTE

            sorted_bank_entries = sorted(bank_entries.items(), key=lambda item: _a1_row_col(item[0]) or (0, 0))
            for a1, text in sorted_bank_entries:
                if _ILLEGAL_XML_CHARS_RE.search(text):
                    errors.append(f"{code_sheet}!{a1}: string contains illegal XML character")
                    continue
                c_el = bank_index.ensure_cell(a1)
                c_el.attrib["t"] = "inlineStr"
                for child in list(c_el):
                    c_el.remove(child)
                is_tag = f"{{{SSML_NS}}}is" if c_el.tag.startswith("{") else "is"
                t_tag = f"{{{SSML_NS}}}t" if c_el.tag.startswith("{") else "t"
                is_el = ET.SubElement(c_el, is_tag)
                t_el = ET.SubElement(is_el, t_tag)
                t_el.text = text
                if text.startswith((" ", "\t", "\n", "\r")) or text.endswith((" ", "\t", "\n", "\r")):
                    t_el.set("{http://www.w3.org/XML/1998/namespace}space", "preserve")

            if existing_sh is None:
                new_sheet_parts[part_name] = serialize_xml_preserving_namespaces(bank_ws_root, bank_ws_ns)

        _ensure_workbook_full_calc_on_load(wb_root)

        if errors:
            raise ValueError("write_dag_formulas_xlsx failed:\n" + "\n".join(errors))

        # Drop calcChain relationship so Excel does not attempt stale chain evaluation.
        for rel in list(rels_root):
            if local_name(rel.tag) != "Relationship":
                continue
            rel_type = rel.attrib.get("Type") or ""
            target = rel.attrib.get("Target", "").replace("\\", "/").rstrip("/").split("/")[-1]
            if rel_type.endswith(CALC_CHAIN_REL_TYPE_SUFFIX) or target == "calcChain.xml":
                rels_root.remove(rel)

        wb_bytes = serialize_xml_preserving_namespaces(wb_root, wb_ns)
        rels_bytes = serialize_xml_preserving_namespaces(rels_root, rels_ns)
        if strip_python_parts:
            rels_bytes = _strip_rels_python(rels_bytes)

    drop_prefixes = ("xl/pythonScripts/",) if strip_python_parts else ()
    drop_exact = {"xl/calcChain.xml"}
    if strip_python_parts:
        drop_exact.add("xl/pythonScripts.xml")

    def _transform(zin: zipfile.ZipFile, zout: zipfile.ZipFile) -> None:
        for info in zin.infolist():
            name = info.filename
            if name in drop_exact or any(name.startswith(p) for p in drop_prefixes):
                continue
            if name == "[Content_Types].xml":
                data = _patch_content_types_dag(zin.read(name), new_content_types_overrides, strip_python=strip_python_parts)
                zout.writestr(info, data)
                continue
            if name == "xl/workbook.xml":
                zout.writestr(info, wb_bytes)
                continue
            if name == "xl/_rels/workbook.xml.rels":
                zout.writestr(info, rels_bytes)
                continue
            if name in patched:
                data = serialize_xml_preserving_namespaces(patched[name], original_namespaces.get(name))
                zout.writestr(info, data)
                continue
            data = zin.read(name)
            if strip_python_parts and name.endswith(".rels"):
                data = _strip_rels_python(data)
            zout.writestr(info, data)

        for part_name, part_data in new_sheet_parts.items():
            zout.writestr(part_name, part_data)

    rewrite_zip(source_xlsx, out_path, _transform, temp_suffix=".tmpdagpy")
