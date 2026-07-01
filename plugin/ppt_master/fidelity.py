# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Compare ppt-master PPTX slides to WriterAgent PPTX→ODP import (PDF/PNG diff + structure)."""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
import xml.etree.ElementTree as ET  # nosemgrep
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, cast

from plugin.contrib.ppt_master.upstream import collect_svg_files
from plugin.embeddings.embeddings_soffice_convert import resolve_soffice_executable
from plugin.ppt_master.adapter.uno_pptx_import import import_pptx_slide_to_odp, load_pptx_as_impress_doc
from plugin.ppt_master.paths import data_root_status
from plugin.ppt_master.pptx_build import ensure_project_pptx, find_project_pptx

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
        root = ET.parse(svg_path).getroot()  # nosemgrep
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


def count_page_text_shapes(page: Any) -> int:
    count = 0
    for i in range(page.getCount()):
        if "TextShape" in page.getByIndex(i).getShapeType():
            count += 1
    return count


def structural_metrics_pptx(source_page: Any, imported_page: Any) -> StructuralMetrics:
    counts = count_odf_shape_types(imported_page)
    return StructuralMetrics(
        svg_text_elements=count_page_text_shapes(source_page),
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
    diff_pixels = sum(sum(cast("tuple[int, ...]", px)) > pixel_threshold for px in cast("Any", diff.getdata()))
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


def pdf_page_to_png(pdf_path: Path, png_path: Path, *, page_1based: int, dpi: int = DEFAULT_DPI, timeout_sec: int = 60) -> bool:
    """Rasterize one PDF page (1-based index) to PNG."""
    png_path.parent.mkdir(parents=True, exist_ok=True)
    stem = png_path.with_suffix("")
    pdftoppm = shutil.which("pdftoppm")
    page = max(1, int(page_1based))
    if pdftoppm:
        out_base = stem
        cmd = [pdftoppm, "-png", "-singlefile", "-f", str(page), "-l", str(page), "-r", str(dpi), str(pdf_path), str(out_base)]
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
        cmd = [magick, "-density", str(dpi), f"{pdf_path}[{page - 1}]", str(png_path)]
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout_sec, check=False)
        except (OSError, subprocess.TimeoutExpired):
            return False
        return proc.returncode == 0 and png_path.is_file()
    return False


def import_slide_to_odp(
    ctx: Any,
    pptx_path: Path,
    slide_index: int,
    odp_path: Path,
) -> tuple[Any, Any, Any] | None:
    """Import one PPTX slide via the shipped pipeline; save a one-slide Impress doc."""
    imported = import_pptx_slide_to_odp(ctx, pptx_path, slide_index, odp_path)
    if imported is None:
        return None
    doc, page = imported
    source_doc = load_pptx_as_impress_doc(ctx, pptx_path)
    source_page = None
    if source_doc is not None:
        try:
            source_page = source_doc.getDrawPages().getByIndex(slide_index)
        finally:
            try:
                source_doc.close(True)
            except Exception as exc:
                log.debug("close source pptx doc: %s", exc)
    return doc, page, source_page


def evaluate_slide_fidelity(
    ctx: Any,
    *,
    project_dir: Path,
    slide_label: str,
    slide_index: int,
    pptx_path: Path,
    reference_deck_pdf: Path,
    work_dir: Path,
    soffice: str,
    threshold: float = DEFAULT_DIFF_THRESHOLD,
    dpi: int = DEFAULT_DPI,
    skip_visual: bool = False,
) -> SlideFidelityResult:
    """Import one PPTX slide to ODP, compare PDF page N (PPTX) vs imported ODP."""
    slide_dir = work_dir / f"slide_{slide_index:02d}_{Path(slide_label).stem}"
    slide_dir.mkdir(parents=True, exist_ok=True)
    result = SlideFidelityResult(
        svg_name=slide_label if slide_label.endswith(".svg") else f"{slide_label}.svg",
        slide_index=slide_index,
        passed=False,
        threshold=threshold,
        artifacts={"pptx": str(pptx_path.resolve())},
    )

    odp_path = slide_dir / "imported.odp"
    imported = import_slide_to_odp(ctx, pptx_path, slide_index, odp_path)
    if imported is None:
        result.errors.append("import_pptx_slide_to_odp failed")
        return result
    doc, page, source_page = imported
    if source_page is not None:
        result.structural = structural_metrics_pptx(source_page, page)
    else:
        result.structural = StructuralMetrics(odf_shape_counts=count_odf_shape_types(page))
    result.artifacts["imported_odp"] = str(odp_path)
    try:
        doc.close(True)
    except Exception as exc:
        log.debug("close impress doc: %s", exc)

    if skip_visual:
        if result.structural and source_page is not None:
            text_ok = result.structural.odf_text_shapes >= result.structural.svg_text_elements
            result.passed = text_ok
            if not text_ok:
                result.errors.append(
                    f"text shape count {result.structural.odf_text_shapes} < pptx text {result.structural.svg_text_elements}"
                )
        else:
            result.passed = page.getCount() > 0
        return result

    imp_pdf_dir = slide_dir / "imp_pdf"
    imp_pdf = soffice_convert_to_pdf(soffice, odp_path, imp_pdf_dir)
    ref_pdf = reference_deck_pdf
    if not ref_pdf.is_file():
        result.errors.append("reference deck PDF missing")
        return result
    if imp_pdf is None:
        result.errors.append("imported PDF export failed (soffice convert ODP)")
        return result
    result.artifacts["reference_pdf"] = str(ref_pdf)
    result.artifacts["imported_pdf"] = str(imp_pdf)

    ref_png = slide_dir / "reference.png"
    imp_png = slide_dir / "imported.png"
    diff_png = slide_dir / "diff.png"
    ref_page = slide_index + 1
    if not pdf_page_to_png(ref_pdf, ref_png, page_1based=ref_page, dpi=dpi):
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
    result.passed = result.visual.diff_fraction <= threshold
    if not result.passed:
        result.errors.append(
            f"visual diff_fraction {result.visual.diff_fraction:.3f} > threshold {threshold:.3f} "
            f"(see {diff_png.name})"
        )
    if result.structural and source_page is not None and result.structural.odf_text_shapes < result.structural.svg_text_elements:
        result.errors.append(
            f"text shapes {result.structural.odf_text_shapes} < pptx text shapes {result.structural.svg_text_elements}"
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

    pptx_path = find_project_pptx(project_dir)
    if pptx_path is None:
        status = data_root_status(ctx)
        if status.get("ok"):
            pptx_path, build_err = ensure_project_pptx(ctx, project_dir, Path(status["data_root"]))
            if pptx_path is None:
                raise RuntimeError(build_err or "PPTX not available for fidelity run")
        else:
            raise RuntimeError("No exports/*.pptx found; build PPTX or configure PPT-Master data path + venv")

    svg_files = collect_svg_files(project_dir)
    if slide_names:
        wanted = {n if n.endswith(".svg") else f"{n}.svg" for n in slide_names}
        svg_files = [p for p in svg_files if p.name in wanted]
    if not svg_files:
        raise RuntimeError("No SVG slide names found for fidelity slide list")

    ref_pdf_dir = out_dir / "reference_deck_pdf"
    reference_deck_pdf = out_dir / "reference_deck.pdf"
    if not reference_deck_pdf.is_file():
        converted = soffice_convert_to_pdf(soffice, pptx_path, ref_pdf_dir)
        if converted is None:
            raise RuntimeError("reference PDF export failed (soffice convert PPTX)")
        if converted != reference_deck_pdf:
            converted.replace(reference_deck_pdf)

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
                slide_label=svg_path.name,
                slide_index=i,
                pptx_path=pptx_path,
                reference_deck_pdf=reference_deck_pdf,
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
