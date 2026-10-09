# SPDX-License-Identifier: GPL-3.0-or-later
"""Parse Excel OOXML for Python scripts, PY formulas, tables, and array ranges.

Uses namespace-aware ElementTree (not regex scans) so prefixed namespaces, shared
formulas, and XML entities are handled correctly.
"""

from __future__ import annotations

import json
import re
import zipfile
from pathlib import Path
from typing import Any
from xml.etree import ElementTree as ET  # nosemgrep: use-defused-xml  # local .xlsx ZIP parts; not network XML (Bandit B314)

from plugin.calc.excel_py_convert.models import ExcelPyCell, ExcelWorkbookModel, SheetInfo
from plugin.calc.excel_py_convert.ooxml_util import (
    find_all,
    find_child,
    local_name,
    parse_rels,
    parse_rels_with_types,
    resolve_rel_target,
    workbook_sheets,
)

# Backwards-compatible aliases
_local = local_name
_findall = find_all
_find_child = find_child
_workbook_sheets = workbook_sheets
_parse_rels = parse_rels
_parse_rels_with_types = parse_rels_with_types
_resolve_rel_target = resolve_rel_target

_RE_A1_CELL = re.compile(r"^\$?([A-Za-z]+)\$?(\d+)$")
_RE_XLWS_PY = re.compile(r"(?:_xlfn\.)?_xlws\.PY\s*\(\s*(\d+)\s*,\s*(\d+)(.*)\)$", re.IGNORECASE | re.DOTALL)


def _col_row(a1: str) -> tuple[int, int]:
    m = _RE_A1_CELL.match(a1.replace("$", "").strip())
    if not m:
        return 0, 0
    col_s, row_s = m.group(1).upper(), m.group(2)
    col = 0
    for ch in col_s:
        col = col * 26 + (ord(ch) - 64)
    return int(row_s), col


def split_top_level_args(tail: str) -> list[str]:
    """Split comma-separated formula args with quote/paren awareness."""
    s = (tail or "").strip()
    if s.startswith(","):
        s = s[1:].strip()
    if not s:
        return []
    args: list[str] = []
    buf: list[str] = []
    depth = 0
    in_sq = False
    in_dq = False
    i = 0
    while i < len(s):
        ch = s[i]
        if in_sq:
            buf.append(ch)
            if ch == "'" and i + 1 < len(s) and s[i + 1] == "'":
                buf.append(s[i + 1])
                i += 2
                continue
            if ch == "'":
                in_sq = False
            i += 1
            continue
        if in_dq:
            buf.append(ch)
            if ch == '"':
                in_dq = False
            i += 1
            continue
        if ch == "'":
            in_sq = True
            buf.append(ch)
        elif ch == '"':
            in_dq = True
            buf.append(ch)
        elif ch == "(":
            depth += 1
            buf.append(ch)
        elif ch == ")":
            depth = max(0, depth - 1)
            buf.append(ch)
        elif ch == "," and depth == 0:
            args.append("".join(buf).strip())
            buf = []
        else:
            buf.append(ch)
        i += 1
    if buf:
        args.append("".join(buf).strip())
    return [a for a in args if a]


def parse_xlws_py_formula(formula: str) -> tuple[int, int, list[str]] | None:
    """Parse ``_xlfn._xlws.PY(scriptIndex, returnType, …deps)``."""
    text = (formula or "").strip()
    if text.startswith("="):
        text = text[1:].strip()
    m = _RE_XLWS_PY.match(text)
    if not m:
        return None
    script_index = int(m.group(1))
    return_type = int(m.group(2))
    deps = split_top_level_args(m.group(3) or "")
    return script_index, return_type, deps


def _rel_type_is_table(rel_type: str) -> bool:
    return rel_type.rstrip("/").endswith("table")


def _parse_python_scripts(zf: zipfile.ZipFile) -> list[str]:
    """Parse ``xl/pythonScripts.xml``.

    Map each script to its index (the explicit attribute, or document
    order). Dropping indexed scripts whenever any sequential script is
    present throws away the indexed bank.
    """
    try:
        raw = zf.read("xl/pythonScripts.xml")
    except KeyError:
        return []
    root = ET.fromstring(raw)
    script_els = [el for el in list(root) if local_name(el.tag) == "pythonScript"]
    if not script_els:
        script_els = [el for el in root.iter() if local_name(el.tag) == "pythonScript"]

    scripts_by_idx: dict[int, str] = {}
    for order, el in enumerate(script_els):
        code_el = find_child(el, "code")
        text = "".join(code_el.itertext()) if code_el is not None else "".join(el.itertext())
        idx_s = el.attrib.get("index") or el.attrib.get("scriptIndex") or ""
        idx = order
        if idx_s != "":
            try:
                idx = int(idx_s)
            except ValueError:
                pass
        scripts_by_idx[idx] = text

    if not scripts_by_idx:
        return []
    max_i = max(scripts_by_idx.keys())
    out = [""] * (max_i + 1)
    for i, body in scripts_by_idx.items():
        out[i] = body
    return out


def _sheet_rels_path(part_name: str) -> str:
    p = Path(part_name)
    return str(p.parent / "_rels" / f"{p.name}.rels").replace("\\", "/")


def _qualify_sheet_ref(sheet_title: str, ref: str) -> str:
    """Return Sheet!A1:B2 with quoting when the sheet name needs it."""
    ref = ref.replace("$", "")
    if "!" in ref:
        return ref
    if re.search(r"[^\w]", sheet_title) or sheet_title[:1].isdigit():
        q = "'" + sheet_title.replace("'", "''") + "'"
    else:
        q = sheet_title
    return f"{q}!{ref}"


def _parse_table_ref(zf: zipfile.ZipFile, table_part: str, sheet_title: str) -> tuple[str, str] | None:
    try:
        root = ET.fromstring(zf.read(table_part))
    except KeyError:
        return None
    name = root.attrib.get("displayName") or root.attrib.get("name") or ""
    ref = root.attrib.get("ref") or ""
    if not name or not ref:
        return None
    return name, _qualify_sheet_ref(sheet_title, ref)


def _collect_array_refs(ws_root: ET.Element, sheet_title: str) -> dict[str, str]:
    """Map Sheet!Anchor → full array ref range from worksheet formula/@ref.

    Only a formula with ``t="array"`` is an array ref. A shared-formula
    master (``<f t="shared" ref=... si=...>``) also has ``ref``, and
    treating it as a spill range clears those cells on export.
    """
    out: dict[str, str] = {}
    for c in find_all(ws_root, "c"):
        cell_ref = c.attrib.get("r") or ""
        f = find_child(c, "f")
        if f is None or not cell_ref:
            continue
        if (f.attrib.get("t") or "") != "array":
            continue
        arr = (f.attrib.get("ref") or "").strip().replace("$", "")
        if not arr:
            continue
        top_left = arr.split(":", 1)[0]
        out[_qualify_sheet_ref(sheet_title, top_left)] = arr if "!" in arr else _qualify_sheet_ref(sheet_title, arr)
        # Bare keys for fixtures / same-sheet lookups (first wins).
        out.setdefault(top_left, arr)
        out.setdefault(cell_ref.replace("$", ""), arr)
        out[_qualify_sheet_ref(sheet_title, cell_ref)] = out[_qualify_sheet_ref(sheet_title, top_left)]
    return out


def _shared_formula_map(ws_root: ET.Element) -> dict[str, str]:
    """Resolve shared formula masters (si → formula text)."""
    masters: dict[str, str] = {}
    for c in find_all(ws_root, "c"):
        f = find_child(c, "f")
        if f is None:
            continue
        if (f.attrib.get("t") or "") != "shared":
            continue
        si = f.attrib.get("si")
        body = "".join(f.itertext()).strip()
        if si is not None and body:
            # ElementTree already decodes entities. A second _unescape_xml
            # corrupts literals like &lt; or &amp;. Use body as decoded.
            masters[si] = body
    return masters


def _iter_py_cells(ws_root: ET.Element, sheet_title: str) -> list[ExcelPyCell]:
    masters = _shared_formula_map(ws_root)
    cells: list[ExcelPyCell] = []
    for c in find_all(ws_root, "c"):
        a1 = c.attrib.get("r") or ""
        f = find_child(c, "f")
        if f is None or not a1:
            continue
        body = "".join(f.itertext()).strip()
        if (f.attrib.get("t") or "") == "shared" and not body:
            si = f.attrib.get("si")
            body = masters.get(si or "", "")
        if not body:
            continue
        # ElementTree already decodes entities. A second _unescape_xml
        # corrupts literals like &lt; or &amp;. Use body as decoded.
        formula = body
        if "_xlws.py" not in formula.lower():
            continue
        parsed = parse_xlws_py_formula(formula)
        if parsed is None:
            continue
        script_index, return_type, deps = parsed
        row, col = _col_row(a1)
        # Only ``t="array"`` gets an array_ref. A shared-formula master
        # (``<f t="shared" ref="A1:A10">``) also has ref, and treating it
        # as a spill range clears those cells on export.
        array_ref = (f.attrib.get("ref") or "").replace("$", "") if (f.attrib.get("t") or "") == "array" else ""
        cells.append(ExcelPyCell(sheet=sheet_title, cell=a1, script_index=script_index, return_type=return_type, deps=deps, formula_raw=formula if formula.startswith("=") else f"={formula}", array_ref=array_ref, row=row, col=col))
    return cells


def parse_excel_xlsx(path: str | Path) -> ExcelWorkbookModel:
    """Parse an ``.xlsx`` into scripts, PY cells, sheet map, tables, array anchors."""
    path = Path(path)
    with zipfile.ZipFile(path, "r") as zf:
        sheets = workbook_sheets(zf)
        scripts = _parse_python_scripts(zf)
        tables: dict[str, str] = {}
        anchors: dict[str, str] = {}
        cells: list[ExcelPyCell] = []
        for sh in sheets:
            if not sh.part_name:
                continue
            try:
                ws_root = ET.fromstring(zf.read(sh.part_name))
            except KeyError:
                continue
            cells.extend(_iter_py_cells(ws_root, sh.title))
            # Sheet-qualified keys (they contain "!") update in place. Bare
            # keys such as "A1" are first-wins via setdefault. dict.update
            # lets a later sheet overwrite an earlier sheet's bare key.
            for k, v in _collect_array_refs(ws_root, sh.title).items():
                if "!" in k:
                    anchors[k] = v
                else:
                    anchors.setdefault(k, v)
            for _rid, rtype, target in parse_rels_with_types(zf, _sheet_rels_path(sh.part_name)):
                if not _rel_type_is_table(rtype) and "tables/table" not in target:
                    continue
                parsed = _parse_table_ref(zf, target, sh.title)
                if parsed:
                    name, qref = parsed
                    tables[name] = qref
        for c in cells:
            if not c.row or not c.col:
                c.row, c.col = _col_row(c.cell)
        return ExcelWorkbookModel(scripts=scripts, cells=cells, sheets=sheets, tables=tables, anchor_snapshots=anchors, source_path=str(path))


def has_excel_python_xlsx(path: str | Path) -> bool:
    """True when *path* is an ``.xlsx`` with ``pythonScripts`` and/or ``_xlws.PY`` cells.

    Peeks the ZIP on disk (stock Calc import may have dropped these parts from the
    in-memory model). Safe to call on any path; returns False on I/O/parse errors.
    """
    path = Path(path)
    if path.suffix.lower() != ".xlsx" or not path.is_file():
        return False
    try:
        with zipfile.ZipFile(path, "r") as zf:
            names = zf.namelist()
            if any(n == "xl/pythonScripts.xml" or n.startswith("xl/pythonScripts/") for n in names):
                return True
            for name in names:
                if not name.startswith("xl/worksheets/") or not name.endswith(".xml"):
                    continue
                data = zf.read(name)
                if b"_xlws.PY" in data or b"_xlws.py" in data:
                    return True
    except Exception:
        return False
    return False


def load_excel_model(path: str | Path, *, prefer_openpyxl_anchors: bool = True) -> ExcelWorkbookModel:
    """Load from ``.xlsx`` or a JSON fixture matching ``ExcelWorkbookModel.to_dict()``.

    prefer_openpyxl_anchors is retained for backwards compatibility; openpyxl anchor enrichment
    is dropped now that _collect_array_refs accurately handles t="array" formulas.
    """
    path = Path(path)
    if path.suffix.lower() == ".json":
        data = json.loads(path.read_text(encoding="utf-8"))
        model = ExcelWorkbookModel.from_dict(data)
        model.source_path = model.source_path or str(path)
        # Fixtures often use sheet1/sheet2 — synthesize sheet order if missing.
        if not model.sheets:
            titles: list[str] = []
            for c in model.cells:
                if c.sheet not in titles:
                    titles.append(c.sheet)
            model.sheets = [SheetInfo(title=t, order=i, part_name="") for i, t in enumerate(titles)]
        for c in model.cells:
            if not c.row or not c.col:
                c.row, c.col = _col_row(c.cell)
        return model
    return parse_excel_xlsx(path)


def dump_model_json(model: ExcelWorkbookModel) -> dict[str, Any]:
    return model.to_dict()
