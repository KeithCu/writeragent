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

def test_evaluate_slide_fidelity_closes_source_doc_after_read(monkeypatch, tmp_path: Path):
    from plugin.ppt_master import fidelity

    class DummyDoc:
        def __init__(self):
            self.closed = False
        def close(self, deliver_ownership):
            self.closed = True

    class DummyPage:
        def getCount(self):
            if hasattr(self, "source_doc") and getattr(self.source_doc, "closed", False):
                raise Exception("Page used after doc closed!")
            return 1

    doc = DummyDoc()
    source_doc = DummyDoc()
    page = DummyPage()
    source_page = DummyPage()
    source_page.source_doc = source_doc

    def mock_import(ctx, pptx_path, slide_index, odp_path):
        return doc, page, source_page, source_doc

    def mock_metrics(src_page, tgt_page):
        # Access the source_page which would fail if source_doc was closed
        src_page.getCount()
        from plugin.ppt_master.fidelity import StructuralMetrics
        return StructuralMetrics()

    monkeypatch.setattr(fidelity, "import_slide_to_odp", mock_import)
    monkeypatch.setattr(fidelity, "structural_metrics_pptx", mock_metrics)
    monkeypatch.setattr(fidelity, "soffice_convert_to_pdf", lambda *args, **kwargs: None)

    fidelity.evaluate_slide_fidelity(
        None,
        project_dir=tmp_path,
        slide_label="test.svg",
        slide_index=0,
        pptx_path=tmp_path / "test.pptx",
        reference_deck_pdf=tmp_path / "ref.pdf",
        work_dir=tmp_path,
        soffice="echo",
        skip_visual=True,
    )

    assert source_doc.closed is True
    assert doc.closed is True
