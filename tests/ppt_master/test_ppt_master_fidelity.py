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

def test_evaluate_slide_fidelity_closes_target_doc_after_read(monkeypatch, tmp_path: Path):
    from plugin.ppt_master import fidelity

    class DummyDoc:
        def __init__(self):
            self.closed = False
        def close(self, deliver_ownership):
            self.closed = True

    class DummyPage:
        def getCount(self):
            return 1

    doc = DummyDoc()
    page = DummyPage()

    def mock_import(ctx, pptx_path, slide_index, odp_path):
        return doc, page

    def mock_metrics(tgt_page, *args, **kwargs):
        tgt_page.getCount()
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

    assert doc.closed is True


def _create_minimal_pptx(path: Path, slide_xml: str) -> Path:
    import zipfile
    pres_xml = """<?xml version="1.0" encoding="utf-8"?>
<p:presentation xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main"
                xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
  <p:sldIdLst>
    <p:sldId id="256" r:id="rId1"/>
  </p:sldIdLst>
</p:presentation>
"""
    rels_xml = """<?xml version="1.0" encoding="UTF-8"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/slide" Target="slides/slide1.xml"/>
</Relationships>
"""
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("ppt/presentation.xml", pres_xml)
        zf.writestr("ppt/_rels/presentation.xml.rels", rels_xml)
        zf.writestr("ppt/slides/slide1.xml", slide_xml)
    return path


def test_count_pptx_slide_text_shapes(tmp_path: Path):
    from plugin.ppt_master.fidelity import count_pptx_slide_text_shapes

    slide_xml = """<?xml version="1.0" encoding="utf-8"?>
<p:sld xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"
       xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main">
  <p:cSld>
    <p:spTree>
      <p:nvGrpSpPr><p:cNvPr id="1" name=""/><p:cNvGrpSpPr/><p:nvPr/></p:nvGrpSpPr>
      <p:grpSpPr/>
      <!-- Shape 1: TextShape (non-empty text in txBody) -->
      <p:sp>
        <p:txBody>
          <a:bodyPr/>
          <a:p><a:r><a:t>Title Text</a:t></a:r></a:p>
        </p:txBody>
      </p:sp>
      <!-- Shape 2: Empty txBody (should not count) -->
      <p:sp>
        <p:txBody>
          <a:bodyPr/>
          <a:p><a:endParaRPr/></a:p>
        </p:txBody>
      </p:sp>
      <!-- Shape 3: Whitespace only txBody (should not count) -->
      <p:sp>
        <p:txBody>
          <a:bodyPr/>
          <a:p><a:r><a:t>   \t\n  </a:t></a:r></a:p>
        </p:txBody>
      </p:sp>
      <!-- Shape 4: Shape without txBody (should not count) -->
      <p:sp>
        <p:spPr/>
      </p:sp>
      <!-- Shape 5: Group shape (should not count) -->
      <p:grpSp>
        <p:sp>
          <p:txBody>
            <a:p><a:r><a:t>Grouped Text</a:t></a:r></a:p>
          </p:txBody>
        </p:sp>
      </p:grpSp>
      <!-- Shape 6: Table / graphicFrame (should not count) -->
      <p:graphicFrame>
        <a:graphic>
          <a:graphicData>
            <a:tbl>
              <a:tr><a:tc><a:txBody><a:p><a:r><a:t>Table Cell</a:t></a:r></a:p></a:txBody></a:tc></a:tr>
            </a:tbl>
          </a:graphicData>
        </a:graphic>
      </p:graphicFrame>
    </p:spTree>
  </p:cSld>
</p:sld>
"""
    pptx = _create_minimal_pptx(tmp_path / "deck.pptx", slide_xml)
    assert count_pptx_slide_text_shapes(pptx, 0) == 1
    assert count_pptx_slide_text_shapes(pptx, 1) == 0


def test_structural_metrics_pptx_detects_dropped_text_shapes(tmp_path: Path):
    from plugin.ppt_master.fidelity import structural_metrics_pptx

    slide_xml = """<?xml version="1.0" encoding="utf-8"?>
<p:sld xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"
       xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main">
  <p:cSld>
    <p:spTree>
      <p:nvGrpSpPr><p:cNvPr id="1" name=""/><p:cNvGrpSpPr/><p:nvPr/></p:nvGrpSpPr>
      <p:grpSpPr/>
      <p:sp>
        <p:txBody><a:p><a:r><a:t>First</a:t></a:r></a:p></p:txBody>
      </p:sp>
      <p:sp>
        <p:txBody><a:p><a:r><a:t>Second</a:t></a:r></a:p></p:txBody>
      </p:sp>
    </p:spTree>
  </p:cSld>
</p:sld>
"""
    pptx = _create_minimal_pptx(tmp_path / "deck.pptx", slide_xml)

    class ImportedPage:
        def getCount(self):
            return 1
        def getByIndex(self, idx):
            class Shape:
                def getShapeType(self):
                    return "com.sun.star.drawing.TextShape"
            return Shape()

    # Imported page has 1 text shape, but source PPTX had 2
    metrics = structural_metrics_pptx(ImportedPage(), pptx_path=pptx, slide_index=0)
    assert metrics.svg_text_elements == 2
    assert metrics.odf_text_shapes == 1
    # text loss gate condition: odf_text_shapes >= svg_text_elements is False
    assert metrics.odf_text_shapes < metrics.svg_text_elements


def test_evaluate_slide_fidelity_closes_on_early_returns(monkeypatch, tmp_path: Path):
    from plugin.ppt_master import fidelity

    class DummyDoc:
        def __init__(self):
            self.closed = False
        def close(self, deliver_ownership):
            self.closed = True

    class DummyPage:
        def getCount(self):
            return 1
        def getByIndex(self, idx):
            class Shape:
                def getShapeType(self):
                    return "com.sun.star.drawing.TextShape"
            return Shape()

    # Case 1: reference deck PDF missing
    doc1 = DummyDoc()
    monkeypatch.setattr(fidelity, "import_slide_to_odp", lambda *args, **kwargs: (doc1, DummyPage()))
    monkeypatch.setattr(fidelity, "soffice_convert_to_pdf", lambda *args, **kwargs: tmp_path / "imp.pdf")

    res1 = fidelity.evaluate_slide_fidelity(
        None,
        project_dir=tmp_path,
        slide_label="test.svg",
        slide_index=0,
        pptx_path=tmp_path / "nonexistent.pptx",
        reference_deck_pdf=tmp_path / "missing_ref.pdf",
        work_dir=tmp_path / "work1",
        soffice="echo",
        skip_visual=False,
    )
    assert doc1.closed is True
    assert "reference deck PDF missing" in res1.errors

    # Case 2: imported PDF export failed
    doc2 = DummyDoc()
    ref_pdf = tmp_path / "ref.pdf"
    ref_pdf.write_bytes(b"%PDF-1.4 test")
    monkeypatch.setattr(fidelity, "import_slide_to_odp", lambda *args, **kwargs: (doc2, DummyPage()))
    monkeypatch.setattr(fidelity, "soffice_convert_to_pdf", lambda *args, **kwargs: None)

    res2 = fidelity.evaluate_slide_fidelity(
        None,
        project_dir=tmp_path,
        slide_label="test.svg",
        slide_index=0,
        pptx_path=tmp_path / "nonexistent.pptx",
        reference_deck_pdf=ref_pdf,
        work_dir=tmp_path / "work2",
        soffice="echo",
        skip_visual=False,
    )
    assert doc2.closed is True
    assert any("imported PDF export failed" in err for err in res2.errors)

    # Case 3: reference PNG rasterize failed
    doc3 = DummyDoc()
    monkeypatch.setattr(fidelity, "import_slide_to_odp", lambda *args, **kwargs: (doc3, DummyPage()))
    monkeypatch.setattr(fidelity, "soffice_convert_to_pdf", lambda *args, **kwargs: tmp_path / "imp.pdf")
    monkeypatch.setattr(fidelity, "pdf_page_to_png", lambda *args, **kwargs: False)

    res3 = fidelity.evaluate_slide_fidelity(
        None,
        project_dir=tmp_path,
        slide_label="test.svg",
        slide_index=0,
        pptx_path=tmp_path / "nonexistent.pptx",
        reference_deck_pdf=ref_pdf,
        work_dir=tmp_path / "work3",
        soffice="echo",
        skip_visual=False,
    )
    assert doc3.closed is True
    assert any("reference PNG rasterize failed" in err for err in res3.errors)

