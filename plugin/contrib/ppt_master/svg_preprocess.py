# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Normalize ppt-master SVG files before LibreOffice draw_svg_import."""

from __future__ import annotations

import re
import tempfile
from pathlib import Path
from typing import Any
from xml.etree import ElementTree as ET

from plugin.contrib.ppt_master.coords import (
    DEFAULT_VIEWBOX_HEIGHT_PX,
    DEFAULT_VIEWBOX_WIDTH_PX,
    parse_viewbox,
    slide_dims_for_viewbox,
)

SVG_NS = "http://www.w3.org/2000/svg"
XLINK_NS = "http://www.w3.org/1999/xlink"

ET.register_namespace("", SVG_NS)
ET.register_namespace("xlink", XLINK_NS)


def _local_tag(tag: str) -> str:
    if "}" in tag:
        return tag.rsplit("}", 1)[-1]
    return tag


def _parse_float(val: str | None, default: float = 0.0) -> float:
    if val is None:
        return default
    try:
        m = re.match(r"^([-\d.]+)", str(val).strip())
        return float(m.group(1)) if m else default
    except (TypeError, ValueError):
        return default


def _resolve_image_href(href: str, svg_dir: Path, project_dir: Path | None) -> str | None:
    if not href or href.startswith("data:"):
        return None
    candidates = [svg_dir / href]
    if project_dir is not None:
        candidates.extend((project_dir / href, project_dir / "images" / href, project_dir / "images" / Path(href).name))
    for candidate in candidates:
        if candidate.is_file():
            return candidate.resolve().as_uri()
    return None


def _fix_image_hrefs(root: ET.Element, svg_dir: Path, project_dir: Path | None) -> int:
    fixed = 0
    for elem in root.iter():
        if _local_tag(elem.tag) != "image":
            continue
        href = elem.get(f"{{{XLINK_NS}}}href") or elem.get("href")
        if not href:
            continue
        resolved = _resolve_image_href(href, svg_dir, project_dir)
        if resolved is None:
            continue
        elem.set(f"{{{XLINK_NS}}}href", resolved)
        if elem.get("href"):
            elem.set("href", resolved)
        fixed += 1
    return fixed


def _strip_unbreakable_filters(root: ET.Element) -> int:
    """Remove SVG filter attrs LO leaves as stacked GraphicObjectShapes after Break.

    ppt-master KPI cards use filter=\"url(#cardShadow)\"; draw_svg_import + Break cannot
    decompose those paths, so four cards collapse to the same GraphicObject position.
    """
    removed = 0
    for elem in root.iter():
        if elem.get("filter"):
            del elem.attrib["filter"]
            removed += 1
    for parent in root.iter():
        for child in list(parent):
            if _local_tag(child.tag) == "filter":
                parent.remove(child)
    return removed


def _strip_text_fill_opacity(root: ET.Element) -> int:
    """Drop fill-opacity on text — LO Break leaves those runs as GraphicObjectShape strips."""
    removed = 0
    for elem in root.iter():
        if _local_tag(elem.tag) != "text":
            continue
        if elem.get("fill-opacity") is not None:
            del elem.attrib["fill-opacity"]
            removed += 1
        for child in elem:
            if _local_tag(child.tag) == "tspan" and child.get("fill-opacity") is not None:
                del child.attrib["fill-opacity"]
                removed += 1
    return removed


def _ensure_slide_dimensions(root: ET.Element) -> None:
    """Set width/height in mm so LO page size matches Impress slide (25400×14288 hmm)."""
    vb = root.get("viewBox")
    _min_x, _min_y, vb_w, vb_h = parse_viewbox(vb)
    if not vb:
        vb_w = _parse_float(root.get("width"), DEFAULT_VIEWBOX_WIDTH_PX)
        vb_h = _parse_float(root.get("height"), DEFAULT_VIEWBOX_HEIGHT_PX)
    width_hmm, height_hmm = slide_dims_for_viewbox(vb_w, vb_h)
    # draw_svg_import maps SVG width/height to page size; px values oversize the page (~1.33×)
    # and break font/layout scaling when shapes are copied to a standard Impress slide.
    root.set("width", f"{width_hmm / 100:.3f}mm")
    root.set("height", f"{height_hmm / 100:.3f}mm")


def _serialize_svg_tree(tree: ET.ElementTree[Any]) -> str:
    """Serialize SVG for LO import (strip ElementTree ns0 prefixes)."""
    import io

    buf = io.BytesIO()
    tree.write(buf, encoding="utf-8", xml_declaration=True)
    text = buf.getvalue().decode("utf-8")
    text = re.sub(r'\sxmlns:ns\d="[^"]*"', "", text)
    text = re.sub(r"</ns\d+:", "</", text)
    text = re.sub(r"<ns\d+:", "<", text)
    if 'xmlns="http://www.w3.org/2000/svg"' not in text and "<svg" in text:
        text = text.replace("<svg", f'<svg xmlns="{SVG_NS}"', 1)
    return text


def _write_svg_tree(tree: ET.ElementTree[Any], dest: Path) -> None:
    dest.write_text(_serialize_svg_tree(tree), encoding="utf-8")


def _svg_tree_to_text(tree: ET.ElementTree[Any]) -> str:
    return _serialize_svg_tree(tree)


def preprocess_svg_for_import(
    svg_path: Path,
    *,
    project_dir: Path | None = None,
    out_path: Path | None = None,
) -> Path:
    """Return path to preprocessed SVG (temp file unless *out_path* given)."""
    svg_path = Path(svg_path).expanduser().resolve()
    tree = ET.parse(svg_path)
    root = tree.getroot()
    proj = project_dir.expanduser().resolve() if project_dir else svg_path.parent.parent
    href_fixes = _fix_image_hrefs(root, svg_path.parent, proj if proj.is_dir() else None)
    filter_fixes = _strip_unbreakable_filters(root)
    opacity_fixes = _strip_text_fill_opacity(root)
    old_vb, old_w, old_h = root.get("viewBox"), root.get("width"), root.get("height")
    _ensure_slide_dimensions(root)
    changed = href_fixes > 0 or filter_fixes > 0 or opacity_fixes > 0 or (
        root.get("viewBox") != old_vb or root.get("width") != old_w or root.get("height") != old_h
    )
    if not changed and out_path is None:
        return svg_path
    if out_path is not None:
        dest = out_path
    else:
        with tempfile.NamedTemporaryFile(suffix=".svg", prefix="wa_ppt_", delete=False) as tmp:
            dest = Path(tmp.name)
    _write_svg_tree(tree, dest)
    return dest


def preprocess_svg_text(svg_path: Path, *, project_dir: Path | None = None) -> str:
    """Return preprocessed SVG XML as a string (for unit tests)."""
    svg_path = Path(svg_path).expanduser().resolve()
    tree = ET.parse(svg_path)
    root = tree.getroot()
    proj = project_dir.expanduser().resolve() if project_dir else svg_path.parent.parent
    _fix_image_hrefs(root, svg_path.parent, proj if proj.is_dir() else None)
    _strip_unbreakable_filters(root)
    _strip_text_fill_opacity(root)
    _ensure_slide_dimensions(root)
    return _svg_tree_to_text(tree)
