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
    parse_rels,
    sheet_rels_path,
    workbook_sheets,
)

_RE_COORD = re.compile(r"^(\$?)([A-Za-z]+)(\$?)(\d+)$")
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
    m = _RE_COORD.match(a1.replace("$", "").strip())
    if not m:
        return 0, 0
    return int(m.group(4)), letters_to_col_index(m.group(2))


def a1_span_bounds(ref: str) -> tuple[tuple[int, int], tuple[int, int]] | None:
    """Return ((min_col, min_row), (max_col, max_row)) for *ref* (1-based), or None."""
    raw = (ref or "").replace("$", "").strip()
    if "!" in raw:
        raw = raw.split("!", 1)[1].strip()
    if not raw:
        return None
    left, sep, right = raw.partition(":")
    r1, c1 = _col_row(left.strip())
    if c1 == 0:
        return None
    if not sep:
        return ((c1, r1), (c1, r1))
    r2, c2 = _col_row(right.strip())
    if c2 == 0:
        return None
    return ((min(c1, c2), min(r1, r2)), (max(c1, c2), max(r1, r2)))


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
            if ch == "'" and i + 1 < len(s) and s[i + 1] == "'":
                buf.append("''")
                i += 2
                continue
            if ch == "'":
                in_sq = False
        elif in_dq:
            if ch == '"':
                in_dq = False
        elif ch == "'":
            in_sq = True
        elif ch == '"':
            in_dq = True
        elif ch == "(":
            paren_depth += 1
        elif ch == ")":
            paren_depth = max(0, paren_depth - 1)
        elif ch == "[":
            bracket_depth += 1
        elif ch == "]":
            bracket_depth = max(0, bracket_depth - 1)
        elif ch == "{":
            brace_depth += 1
        elif ch == "}":
            brace_depth = max(0, brace_depth - 1)
        elif ch == "," and paren_depth == 0 and bracket_depth == 0 and brace_depth == 0:
            args.append("".join(buf).strip())
            buf = []
            i += 1
            continue
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


_RE_ANCHORARRAY = re.compile(r"^((?:_xlfn\.)?ANCHORARRAY\s*\()(.*)(\))$", re.IGNORECASE)
_RE_DEFINED_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_.]*$")


def _shift_single_coord(coord: str, delta_row: int, delta_col: int) -> str | None:
    part = coord.strip()
    m = _RE_COORD.match(part)
    if m:
        col_locked, row_locked = bool(m.group(1)), bool(m.group(3))
        new_c = letters_to_col_index(m.group(2)) + (0 if col_locked else delta_col)
        new_r = int(m.group(4)) + (0 if row_locked else delta_row)
        if new_c < 1 or new_r < 1 or new_c > 16384 or new_r > 1048576:
            return None
        return f"{'$' if col_locked else ''}{col_index_to_letters(new_c)}{'$' if row_locked else ''}{new_r}"
    bare = part.replace("$", "")
    locked = part.startswith("$")
    if bare.isalpha():
        new_c = letters_to_col_index(bare) + (0 if locked else delta_col)
        if new_c < 1 or new_c > 16384:
            return None
        return ("$" if locked else "") + col_index_to_letters(new_c)
    if bare.isdigit():
        new_r = int(bare) + (0 if locked else delta_row)
        if new_r < 1 or new_r > 1048576:
            return None
        return ("$" if locked else "") + str(new_r)
    return None


def _shift_cell_or_range_ref(ref: str, delta_row: int, delta_col: int) -> str | None:
    sheet_prefix = ""
    target = ref.strip()
    if "!" in target:
        sheet_part, sep, target = target.rpartition("!")
        sheet_prefix = f"{sheet_part}{sep}"

    if _RE_DEFINED_NAME.match(target) and not _RE_COORD.match(target.replace("$", "")):
        return ref.strip()

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


def _read_xml(zf: zipfile.ZipFile, part: str, issues: list[str]) -> ET.Element | None:
    try:
        raw = zf.read(part)
    except KeyError:
        return None
    try:
        return ET.fromstring(raw)
    except ET.ParseError as exc:
        issues.append(f"{part}: {exc}")
        return None


def _parse_python_scripts(zf: zipfile.ZipFile, issues: list[str]) -> list[str]:
    """Parse ``xl/pythonScripts.xml``.

    Map each script to its index (the explicit attribute, or document
    order). Dropping indexed scripts whenever any sequential script is
    present throws away the indexed bank.
    """
    root = _read_xml(zf, "xl/pythonScripts.xml", issues)
    if root is None:
        return []
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
                issues.append(f"invalid script index {idx_s!r} in xl/pythonScripts.xml")
                continue
            if idx < 0 or idx > 100_000 or idx > len(script_els) + 10_000:
                issues.append(f"script index {idx} out of range in xl/pythonScripts.xml")
                continue
        if idx in scripts_by_idx:
            issues.append(f"duplicate script index {idx} in xl/pythonScripts.xml")
        scripts_by_idx[idx] = text

    if not scripts_by_idx:
        return []
    max_i = max(scripts_by_idx.keys())
    out = [""] * (max_i + 1)
    for i, body in scripts_by_idx.items():
        out[i] = body
    return out


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


def _parse_table_ref(zf: zipfile.ZipFile, table_part: str, sheet_title: str, issues: list[str]) -> tuple[str, str] | None:
    root = _read_xml(zf, table_part, issues)
    if root is None:
        return None
    name = root.attrib.get("displayName") or root.attrib.get("name") or ""
    ref = root.attrib.get("ref") or ""
    if not name or not ref:
        return None
    return name, _qualify_sheet_ref(sheet_title, ref)


def _collect_array_refs(ws_root: ET.Element, sheet_title: str, anchors: dict[str, str]) -> None:
    """Qualified keys overwrite; bare keys are first-wins. Only t="array" (not shared ref=)."""
    for c in find_all(ws_root, "c"):
        cell_ref = (c.attrib.get("r") or "").replace("$", "")
        f = find_child(c, "f")
        if f is None or not cell_ref or (f.attrib.get("t") or "") != "array":
            continue
        arr = (f.attrib.get("ref") or "").strip().replace("$", "")
        if not arr:
            continue
        top_left = arr.split(":", 1)[0]
        q_top = _qualify_sheet_ref(sheet_title, top_left)
        q_arr = _qualify_sheet_ref(sheet_title, arr)
        anchors[q_top] = q_arr
        anchors.setdefault(top_left, arr)
        anchors.setdefault(cell_ref, arr)
        anchors[_qualify_sheet_ref(sheet_title, cell_ref)] = q_arr


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


def _iter_py_cells(ws_root: ET.Element, sheet_title: str, issues: list[str]) -> list[ExcelPyCell]:
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
        row, col = _col_row(a1)
        if t_attr == "shared" and si is not None:
            cell_si[a1] = si
            master_info = masters.get(si)
            if master_info is not None and "_xlws.py" in master_info[0].lower():
                master_body, master_a1 = master_info
                m_row, m_col = _col_row(master_a1)
                shifted = shift_xlws_py_formula(master_body, row - m_row, col - m_col)
                if shifted is None:
                    failed_si.add(si)
                    issues.append(f"shared formula si={si} at {sheet_title}!{a1}: unsupported dependency argument in {master_body!r}")
                    continue
                body = shifted[0]
        if not body or "_xlws.py" not in body.lower():
            continue
        parsed = parse_xlws_py_formula(body)
        if parsed is None:
            continue
        script_index, return_type, deps = parsed
        # Only t="array" is a spill ref. A shared-formula ref= must not clear those cells.
        array_ref = (f.attrib.get("ref") or "").replace("$", "") if t_attr == "array" else ""
        cells.append(ExcelPyCell(
            sheet=sheet_title, cell=a1, script_index=script_index, return_type=return_type,
            deps=deps, formula_raw=body if body.startswith("=") else f"={body}",
            array_ref=array_ref, row=row, col=col,
        ))
    if failed_si:
        cells = [c for c in cells if cell_si.get(c.cell) not in failed_si]
    return cells


def parse_excel_xlsx(path: str | Path) -> ExcelWorkbookModel:
    """Parse an ``.xlsx`` into scripts, PY cells, sheet map, tables, array anchors."""
    path = Path(path)
    issues: list[str] = []
    with zipfile.ZipFile(path, "r") as zf:
        sheets = workbook_sheets(zf)
        scripts = _parse_python_scripts(zf, issues)
        tables: dict[str, str] = {}
        anchors: dict[str, str] = {}
        cells: list[ExcelPyCell] = []
        for sh in sheets:
            if not sh.part_name:
                continue
            ws_root = _read_xml(zf, sh.part_name, issues)
            if ws_root is None:
                continue
            cells.extend(_iter_py_cells(ws_root, sh.title, issues=issues))
            _collect_array_refs(ws_root, sh.title, anchors)
            for _rid, rtype, target in parse_rels(zf, sheet_rels_path(sh.part_name)):
                if not rtype.rstrip("/").endswith("table") and "tables/table" not in target:
                    continue
                parsed = _parse_table_ref(zf, target, sh.title, issues)
                if parsed:
                    name, qref = parsed
                    tables[name] = qref
        return ExcelWorkbookModel(scripts=scripts, cells=cells, sheets=sheets, tables=tables, anchor_snapshots=anchors, source_path=str(path), issues=issues)


def load_excel_model(path: str | Path) -> ExcelWorkbookModel:
    """Load from ``.xlsx`` or a JSON object ``ExcelWorkbookModel.from_dict`` accepts."""
    path = Path(path)
    if path.suffix.lower() == ".json":
        data = json.loads(path.read_text(encoding="utf-8"))
        model = ExcelWorkbookModel.from_dict(data)
        model.source_path = model.source_path or str(path)
        # Fixtures often use sheet1/sheet2 — synthesize sheet order if missing.
        if not model.sheets:
            titles = list(dict.fromkeys(c.sheet for c in model.cells))
            model.sheets = [SheetInfo(title=t, order=i, part_name="") for i, t in enumerate(titles)]
        for c in model.cells:
            if not c.row or not c.col:
                c.row, c.col = _col_row(c.cell)
        return model
    return parse_excel_xlsx(path)
