# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Minimal SVG → ShapeOp converter for ppt-master UNO export.

UPSTREAM NOTE (WriterAgent addition — parallel to, not a fork of, drawingml_converter.py):
  Upstream's full converter lives in the venv install:
  ``<PPT_MASTER_DATA_ROOT>/scripts/svg_to_pptx/drawingml_converter.py``
  (pure Python — import via plugin.contrib.ppt_master.upstream).

  # Upstream full conversion (PPTX export — run in user venv / scripts on sys.path):
  #   from svg_to_pptx.drawingml_converter import convert_svg_to_slide_shapes
  #   from svg_to_pptx.pptx_builder import create_pptx_with_native_svg
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from xml.etree import ElementTree as ET

from plugin.contrib.ppt_master.coords import (
    DEFAULT_SLIDE_HEIGHT_HMM,
    DEFAULT_SLIDE_WIDTH_HMM,
    parse_viewbox,
    px_to_hmm,
    slide_dims_for_viewbox,
)
from plugin.contrib.ppt_master.shape_ops import ShapeOp, SlideBuildPlan

SVG_NS = "http://www.w3.org/2000/svg"
XLINK_NS = "http://www.w3.org/1999/xlink"


def _local_tag(tag: str) -> str:
    if "}" in tag:
        return tag.rsplit("}", 1)[-1]
    return tag


def _parse_hex_color(value: str | None) -> int | None:
    if not value or value.strip().lower() in ("none", "transparent"):
        return None
    v = value.strip()
    if v.startswith("#"):
        try:
            hex_part = v[1:]
            if len(hex_part) == 3:
                hex_part = "".join(c * 2 for c in hex_part)
            return int(hex_part[:6], 16)
        except ValueError:
            return None
    names = {
        "black": 0x000000,
        "white": 0xFFFFFF,
        "red": 0xFF0000,
        "green": 0x00FF00,
        "blue": 0x0000FF,
        "yellow": 0xFFFF00,
        "gray": 0x808080,
        "grey": 0x808080,
    }
    return names.get(v.lower())


def _parse_float(val: str | None, default: float = 0.0) -> float:
    if val is None:
        return default
    try:
        m = re.match(r"^([-\d.]+)", str(val).strip())
        return float(m.group(1)) if m else default
    except (TypeError, ValueError):
        return default


def _stroke_width_hmm(sw: str | None, slide_w: int, vb_w: float) -> int | None:
    px = _parse_float(sw, 1.0)
    if px <= 0:
        return None
    return max(1, px_to_hmm(px, slide_hmm=slide_w, viewbox_px=vb_w))


def _text_content(elem: ET.Element) -> str:
    parts: list[str] = []
    if elem.text:
        parts.append(elem.text)
    for child in elem:
        if _local_tag(child.tag) == "tspan":
            if child.text:
                parts.append(child.text)
            if child.tail:
                parts.append(child.tail)
        elif child.tail:
            parts.append(child.tail)
    return "".join(parts).strip()


def _resolve_image_href(elem: ET.Element, svg_dir: Path) -> str | None:
    href = elem.get(f"{{{XLINK_NS}}}href") or elem.get("href")
    if not href:
        return None
    if href.startswith("data:"):
        return None
    for candidate in (svg_dir / href, svg_dir.parent / href, svg_dir.parent / "images" / href):
        if candidate.is_file():
            return str(candidate.resolve())
    return None


class _ConvertState:
    def __init__(self, slide_w: int, slide_h: int, vb_min_x: float, vb_min_y: float, vb_w: float, vb_h: float, svg_dir: Path):
        self.slide_w = slide_w
        self.slide_h = slide_h
        self.vb_min_x = vb_min_x
        self.vb_min_y = vb_min_y
        self.vb_w = vb_w
        self.vb_h = vb_h
        self.svg_dir = svg_dir

    def x_hmm(self, px: float) -> int:
        return px_to_hmm(px, slide_hmm=self.slide_w, viewbox_px=self.vb_w, viewbox_min=self.vb_min_x)

    def y_hmm(self, py: float) -> int:
        return px_to_hmm(py, slide_hmm=self.slide_h, viewbox_px=self.vb_h, viewbox_min=self.vb_min_y)

    def w_hmm(self, px: float) -> int:
        return max(1, px_to_hmm(px, slide_hmm=self.slide_w, viewbox_px=self.vb_w, viewbox_min=0.0))

    def h_hmm(self, px: float) -> int:
        return max(1, px_to_hmm(px, slide_hmm=self.slide_h, viewbox_px=self.vb_h, viewbox_min=0.0))


def _convert_element(elem: ET.Element, st: _ConvertState, inherited: dict) -> list[ShapeOp]:
    tag = _local_tag(elem.tag)
    style = dict(inherited)
    for attr in ("fill", "stroke", "stroke-width", "opacity", "font-size", "font-family"):
        if elem.get(attr):
            style[attr] = elem.get(attr)

    if tag == "g":
        ops: list[ShapeOp] = []
        child_style = style
        for child in elem:
            ops.extend(_convert_element(child, st, child_style))
        return ops

    if tag == "rect":
        x = _parse_float(elem.get("x"))
        y = _parse_float(elem.get("y"))
        w = _parse_float(elem.get("width"), 100)
        h = _parse_float(elem.get("height"), 100)
        return [
            ShapeOp(
                kind="rect",
                x_hmm=st.x_hmm(x),
                y_hmm=st.y_hmm(y),
                w_hmm=st.w_hmm(w),
                h_hmm=st.h_hmm(h),
                fill_color=_parse_hex_color(style.get("fill")),
                line_color=_parse_hex_color(style.get("stroke")),
                line_width_hmm=_stroke_width_hmm(style.get("stroke-width"), st.slide_w, st.vb_w),
            )
        ]

    if tag in ("circle", "ellipse"):
        cx = _parse_float(elem.get("cx"))
        cy = _parse_float(elem.get("cy"))
        if tag == "circle":
            r = _parse_float(elem.get("r"), 10)
            w = h = r * 2
        else:
            w = _parse_float(elem.get("rx"), 10) * 2
            h = _parse_float(elem.get("ry"), 10) * 2
        return [
            ShapeOp(
                kind="ellipse",
                x_hmm=st.x_hmm(cx - w / 2),
                y_hmm=st.y_hmm(cy - h / 2),
                w_hmm=st.w_hmm(w),
                h_hmm=st.h_hmm(h),
                fill_color=_parse_hex_color(style.get("fill")),
                line_color=_parse_hex_color(style.get("stroke")),
                line_width_hmm=_stroke_width_hmm(style.get("stroke-width"), st.slide_w, st.vb_w),
            )
        ]

    if tag == "line":
        x1 = _parse_float(elem.get("x1"))
        y1 = _parse_float(elem.get("y1"))
        x2 = _parse_float(elem.get("x2"))
        y2 = _parse_float(elem.get("y2"))
        x_hmm = st.x_hmm(min(x1, x2))
        y_hmm = st.y_hmm(min(y1, y2))
        return [
            ShapeOp(
                kind="line",
                x_hmm=x_hmm,
                y_hmm=y_hmm,
                w_hmm=max(1, abs(st.x_hmm(x2) - x_hmm)),
                h_hmm=max(1, abs(st.y_hmm(y2) - y_hmm)),
                line_color=_parse_hex_color(style.get("stroke")) or 0x000000,
                line_width_hmm=_stroke_width_hmm(style.get("stroke-width"), st.slide_w, st.vb_w) or 1,
            )
        ]

    if tag == "text":
        x = _parse_float(elem.get("x"))
        y = _parse_float(elem.get("y"))
        fs = _parse_float(style.get("font-size"), 16)
        text = _text_content(elem)
        if not text:
            return []
        est_w = max(1000, int(len(text) * fs * 0.6))
        est_h = max(500, int(fs * 1.5 * 100 / 72))
        return [
            ShapeOp(
                kind="text",
                x_hmm=st.x_hmm(x),
                y_hmm=st.y_hmm(y) - est_h // 2,
                w_hmm=min(st.slide_w - st.x_hmm(x), est_w),
                h_hmm=est_h,
                text=text,
                font_size_pt=fs * 0.75,
                font_family=style.get("font-family"),
                fill_color=_parse_hex_color(style.get("fill")) or 0x000000,
            )
        ]

    if tag == "image":
        img_path = _resolve_image_href(elem, st.svg_dir)
        x = _parse_float(elem.get("x"))
        y = _parse_float(elem.get("y"))
        w = _parse_float(elem.get("width"), 100)
        h = _parse_float(elem.get("height"), 100)
        if not img_path:
            return []
        return [
            ShapeOp(
                kind="image",
                x_hmm=st.x_hmm(x),
                y_hmm=st.y_hmm(y),
                w_hmm=st.w_hmm(w),
                h_hmm=st.h_hmm(h),
                image_path=img_path,
            )
        ]

    if tag == "path":
        d = elem.get("d") or ""
        nums = [float(n) for n in re.findall(r"[-+]?(?:\d*\.\d+|\d+)", d)]
        if len(nums) < 4:
            return []
        xs = nums[0::2]
        ys = nums[1::2]
        min_x, max_x = min(xs), max(xs)
        min_y, max_y = min(ys), max(ys)
        pts = [(st.x_hmm(x), st.y_hmm(y)) for x, y in zip(xs[:32], ys[:32])]
        return [
            ShapeOp(
                kind="path",
                x_hmm=st.x_hmm(min_x),
                y_hmm=st.y_hmm(min_y),
                w_hmm=max(1, st.x_hmm(max_x) - st.x_hmm(min_x)),
                h_hmm=max(1, st.y_hmm(max_y) - st.y_hmm(min_y)),
                path_points=pts,
                fill_color=_parse_hex_color(style.get("fill")),
                line_color=_parse_hex_color(style.get("stroke")),
                line_width_hmm=_stroke_width_hmm(style.get("stroke-width"), st.slide_w, st.vb_w),
            )
        ]

    return []


def svg_to_slide_plan(
    svg_path: Path,
    *,
    slide_index: int = 0,
    slide_width_hmm: int = DEFAULT_SLIDE_WIDTH_HMM,
    slide_height_hmm: int | None = None,
    notes_text: str | None = None,
) -> SlideBuildPlan:
    """Parse one SVG file into a SlideBuildPlan."""
    # WriterAgent UNO path (active): ElementTree walk → ShapeOp list → uno_apply.
    # Upstream PPTX path (commented):
    #   shapes_xml, rels = convert_svg_to_slide_shapes(svg_path)
    tree = ET.parse(svg_path)
    root = tree.getroot()
    vb = root.get("viewBox")
    min_x, min_y, vb_w, vb_h = parse_viewbox(vb)
    if not vb:
        vb_w = _parse_float(root.get("width"), DEFAULT_SLIDE_WIDTH_HMM)
        vb_h = _parse_float(root.get("height"), DEFAULT_SLIDE_HEIGHT_HMM)
    if slide_height_hmm is None:
        slide_width_hmm, slide_height_hmm = slide_dims_for_viewbox(vb_w, vb_h, width_hmm=slide_width_hmm)

    st = _ConvertState(slide_width_hmm, slide_height_hmm, min_x, min_y, vb_w, vb_h, svg_path.parent)
    shapes: list[ShapeOp] = []
    for child in root:
        if _local_tag(child.tag) == "defs":
            continue
        shapes.extend(_convert_element(child, st, {}))

    return SlideBuildPlan(
        slide_index=slide_index,
        viewbox_width_px=vb_w,
        viewbox_height_px=vb_h,
        slide_width_hmm=slide_width_hmm,
        slide_height_hmm=slide_height_hmm,
        shapes=shapes,
        notes_text=notes_text,
        svg_source=str(svg_path),
    )


def collect_svg_files(project_path: Path, *, subdir: str = "svg_final", data_root: Path | None = None) -> list[Path]:
    """Return sorted SVG paths from a ppt-master project folder."""
    root = data_root
    if root is None:
        env = os.environ.get("PPT_MASTER_DATA_ROOT", "").strip()
        if env:
            root = Path(env)
    if root is not None:
        from plugin.contrib.ppt_master.upstream import collect_svg_files_upstream

        upstream_files = collect_svg_files_upstream(project_path, root)
        if upstream_files:
            return upstream_files

    # WriterAgent fallback when ppt-master is not installed (unit tests, minimal dirs).
    # Upstream equivalent (venv): svg_to_pptx.pptx_discovery.find_svg_files
    for name in (subdir, "svg_output"):
        folder = project_path / name
        if folder.is_dir():
            files = sorted(folder.glob("*.svg"))
            if files:
                return files
    return []
