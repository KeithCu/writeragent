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
from xml.etree import ElementTree as ET  # nosemgrep: use-defused-xml  # local .xlsx ZIP parts; not network XML (Bandit B314)

from plugin.calc.excel_py_convert.models import ExcelPyCell, ExcelWorkbookModel, SheetInfo
from plugin.calc.excel_py_convert.ooxml_util import (
    find_all,
    find_child,
    local_name,
    parse_rels_with_types,
    workbook_sheets,
)

_RE_A1_CELL = re.compile(r"^\$?([A-Za-z]+)\$?(\d+)$")
_RE_XLWS_PY = re.compile(r"(?:_xlfn\.)?_xlws\.PY\s*\(\s*(\d+)\s*,\s*(\d+)(.*)\)$", re.IGNORECASE | re.DOTALL)


def letters_to_col_index(col: str) -> int:
    """1-based. ``A`` → 1. Empty → 0."""
    n = 0
    for ch in (col or "").upper():
        n = n * 26 + (ord(ch) - 64)
    return n


def col_index_to_letters(col: int) -> str:
    """1-based. ``1`` → ``A``. Zero or negative → ``""``."""
    s = ""
    while col > 0:
        col, rem = divmod(col - 1, 26)
        s = chr(65 + rem) + s
    return s


def _col_row(a1: str) -> tuple[int, int]:
    m = _RE_A1_CELL.match(a1.replace("$", "").strip())
    if not m:
        return 0, 0
    col = letters_to_col_index(m.group(1))
    return int(m.group(2)), col


def split_top_level_args(tail: str) -> list[str]:
    """Split comma-separated formula args with quote/paren awareness."""
    s = (tail or "").strip()
    if s.startswith(","):
        s = s[1:].strip()
    if not s:
        return []
    args: list[str] = []
    buf: list[str] = []
    paren_depth = 0
    bracket_depth = 0
    brace_depth = 0
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
            paren_depth += 1
            buf.append(ch)
        elif ch == ")":
            paren_depth = max(0, paren_depth - 1)
            buf.append(ch)
        elif ch == "[":
            bracket_depth += 1
            buf.append(ch)
        elif ch == "]":
            bracket_depth = max(0, bracket_depth - 1)
            buf.append(ch)
        elif ch == "{":
            brace_depth += 1
            buf.append(ch)
        elif ch == "}":
            brace_depth = max(0, brace_depth - 1)
            buf.append(ch)
        elif ch == "," and paren_depth == 0 and bracket_depth == 0 and brace_depth == 0:
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


_RE_COORD = re.compile(r"^(\$?)([A-Za-z]+)(\$?)(\d+)$")
_RE_ANCHORARRAY = re.compile(r"^((?:_xlfn\.)?ANCHORARRAY\s*\()(.*)(\))$", re.IGNORECASE)
_RE_DEFINED_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_.]*$")


def _shift_single_coord(coord: str, delta_row: int, delta_col: int) -> str | None:
    m = _RE_COORD.match(coord.strip())
    if not m:
        return None
    col_locked = bool(m.group(1))
    col_str = m.group(2)
    row_locked = bool(m.group(3))
    row_int = int(m.group(4))

    r, c = _col_row(f"{col_str}{row_int}")
    new_c = c if col_locked else c + delta_col
    new_r = r if row_locked else r + delta_row
    if new_c < 1 or new_r < 1 or new_c > 16384 or new_r > 1048576:
        return None
    new_col_str = col_index_to_letters(new_c)
    col_prefix = "$" if col_locked else ""
    row_prefix = "$" if row_locked else ""
    return f"{col_prefix}{new_col_str}{row_prefix}{new_r}"


def _shift_cell_or_range_ref(ref: str, delta_row: int, delta_col: int) -> str | None:
    sheet_prefix = ""
    target = ref.strip()
    if "!" in target:
        sheet_part, sep, target = target.rpartition("!")
        sheet_prefix = f"{sheet_part}{sep}"

    if ":" in target:
        c1, _sep, c2 = target.partition(":")
        s1 = _shift_single_coord(c1, delta_row, delta_col)
        s2 = _shift_single_coord(c2, delta_row, delta_col)
        if s1 is None or s2 is None:
            return None
        return f"{sheet_prefix}{s1}:{s2}"
    s = _shift_single_coord(target, delta_row, delta_col)
    if s is None:
        return None
    return f"{sheet_prefix}{s}"


def shift_xlws_py_dep(dep: str, delta_row: int, delta_col: int) -> str | None:
    """Shift a single dependency argument by row and col delta.

    Returns the shifted dep string, or None if unrecognized or unshiftable.
    """
    if delta_row == 0 and delta_col == 0:
        return dep

    # ANCHORARRAY / _xlfn.ANCHORARRAY
    m_arr = _RE_ANCHORARRAY.match(dep)
    if m_arr:
        inner = m_arr.group(2).strip()
        shifted_inner = _shift_cell_or_range_ref(inner, delta_row, delta_col)
        if shifted_inner is None:
            return None
        return f"{m_arr.group(1)}{shifted_inner}{m_arr.group(3)}"

    # Structured ref (e.g. Table1[Col] or [#This Row])
    if dep.endswith("]") and "[" in dep:
        return dep

    # Defined name (row-invariant identifier not matching A1 cell pattern)
    if _RE_DEFINED_NAME.match(dep) and not _RE_A1_CELL.match(dep.replace("$", "")):
        return dep

    # A1 or A1:B2 (with optional sheet prefix)
    return _shift_cell_or_range_ref(dep, delta_row, delta_col)


def shift_xlws_py_formula(
    formula: str,
    delta_row: int,
    delta_col: int,
) -> tuple[str, list[str]] | None:
    """Shift _xlws.PY dependency arguments by (delta_row, delta_col).

    Returns (shifted_formula, shifted_deps) or None if any dep cannot be shifted.
    """
    parsed = parse_xlws_py_formula(formula)
    if parsed is None:
        return None
    script_index, return_type, deps = parsed
    if delta_row == 0 and delta_col == 0:
        return formula, list(deps)
    shifted_deps: list[str] = []
    for dep in deps:
        shifted = shift_xlws_py_dep(dep, delta_row, delta_col)
        if shifted is None:
            return None
        shifted_deps.append(shifted)
    prefix = "=" if formula.strip().startswith("=") else ""
    parts = [str(script_index), str(return_type)] + shifted_deps
    shifted_formula = f"{prefix}_xlfn._xlws.PY({','.join(parts)})"
    return shifted_formula, shifted_deps


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
                raise ValueError(f"invalid script index {idx_s!r} in xl/pythonScripts.xml")
            if idx < 0 or idx > 100_000 or idx > len(script_els) + 10_000:
                raise ValueError(f"invalid script index {idx} in xl/pythonScripts.xml")
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


def _shared_formula_map(ws_root: ET.Element) -> dict[str, tuple[str, str]]:
    """Resolve shared formula masters (si → (formula text, master A1))."""
    masters: dict[str, tuple[str, str]] = {}
    for c in find_all(ws_root, "c"):
        a1 = c.attrib.get("r") or ""
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
            masters[si] = (body, a1)
    return masters


def _iter_py_cells(ws_root: ET.Element, sheet_title: str, issues: list[str] | None = None) -> list[ExcelPyCell]:
    masters = _shared_formula_map(ws_root)
    cells: list[ExcelPyCell] = []
    failed_si: set[str] = set()
    cell_si: dict[str, str] = {}

    for c in find_all(ws_root, "c"):
        a1 = c.attrib.get("r") or ""
        f = find_child(c, "f")
        if f is None or not a1:
            continue
        body = "".join(f.itertext()).strip()
        t_attr = f.attrib.get("t") or ""
        si = f.attrib.get("si")

        if t_attr == "shared":
            if si is not None:
                cell_si[a1] = si
            master_info = masters.get(si or "") if si is not None else None
            if master_info is not None:
                master_body, master_a1 = master_info
                if "_xlws.py" in master_body.lower():
                    m_row, m_col = _col_row(master_a1)
                    c_row, c_col = _col_row(a1)
                    delta_r = c_row - m_row
                    delta_c = c_col - m_col
                    shifted = shift_xlws_py_formula(master_body, delta_r, delta_c)
                    if shifted is None:
                        if si is not None:
                            failed_si.add(si)
                        if issues is not None:
                            issues.append(f"shared formula si={si} at {sheet_title}!{a1}: unsupported dependency argument in {master_body!r}")
                        continue
                    formula, deps = shifted
                    parsed = parse_xlws_py_formula(formula)
                    if parsed is None:
                        continue
                    script_index, return_type, _ = parsed
                    array_ref = (f.attrib.get("ref") or "").replace("$", "") if t_attr == "array" else ""
                    cells.append(ExcelPyCell(
                        sheet=sheet_title,
                        cell=a1,
                        script_index=script_index,
                        return_type=return_type,
                        deps=deps,
                        formula_raw=formula if formula.startswith("=") else f"={formula}",
                        array_ref=array_ref,
                        row=c_row,
                        col=c_col,
                    ))
                    continue

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
        array_ref = (f.attrib.get("ref") or "").replace("$", "") if t_attr == "array" else ""
        cells.append(ExcelPyCell(sheet=sheet_title, cell=a1, script_index=script_index, return_type=return_type, deps=deps, formula_raw=formula if formula.startswith("=") else f"={formula}", array_ref=array_ref, row=row, col=col))

    if failed_si:
        cells = [c for c in cells if cell_si.get(c.cell) not in failed_si]
    return cells


def parse_excel_xlsx(path: str | Path) -> ExcelWorkbookModel:
    """Parse an ``.xlsx`` into scripts, PY cells, sheet map, tables, array anchors."""
    path = Path(path)
    issues: list[str] = []
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
            cells.extend(_iter_py_cells(ws_root, sh.title, issues=issues))
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
        return ExcelWorkbookModel(scripts=scripts, cells=cells, sheets=sheets, tables=tables, anchor_snapshots=anchors, source_path=str(path), issues=issues)


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


def load_excel_model(path: str | Path) -> ExcelWorkbookModel:
    """Load from ``.xlsx`` or a JSON fixture matching ``ExcelWorkbookModel.to_dict()``."""
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
