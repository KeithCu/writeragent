# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Compare ppt-master SVG slides to WriterAgent UNO import output (PDF/PNG diff + structure)."""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from plugin.contrib.ppt_master.coords import DEFAULT_SLIDE_HEIGHT_HMM, DEFAULT_SLIDE_WIDTH_HMM
from plugin.contrib.ppt_master.svg_preprocess import preprocess_svg_for_import
from plugin.contrib.ppt_master.upstream import collect_svg_files
from plugin.embeddings.embeddings_soffice_convert import resolve_soffice_executable
from plugin.ppt_master.adapter.uno_svg_import import import_svg_to_slide

log = logging.getLogger(__name__)

SVG_NS = "http://www.w3.org/2000/svg"
DEFAULT_DIFF_THRESHOLD = 0.12
DEFAULT_DPI = 150


@dataclass
class StructuralMetrics:
    svg_text_elements: int = 0
    odf_text_shapes: int = 0
    odf_shape_counts: dict[str, int] = field(default_factory=dict)


@dataclass
class PdfInfo:
    page_size_pts: str = ""
    pages: int = 0
    file_bytes: int = 0


@dataclass
class VisualMetrics:
    width: int = 0
    height: int = 0
    mae: float = 1.0
    diff_fraction: float = 1.0
    reference_png: str = ""
    imported_png: str = ""
    diff_png: str = ""
    reference_pdf: str = ""
    imported_pdf: str = ""
    reference_pdf_info: PdfInfo | None = None
    imported_pdf_info: PdfInfo | None = None


@dataclass
class SlideFidelityResult:
    svg_name: str
    slide_index: int
    passed: bool
    threshold: float
    visual: VisualMetrics | None = None
    structural: StructuralMetrics | None = None
    artifacts: dict[str, str] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)


@dataclass
class ProjectFidelityReport:
    project: str
    work_dir: str
    threshold: float
    slides: list[SlideFidelityResult] = field(default_factory=list)

    @property
    def passed_count(self) -> int:
        return sum(1 for s in self.slides if s.passed and not s.errors)

    @property
    def failed_count(self) -> int:
        return sum(1 for s in self.slides if not s.passed or s.errors)

    def worst_slide(self) -> SlideFidelityResult | None:
        scored = [s for s in self.slides if s.visual is not None and not s.errors]
        if not scored:
            return None
        return max(scored, key=lambda s: s.visual.diff_fraction if s.visual else 1.0)

    def to_dict(self) -> dict[str, Any]:
        worst = self.worst_slide()
        return {
            "project": self.project,
            "work_dir": self.work_dir,
            "threshold": self.threshold,
            "summary": {
                "passed": self.passed_count,
                "failed": self.failed_count,
                "worst_slide": worst.svg_name if worst else None,
                "worst_diff_fraction": worst.visual.diff_fraction if worst and worst.visual else None,
            },
            "slides": [asdict(s) for s in self.slides],
        }


def count_svg_text_elements(svg_path: Path) -> int:
    """Count ``<text>`` nodes in an SVG (ppt-master uses one block per line)."""
    try:
        root = ET.parse(svg_path).getroot()
    except ET.ParseError as exc:
        log.warning("SVG parse failed for %s: %s", svg_path, exc)
        return 0
    return sum(1 for elem in root.iter() if elem.tag == "text" or elem.tag.endswith("}text"))


def count_odf_shape_types(page: Any) -> dict[str, int]:
    counts: dict[str, int] = {}
    for i in range(page.getCount()):
        shape_type = page.getByIndex(i).getShapeType()
        short = shape_type.rsplit(".", 1)[-1]
        counts[short] = counts.get(short, 0) + 1
    return counts


def structural_metrics(svg_path: Path, page: Any) -> StructuralMetrics:
    counts = count_odf_shape_types(page)
    return StructuralMetrics(
        svg_text_elements=count_svg_text_elements(svg_path),
        odf_text_shapes=counts.get("TextShape", 0),
        odf_shape_counts=counts,
    )


def read_pdf_info(pdf_path: Path) -> PdfInfo:
    info = PdfInfo(file_bytes=pdf_path.stat().st_size if pdf_path.is_file() else 0)
    proc = subprocess.run(["pdfinfo", str(pdf_path)], capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        return info
    for line in proc.stdout.splitlines():
        if line.startswith("Pages:"):
            info.pages = int(line.split(":", 1)[1].strip())
        elif line.startswith("Page size:"):
            info.page_size_pts = line.split(":", 1)[1].strip()
    return info


def compare_png_images(reference_png: Path, imported_png: Path, diff_png: Path, *, pixel_threshold: int = 24) -> VisualMetrics:
    """Pixel diff after resizing imported image to reference dimensions."""
    from PIL import Image, ImageChops, ImageStat

    ref = Image.open(reference_png).convert("RGB")
    imp = Image.open(imported_png).convert("RGB")
    if imp.size != ref.size:
        imp = imp.resize(ref.size, Image.Resampling.LANCZOS)
    diff = ImageChops.difference(ref, imp)
    diff.save(diff_png)
    stat = ImageStat.Stat(diff)
    mae = sum(stat.mean) / (3.0 * 255.0)
    diff_pixels = sum(1 for px in diff.getdata() if sum(px) > pixel_threshold)
    total = ref.size[0] * ref.size[1] or 1
    return VisualMetrics(
        width=ref.size[0],
        height=ref.size[1],
        mae=round(mae, 5),
        diff_fraction=round(diff_pixels / total, 5),
        reference_png=str(reference_png),
        imported_png=str(imported_png),
        diff_png=str(diff_png),
    )


def soffice_convert_to_pdf(soffice: str, source: Path, out_dir: Path, *, timeout_sec: int = 120) -> Path | None:
    out_dir.mkdir(parents=True, exist_ok=True)
    cmd = [
        soffice,
        "--headless",
        "--nologo",
        "--nodefault",
        "--nofirststartwizard",
        "--convert-to",
        "pdf",
        "--outdir",
        str(out_dir),
        str(source),
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout_sec, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        log.warning("soffice pdf convert failed for %s: %s", source, exc)
        return None
    if proc.returncode != 0:
        log.warning("soffice pdf exit %s: %s", proc.returncode, (proc.stderr or proc.stdout or "")[:400])
        return None
    pdfs = sorted(out_dir.glob(f"{source.stem}.pdf"))
    if not pdfs:
        pdfs = sorted(out_dir.glob("*.pdf"), key=lambda p: p.stat().st_mtime, reverse=True)
    return pdfs[0] if pdfs else None


def pdf_to_png(pdf_path: Path, png_path: Path, *, dpi: int = DEFAULT_DPI, timeout_sec: int = 60) -> bool:
    """Rasterize first PDF page to PNG (pdftoppm, then ImageMagick)."""
    png_path.parent.mkdir(parents=True, exist_ok=True)
    stem = png_path.with_suffix("")
    pdftoppm = shutil.which("pdftoppm")
    if pdftoppm:
        out_base = stem
        cmd = [pdftoppm, "-png", "-singlefile", "-f", "1", "-l", "1", "-r", str(dpi), str(pdf_path), str(out_base)]
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout_sec, check=False)
        except (OSError, subprocess.TimeoutExpired):
            proc = None
        produced = Path(f"{out_base}.png")
        if proc is not None and proc.returncode == 0 and produced.is_file():
            if produced != png_path:
                produced.replace(png_path)
            return True
    magick = shutil.which("magick") or shutil.which("convert")
    if magick:
        cmd = [magick, "-density", str(dpi), f"{pdf_path}[0]", str(png_path)]
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout_sec, check=False)
        except (OSError, subprocess.TimeoutExpired):
            return False
        return proc.returncode == 0 and png_path.is_file()
    return False


def import_slide_to_odp(ctx: Any, svg_path: Path, project_dir: Path, odp_path: Path) -> tuple[Any, Any] | None:
    """Import one SVG via the shipped pipeline; save a one-slide Impress doc."""
    import uno

    from plugin.framework.uno_context import get_desktop

    desktop = get_desktop(ctx)
    hidden = uno.createUnoStruct("com.sun.star.beans.PropertyValue", Name="Hidden", Value=True)
    doc = desktop.loadComponentFromURL("private:factory/simpress", "_blank", 0, (hidden,))
    if doc is None:
        return None
    page = doc.getDrawPages().getByIndex(0)
    try:
        page.setPropertyValue("Width", DEFAULT_SLIDE_WIDTH_HMM)
        page.setPropertyValue("Height", DEFAULT_SLIDE_HEIGHT_HMM)
    except Exception as exc:
        log.debug("set impress page size: %s", exc)
    result = import_svg_to_slide(ctx, doc, svg_path, slide_index=0, project_dir=project_dir)
    if result.get("status") != "ok":
        doc.close(True)
        return None
    page = doc.getDrawPages().getByIndex(0)
    odp_path.parent.mkdir(parents=True, exist_ok=True)
    doc.storeToURL(odp_path.resolve().as_uri(), ())
    return doc, page


def evaluate_slide_fidelity(
    ctx: Any,
    *,
    project_dir: Path,
    svg_path: Path,
    slide_index: int,
    work_dir: Path,
    soffice: str,
    threshold: float = DEFAULT_DIFF_THRESHOLD,
    dpi: int = DEFAULT_DPI,
    skip_visual: bool = False,
) -> SlideFidelityResult:
    """Import one slide, export reference/import PDFs, compare PNGs, return metrics."""
    slide_dir = work_dir / f"slide_{slide_index:02d}_{svg_path.stem}"
    slide_dir.mkdir(parents=True, exist_ok=True)
    result = SlideFidelityResult(
        svg_name=svg_path.name,
        slide_index=slide_index,
        passed=False,
        threshold=threshold,
        artifacts={"svg": str(svg_path.resolve())},
    )

    odp_path = slide_dir / "imported.odp"
    imported = import_slide_to_odp(ctx, svg_path, project_dir, odp_path)
    if imported is None:
        result.errors.append("import_svg_to_slide failed")
        return result
    doc, page = imported
    result.structural = structural_metrics(svg_path, page)
    result.artifacts["imported_odp"] = str(odp_path)
    try:
        doc.close(True)
    except Exception as exc:
        log.debug("close impress doc: %s", exc)

    if skip_visual:
        text_ok = result.structural.odf_text_shapes >= result.structural.svg_text_elements
        result.passed = text_ok
        if not text_ok:
            result.errors.append(
                f"text shape count {result.structural.odf_text_shapes} < svg text {result.structural.svg_text_elements}"
            )
        return result

    ref_pdf_dir = slide_dir / "ref_pdf"
    imp_pdf_dir = slide_dir / "imp_pdf"
    preprocessed = preprocess_svg_for_import(svg_path, project_dir=project_dir)
    result.artifacts["preprocessed_svg"] = str(preprocessed)
    ref_pdf = soffice_convert_to_pdf(soffice, preprocessed, ref_pdf_dir)
    imp_pdf = soffice_convert_to_pdf(soffice, odp_path, imp_pdf_dir)
    if ref_pdf is None:
        result.errors.append("reference PDF export failed (soffice convert SVG)")
    else:
        result.artifacts["reference_pdf"] = str(ref_pdf)
    if imp_pdf is None:
        result.errors.append("imported PDF export failed (soffice convert ODP)")
    else:
        result.artifacts["imported_pdf"] = str(imp_pdf)
    if ref_pdf is None or imp_pdf is None:
        return result

    ref_png = slide_dir / "reference.png"
    imp_png = slide_dir / "imported.png"
    diff_png = slide_dir / "diff.png"
    if not pdf_to_png(ref_pdf, ref_png, dpi=dpi):
        result.errors.append("reference PNG rasterize failed (install poppler pdftoppm or ImageMagick)")
        return result
    if not pdf_to_png(imp_pdf, imp_png, dpi=dpi):
        result.errors.append("imported PNG rasterize failed (install poppler pdftoppm or ImageMagick)")
        return result

    result.visual = compare_png_images(ref_png, imp_png, diff_png)
    result.visual.reference_pdf = str(ref_pdf)
    result.visual.imported_pdf = str(imp_pdf)
    result.visual.reference_pdf_info = read_pdf_info(ref_pdf)
    result.visual.imported_pdf_info = read_pdf_info(imp_pdf)
    if (
        result.visual.reference_pdf_info.page_size_pts
        and result.visual.imported_pdf_info.page_size_pts
        and result.visual.reference_pdf_info.page_size_pts != result.visual.imported_pdf_info.page_size_pts
    ):
        result.errors.append(
            "PDF page size mismatch: reference "
            f"{result.visual.reference_pdf_info.page_size_pts} vs imported "
            f"{result.visual.imported_pdf_info.page_size_pts}"
        )
    result.passed = result.visual.diff_fraction <= threshold
    if not result.passed:
        result.errors.append(
            f"visual diff_fraction {result.visual.diff_fraction:.3f} > threshold {threshold:.3f} "
            f"(see {diff_png.name})"
        )
    if result.structural and result.structural.odf_text_shapes < result.structural.svg_text_elements:
        result.errors.append(
            f"text shapes {result.structural.odf_text_shapes} < svg text elements {result.structural.svg_text_elements}"
        )
    return result


def run_project_fidelity(
    ctx: Any,
    project_dir: Path,
    *,
    work_dir: Path | None = None,
    slide_names: list[str] | None = None,
    threshold: float = DEFAULT_DIFF_THRESHOLD,
    dpi: int = DEFAULT_DPI,
    skip_visual: bool = False,
) -> ProjectFidelityReport:
    project_dir = project_dir.expanduser().resolve()
    out_dir = (work_dir or project_dir / ".import_fidelity").expanduser().resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    soffice = resolve_soffice_executable()
    if soffice is None:
        raise RuntimeError("soffice not found; install LibreOffice and ensure soffice is on PATH")

    svg_files = collect_svg_files(project_dir)
    if slide_names:
        wanted = {n if n.endswith(".svg") else f"{n}.svg" for n in slide_names}
        svg_files = [p for p in svg_files if p.name in wanted]
    report = ProjectFidelityReport(
        project=str(project_dir),
        work_dir=str(out_dir),
        threshold=threshold,
    )
    for i, svg_path in enumerate(svg_files):
        report.slides.append(
            evaluate_slide_fidelity(
                ctx,
                project_dir=project_dir,
                svg_path=svg_path,
                slide_index=i,
                work_dir=out_dir,
                soffice=soffice,
                threshold=threshold,
                dpi=dpi,
                skip_visual=skip_visual,
            )
        )
    report_path = out_dir / "report.json"
    report_path.write_text(json.dumps(report.to_dict(), indent=2), encoding="utf-8")
    return report


def write_agent_summary(report: ProjectFidelityReport, path: Path) -> None:
    """Short markdown checklist for humans/agents (worst slides first)."""
    lines = [
        "# PPT-Master import fidelity",
        "",
        f"Project: `{report.project}`",
        f"Work dir: `{report.work_dir}`",
        f"Threshold (diff_fraction): {report.threshold}",
        "",
        f"Passed: {report.passed_count} / {len(report.slides)}",
        "",
        "## Slides (worst first)",
        "",
    ]
    slides = sorted(
        report.slides,
        key=lambda s: (s.visual.diff_fraction if s.visual else 1.0, s.svg_name),
        reverse=True,
    )
    for slide in slides:
        diff = slide.visual.diff_fraction if slide.visual else "n/a"
        status = "PASS" if slide.passed and not slide.errors else "FAIL"
        lines.append(f"- **{status}** `{slide.svg_name}` diff_fraction={diff}")
        if slide.errors:
            for err in slide.errors:
                lines.append(f"  - {err}")
        if slide.visual:
            lines.append(f"  - diff image: `{slide.visual.diff_png}`")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
