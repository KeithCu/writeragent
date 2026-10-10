# SPDX-License-Identifier: GPL-3.0-or-later
"""Shared OOXML helpers: element traversal, relationships, namespaces, and ZIP rewrite."""

from __future__ import annotations

import io
from pathlib import Path
from typing import TYPE_CHECKING, Callable
import zipfile
from xml.etree import ElementTree as ET  # nosemgrep: use-defused-xml  # local .xlsx ZIP parts

if TYPE_CHECKING:
    from plugin.calc.excel_py_convert.models import SheetInfo

SSML_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
CONTENT_TYPES_NS = "http://schemas.openxmlformats.org/package/2006/content-types"
PACKAGE_REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
OFFICE_DOC_REL_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
MC_NS = "http://schemas.openxmlformats.org/markup-compatibility/2006"
PY_SCRIPT_NS = "http://schemas.microsoft.com/office/spreadsheetml/2022/pythonscript"

# Standard prefixes and namespaces used by Excel and OpenXML packages.
STANDARD_OOXML_NAMESPACES: dict[str, str] = {
    "": SSML_NS,
    "r": OFFICE_DOC_REL_NS,
    "mc": MC_NS,
    "x14ac": "http://schemas.microsoft.com/office/spreadsheetml/2009/9/ac",
    "x14": "http://schemas.microsoft.com/office/spreadsheetml/2009/9/main",
    "x15": "http://schemas.microsoft.com/office/spreadsheetml/2010/11/main",
    "x16": "http://schemas.microsoft.com/office/spreadsheetml/2015/02/main",
    "x16r2": "http://schemas.microsoft.com/office/spreadsheetml/2015/02/main",
    "xr": "http://schemas.microsoft.com/office/spreadsheetml/2014/revision",
    "xr2": "http://schemas.microsoft.com/office/spreadsheetml/2015/revision2",
    "xr3": "http://schemas.microsoft.com/office/spreadsheetml/2016/revision3",
    "xr6": "http://schemas.microsoft.com/office/spreadsheetml/2016/revision6",
    "xr10": "http://schemas.microsoft.com/office/spreadsheetml/2016/revision10",
    "xdr": "http://schemas.openxmlformats.org/drawingml/2006/spreadsheetDrawing",
    "a": "http://schemas.openxmlformats.org/drawingml/2006/main",
}


def register_standard_namespaces() -> None:
    """Register standard OOXML prefixes with ElementTree to prevent ns0/ns1 renames."""
    for prefix, uri in STANDARD_OOXML_NAMESPACES.items():
        ET.register_namespace(prefix, uri)


# Register upfront for module consumers.
register_standard_namespaces()


def local_name(tag: str) -> str:
    """Strip '{http://...}' namespace wrapper from an XML tag."""
    if tag.startswith("{"):
        return tag.rsplit("}", 1)[-1]
    return tag


def find_all(parent: ET.Element, name: str) -> list[ET.Element]:
    """Find all descendant elements whose local tag matches *name*."""
    return [el for el in parent.iter() if local_name(el.tag) == name]


def find_child(parent: ET.Element, name: str) -> ET.Element | None:
    """Find the first direct child whose local tag matches *name*."""
    for child in list(parent):
        if local_name(child.tag) == name:
            return child
    return None


def parse_and_extract_namespaces(data: bytes) -> tuple[ET.Element, dict[str, str]]:
    """Parse XML bytes into an ElementTree root, registering and returning all in-scope namespaces."""
    ns_map: dict[str, str] = {}
    try:
        for _event, (prefix, uri) in ET.iterparse(io.BytesIO(data), events=["start-ns"]):
            ns_map[prefix] = uri
            ET.register_namespace(prefix, uri)
    except Exception:
        pass
    root = ET.fromstring(data)
    return root, ns_map


def serialize_xml_preserving_namespaces(root: ET.Element, original_namespaces: dict[str, str] | None = None) -> bytes:
    """Serialize *root* to bytes, ensuring all ignorable prefixes are declared on the root element.

    ElementTree writes xmlns only for prefixes used in a tag or attribute
    QName. A prefix listed in ``mc:Ignorable`` (for example ``x14ac xr``)
    but unused in a QName is omitted, and Excel repairs the file. Detect
    those prefixes and inject the missing xmlns declaration on the opening
    root tag.
    """
    out = ET.tostring(root, encoding="utf-8", xml_declaration=True)
    ns_map = dict(STANDARD_OOXML_NAMESPACES)
    if original_namespaces:
        ns_map.update(original_namespaces)

    ignorable_prefixes: set[str] = set()
    for k, v in root.attrib.items():
        if k.endswith("}Ignorable") or k == "mc:Ignorable":
            ignorable_prefixes.update(v.split())

    if not ignorable_prefixes:
        return out

    start = out.find(b"<", 5) if out.startswith(b"<?xml") else 0
    tag_end = out.find(b">", start)
    if tag_end == -1:
        return out
    is_self_closing = out[tag_end - 1 : tag_end] == b"/"
    insert_pos = tag_end - 1 if is_self_closing else tag_end
    root_tag_bytes = out[start:tag_end]

    missing: dict[str, str] = {}
    for prefix in ignorable_prefixes:
        decl = f"xmlns:{prefix}=".encode("utf-8")
        if decl not in root_tag_bytes and prefix in ns_map:
            missing[prefix] = ns_map[prefix]

    if not missing:
        return out

    insert = b"".join(f' xmlns:{p}="{u}"'.encode("utf-8") for p, u in sorted(missing.items()))
    return out[:insert_pos] + insert + out[insert_pos:]


def resolve_rel_target(rels_path: str, target: str) -> str:
    """Resolve a Relationship Target to a package-relative path (forward slashes).

    In OPC a leading slash is a package-absolute part path. Stripping it
    and testing ``startswith('xl/')`` treats a target outside ``xl/`` as
    relative to the owning directory.
    """
    norm = target.replace("\\", "/")
    if norm.startswith("/"):
        clean = norm.lstrip("/")
    elif norm.startswith("xl/") or norm.startswith("[Content_Types"):
        clean = norm
    else:
        owning_dir = str(Path(rels_path).parent.parent).replace("\\", "/")
        if owning_dir == ".":
            owning_dir = ""
        clean = f"{owning_dir}/{norm}" if owning_dir else norm

    parts: list[str] = []
    for p in clean.split("/"):
        if p == "..":
            if parts:
                parts.pop()
        elif p and p != ".":
            parts.append(p)
    return "/".join(parts)


def parse_rels(zf: zipfile.ZipFile, rels_path: str) -> dict[str, str]:
    """Return relationship Id → package-relative Target."""
    out: dict[str, str] = {}
    try:
        root = ET.fromstring(zf.read(rels_path))
    except KeyError:
        return out
    for rel in root:
        if local_name(rel.tag) != "Relationship":
            continue
        rid = rel.attrib.get("Id") or ""
        target = rel.attrib.get("Target") or ""
        if not rid or not target:
            continue
        out[rid] = resolve_rel_target(rels_path, target)
    return out


def parse_rels_with_types(zf: zipfile.ZipFile, rels_path: str) -> list[tuple[str, str, str]]:
    """Return (Id, Type, Target) triples."""
    out: list[tuple[str, str, str]] = []
    try:
        root = ET.fromstring(zf.read(rels_path))
    except KeyError:
        return out
    for rel in root:
        if local_name(rel.tag) != "Relationship":
            continue
        rid = rel.attrib.get("Id") or ""
        rtype = rel.attrib.get("Type") or ""
        target = rel.attrib.get("Target") or ""
        if not rid or not target:
            continue
        out.append((rid, rtype, resolve_rel_target(rels_path, target)))
    return out


def workbook_sheets(zf: zipfile.ZipFile) -> list[SheetInfo]:
    """Map workbook sheet order/titles to worksheet part paths and rel_ids."""
    from plugin.calc.excel_py_convert.models import SheetInfo

    try:
        wb = ET.fromstring(zf.read("xl/workbook.xml"))
    except KeyError:
        return []
    rels = parse_rels(zf, "xl/_rels/workbook.xml.rels")
    sheets_el = None
    for el in wb:
        if local_name(el.tag) == "sheets":
            sheets_el = el
            break
    if sheets_el is None:
        return []
    out: list[SheetInfo] = []
    for order, sh in enumerate(list(sheets_el)):
        if local_name(sh.tag) != "sheet":
            continue
        title = sh.attrib.get("name") or f"Sheet{order + 1}"
        rid = sh.attrib.get(f"{{{OFFICE_DOC_REL_NS}}}id") or ""
        if not rid:
            for k, v in sh.attrib.items():
                if k.endswith("}id") or k in ("r:id", "id"):
                    rid = v
                    break
        part = rels.get(rid, "")
        if part and not part.startswith("xl/"):
            part = f"xl/{part}"
        out.append(SheetInfo(title=title, order=order, part_name=part, rel_id=rid))
    return out


def rewrite_zip(
    src: str | Path,
    dst: str | Path,
    transform: Callable[[zipfile.ZipFile, zipfile.ZipFile], None],
    *,
    temp_suffix: str = ".tmprewrite",
) -> None:
    """Rewrite a ZIP file atomically from *src* to *dst*, cleaning up temporary files on exception."""
    src_path = Path(src)
    dst_path = Path(dst)
    tmp_path = dst_path.with_suffix(dst_path.suffix + temp_suffix)
    try:
        with zipfile.ZipFile(src_path, "r") as zin, zipfile.ZipFile(tmp_path, "w", compression=zipfile.ZIP_DEFLATED) as zout:
            transform(zin, zout)
        tmp_path.replace(dst_path)
    finally:
        if tmp_path.exists():
            tmp_path.unlink()


# Backward-compatible private aliases
_local = local_name
_findall = find_all
_find_child = find_child
_workbook_sheets = workbook_sheets
