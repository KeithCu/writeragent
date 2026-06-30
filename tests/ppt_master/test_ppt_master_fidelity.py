# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

from pathlib import Path

from PIL import Image

from plugin.ppt_master.fidelity import (
    ProjectFidelityReport,
    SlideFidelityResult,
    VisualMetrics,
    compare_png_images,
    count_svg_text_elements,
    write_agent_summary,
)


def test_count_svg_text_elements(tmp_path: Path):
    svg = tmp_path / "s.svg"
    svg.write_text(
        '<svg xmlns="http://www.w3.org/2000/svg"><text x="0" y="10">A</text><text x="0" y="20">B</text></svg>',
        encoding="utf-8",
    )
    assert count_svg_text_elements(svg) == 2


def test_compare_png_identical(tmp_path: Path):
    img = Image.new("RGB", (64, 32), color=(240, 128, 64))
    ref = tmp_path / "ref.png"
    imp = tmp_path / "imp.png"
    diff = tmp_path / "diff.png"
    img.save(ref)
    img.save(imp)
    metrics = compare_png_images(ref, imp, diff)
    assert metrics.mae == 0.0
    assert metrics.diff_fraction == 0.0
    assert diff.is_file()


def test_compare_png_different(tmp_path: Path):
    ref = tmp_path / "ref.png"
    imp = tmp_path / "imp.png"
    diff = tmp_path / "diff.png"
    Image.new("RGB", (40, 40), color=(255, 255, 255)).save(ref)
    Image.new("RGB", (40, 40), color=(0, 0, 0)).save(imp)
    metrics = compare_png_images(ref, imp, diff)
    assert metrics.diff_fraction > 0.9


def test_write_agent_summary(tmp_path: Path):
    report = ProjectFidelityReport(project="/proj", work_dir=str(tmp_path), threshold=0.1)
    report.slides = [
        SlideFidelityResult(
            svg_name="01_cover.svg",
            slide_index=0,
            passed=False,
            threshold=0.1,
            visual=VisualMetrics(diff_fraction=0.25, diff_png=str(tmp_path / "diff.png")),
            errors=["visual diff too high"],
        )
    ]
    out = tmp_path / "SUMMARY.md"
    write_agent_summary(report, out)
    text = out.read_text(encoding="utf-8")
    assert "01_cover.svg" in text
    assert "FAIL" in text
