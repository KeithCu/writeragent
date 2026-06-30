# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

from pathlib import Path

from plugin.contrib.ppt_master.svg_preprocess import preprocess_svg_text


def test_preprocess_ensure_viewbox_and_dimensions(tmp_path: Path):
    svg = tmp_path / "s.svg"
    svg.write_text(
        '<svg xmlns="http://www.w3.org/2000/svg">'
        '<rect x="0" y="0" width="100" height="50" fill="#000"/>'
        "</svg>",
        encoding="utf-8",
    )
    out = preprocess_svg_text(svg)
    assert 'width="' in out
    assert 'height="' in out


def test_preprocess_resolves_image_href(tmp_path: Path):
    project = tmp_path / "proj"
    images = project / "images"
    svg_dir = project / "svg_final"
    images.mkdir(parents=True)
    svg_dir.mkdir()
    img = images / "logo.png"
    img.write_bytes(b"\x89PNG\r\n")
    svg = svg_dir / "slide.svg"
    svg.write_text(
        '<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink" viewBox="0 0 100 100">'
        '<image xlink:href="logo.png" x="0" y="0" width="50" height="50"/>'
        "</svg>",
        encoding="utf-8",
    )
    out = preprocess_svg_text(svg, project_dir=project)
    assert "file://" in out
    assert "logo.png" in out
