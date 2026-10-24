# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Export LibreOffice Math objects to MathML and LaTeX.

Inverse of ``math_mml_convert`` (TeX/MathML → StarMath). Canonical in-document
form stays StarMath; TeX is a serialization for chat and tools.

LibreOffice MathML filter name: ``MathML XML (Math)`` (Help: MathML 2.0 / .mml).
"""

from __future__ import annotations

import logging
import os
import tempfile
from dataclasses import dataclass
from typing import Any, Iterator

from plugin.framework.errors import reraise_if_disposed, uno_read
from plugin.framework.uno_context import get_desktop, uno_same
from plugin.writer.math.math_mml_convert import (
    MATH_CLSID,
    _exception_message,
    file_url,
    iter_math_roots,
    math_root,
)

log = logging.getLogger("writeragent.writer")

# API FilterName for File > Save As > MathML 2.0 (.mml). See LO Help convertfilters.
MATHML_EXPORT_FILTER = "MathML XML (Math)"
_MATH_FACTORY_URL = "private:factory/smath"

_OBJECT_REPLACEMENT = "\ufffc"
_MATH_CLSID_NORM = MATH_CLSID.lower()


@dataclass(frozen=True)
class MathExportResult:
    ok: bool
    latex: str | None
    mathml: str | None
    error_message: str | None


@dataclass(frozen=True)
class WriterMathHit:
    """One Writer formula object in document order."""

    starmath: str
    latex: str | None
    mathml: str | None
    error_message: str | None
    display_block: bool
    para_index: int


def convert_mathml_to_latex(mathml_fragment: str) -> MathExportResult:
    """Presentation MathML string → LaTeX via vendored ``mathml-to-latex``. No UNO."""
    if not mathml_fragment or not isinstance(mathml_fragment, str):
        return MathExportResult(False, None, None, "empty_mathml")
    root = math_root(mathml_fragment)
    if not root:
        return MathExportResult(False, None, None, "not_math_root")
    try:
        from mathml_to_latex import MathMLToLaTeX
    except ImportError as exc:
        return MathExportResult(False, None, None, f"mathml_to_latex_import:{exc}")
    try:
        latex = MathMLToLaTeX.convert(root)
    except Exception as exc:
        log.debug("mathml-to-latex convert failed: %s", exc, exc_info=True)
        return MathExportResult(False, None, None, _exception_message(exc))
    if not isinstance(latex, str) or not latex.strip():
        return MathExportResult(False, None, None, "mathml_to_latex_empty_output")
    return MathExportResult(True, latex.strip(), root, None)


def convert_starmath_to_mathml(ctx: Any, starmath: str) -> MathExportResult:
    """StarMath command string → MathML via a hidden LibreOffice Math document."""
    if not starmath or not isinstance(starmath, str) or not starmath.strip():
        return MathExportResult(False, None, None, "empty_starmath")

    from plugin.writer.format import create_property_value

    desktop = get_desktop(ctx)
    hidden = (create_property_value("Hidden", True),)
    doc = desktop.loadComponentFromURL(_MATH_FACTORY_URL, "_blank", 0, hidden)
    if doc is None:
        return MathExportResult(False, None, None, "smath_factory_returned_none")

    fd, path = tempfile.mkstemp(suffix=".mml", prefix="writeragent-math-export-", text=False)
    os.close(fd)
    try:
        try:
            doc.setPropertyValue("Formula", starmath.strip())
        except Exception:
            doc.Formula = starmath.strip()

        url = file_url(path)
        props = (
            create_property_value("FilterName", MATHML_EXPORT_FILTER),
            create_property_value("Overwrite", True),
        )
        doc.storeToURL(url, props)
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            raw = f.read()
        mathml = math_root(raw)
        if not mathml:
            return MathExportResult(False, None, None, "empty_mathml_export")
        return MathExportResult(True, None, mathml, None)
    except Exception as exc:
        log.debug("convert_starmath_to_mathml failed: %s", exc, exc_info=True)
        return MathExportResult(False, None, None, _exception_message(exc))
    finally:
        try:
            doc.close(True)
        except Exception as exc:
            log.debug("math export doc close: %s", exc)
        try:
            os.unlink(path)
        except OSError:
            pass


def convert_starmath_to_latex(ctx: Any, starmath: str) -> MathExportResult:
    """StarMath → MathML (LO) → LaTeX (``mathml-to-latex``)."""
    mml = convert_starmath_to_mathml(ctx, starmath)
    if not mml.ok or not mml.mathml:
        return MathExportResult(False, None, mml.mathml, mml.error_message)
    tex = convert_mathml_to_latex(mml.mathml)
    if not tex.ok:
        return MathExportResult(False, None, mml.mathml, tex.error_message)
    return MathExportResult(True, tex.latex, mml.mathml, None)


def wrap_latex_delimiters(latex: str, *, display_block: bool) -> str:
    """Wrap TeX for ``apply_document_content`` / chat (``$…$`` vs ``$$…$$``)."""
    inner = (latex or "").strip()
    if inner.startswith("$$") and inner.endswith("$$"):
        return inner
    if inner.startswith("$") and inner.endswith("$") and not inner.startswith("$$"):
        if not display_block:
            return inner
        inner = inner[1:-1].strip()
    if display_block:
        return f"$${inner}$$"
    return f"${inner}$"


def iter_writer_math_objects(model: Any, ctx: Any | None = None) -> Iterator[WriterMathHit]:
    """Yield Math embeds in document order."""
    text = model.getText() if hasattr(model, "getText") else None
    if text is None:
        return
    batch = _MathExportBatch(ctx) if ctx is not None else None
    try:
        para_index = 0
        for para in _iter_body_paragraphs(text):
            for hit in _math_hits_in_paragraph(para, para_index, ctx, batch=batch):
                yield hit
            para_index += 1
    finally:
        if batch is not None:
            batch.close()


def inject_math_tex_into_html(model: Any, ctx: Any, html: str) -> str:
    """Replace XHTML ``<math>`` from Writer Math OLE with delimited TeX.

    ``XHTML Writer File`` inlines Presentation MathML (measured on quadratic.odt).
    Pair those ``<math>`` subtrees with OLE hits in order. Do not append leftovers.
    """
    if not html or not isinstance(html, str):
        return html
    math_matches = list(iter_math_roots(html))
    if not math_matches:
        return html

    hits = list(iter_writer_math_objects(model, ctx))
    n_hits = len(hits)
    n_math = len(math_matches)
    if n_hits == 0:
        return html
    if n_hits != n_math:
        log.error(
            "inject_math_tex_into_html: Math OLE count %d != <math> count %d; replacing min() only",
            n_hits,
            n_math,
        )

    parts: list[str] = []
    pos = 0
    for hit, match in zip(hits, math_matches):
        tex = hit.latex
        if not tex:
            snippet = (hit.starmath or hit.error_message or "")[:200]
            tex = f"[Math export fallback] {snippet}".strip()
        wrapped = wrap_latex_delimiters(tex, display_block=hit.display_block)
        parts.append(html[pos : match.start()])
        parts.append(wrapped)
        pos = match.end()
    parts.append(html[pos:])
    return "".join(parts)


def substitute_math_placeholders(
    content: str,
    math_records: list[tuple[str, str, bool]],
    ctx: Any,
) -> str:
    """Replace private-use formula placeholders with delimited TeX."""
    if not math_records or not content:
        return content

    for placeholder, starmath, display_block in math_records:
        res = convert_starmath_to_latex(ctx, starmath) if (ctx and starmath) else None
        if res and res.ok and res.latex:
            tex = res.latex
        else:
            snippet = starmath[:200]
            tex = f"[Math export fallback] {snippet}".strip()
        wrapped = wrap_latex_delimiters(tex, display_block=display_block)

        code = ord(placeholder)
        for target in (placeholder, f"&#x{code:x};", f"&#x{code:X};", f"&#{code};"):
            if target in content:
                content = content.replace(target, wrapped)
    return content


def inject_exported_math_tex(model: Any, ctx: Any, content: str) -> str:
    """Replace formula OLE holes with delimited TeX for the model/chat.

    Failures stay in the HTML as a visible fallback; never drop formulas.
    """
    if not content or model is None or ctx is None:
        return content
    try:
        return inject_math_tex_into_html(model, ctx, content)
    except Exception as exc:
        reraise_if_disposed(exc)
        log.exception("inject_exported_math_tex failed")
        return content


def selected_math_embeds(doc: Any) -> list[Any]:
    """Math ``TextEmbeddedObject``s in the current selection (document order)."""
    found: list[Any] = []

    def _add(obj: Any) -> None:
        if obj is None or not _is_math_embed(obj):
            return
        if any(uno_same(obj, existing) for existing in found):
            return
        found.append(obj)

    try:
        sel = doc.getCurrentSelection()
    except Exception:
        return []
    if sel is None:
        return []

    _add(sel)
    try:
        container = doc.getEmbeddedObjects()
        names = list(container.getElementNames())
        embed_objects = []
        for name in names:
            try:
                obj = container.getByName(name)
                if _is_math_embed(obj):
                    embed_objects.append(obj)
            except Exception:
                log.debug("selected_math_embeds: getByName(%r) failed", name, exc_info=True)
                continue
    except Exception:
        embed_objects = []

    get_count = getattr(sel, "getCount", None)
    get_by_index = getattr(sel, "getByIndex", None)
    if callable(get_count) and callable(get_by_index):
        try:
            count_val = get_count()
            n = count_val if isinstance(count_val, int) else int(str(count_val))
        except Exception:
            n = 0
        for i in range(n):
            try:
                el = get_by_index(i)
            except Exception:
                log.debug("selected_math_embeds: getByIndex(%s) failed", i, exc_info=True)
                continue
            _add(el)
            if hasattr(el, "getStart"):
                for obj in _math_embeds_in_text_range(el, embed_objects):
                    _add(obj)
    if hasattr(sel, "getStart"):
        for obj in _math_embeds_in_text_range(sel, embed_objects):
            _add(obj)
    return found


def math_embed_name(embed: Any) -> str | None:
    try:
        name = embed.getName() if hasattr(embed, "getName") else getattr(embed, "Name", None)
    except Exception:
        name = getattr(embed, "Name", None)
    text = str(name or "").strip()
    return text or None


def resolve_math_embed_name(doc: Any, embed: Any) -> str | None:
    """Stable ``getEmbeddedObjects()`` name for *embed*, or None."""
    named = math_embed_name(embed)
    if named:
        return named
    try:
        container = doc.getEmbeddedObjects()
        names = list(container.getElementNames())
    except Exception:
        return None
    for name in names:
        try:
            obj = container.getByName(name)
        except Exception:
            log.debug("resolve_math_embed_name: getByName(%r) failed", name, exc_info=True)
            continue
        if uno_same(obj, embed):
            return str(name)
    return None


def lookup_math_embed(doc: Any, name: str) -> Any | None:
    if not name:
        return None
    try:
        container = doc.getEmbeddedObjects()
        if not container.hasByName(name):
            return None
        obj = container.getByName(name)
    except Exception:
        return None
    return obj if _is_math_embed(obj) else None


def _math_embeds_in_text_range(range_obj: Any, embed_objects: list[Any]) -> list[Any]:
    """Math embeds whose anchors fall inside *range_obj*."""
    try:
        text = range_obj.getText()
        sel_start = range_obj.getStart()
        sel_end = range_obj.getEnd()
    except Exception:
        return []
    if text is None or sel_start is None or sel_end is None:
        return []
    found: list[Any] = []
    for obj in embed_objects:
        try:
            anchor = obj.getAnchor()
            anchor_start = anchor.getStart() if hasattr(anchor, "getStart") else anchor
            if text.compareRegionStarts(sel_start, anchor_start) < 0:
                continue
            if text.compareRegionStarts(anchor_start, sel_end) < 0:
                continue
        except Exception:
            log.debug("math embed anchor is not comparable to the selection", exc_info=True)
            continue
        found.append(obj)
    return found


def _is_math_embed(obj: Any) -> bool:
    try:
        clsid = str(getattr(obj, "CLSID", "") or "")
    except Exception:
        return False
    return clsid.lower() == _MATH_CLSID_NORM


def portion_math_embed(portion: Any) -> Any | None:
    """Return the Math TextEmbeddedObject from a text portion, or None."""
    # A false _is_math_embed result is the CLSID check. Repeating it cannot succeed.
    for key in ("TextEmbeddedObject", "EmbeddedObject", "TextContent"):
        try:
            obj = portion.getPropertyValue(key)
        except Exception:
            obj = getattr(portion, key, None)
        if obj is not None and _is_math_embed(obj):
            return obj
    # Writer exposes an as-character object only through the Frame portion's
    # content enumeration. Text portions enumerate objects anchored nearby too,
    # so only a Frame portion counts (else the text before a formula is lost).
    try:
        if str(portion.getPropertyValue("TextPortionType") or "") != "Frame":
            return None
        enum = portion.createContentEnumeration("com.sun.star.text.TextContent")
    except Exception:
        return None
    while enum is not None and enum.hasMoreElements() is True:
        try:
            obj = enum.nextElement()
        except Exception:
            break
        if obj is not None and _is_math_embed(obj):
            return obj
    return None


def read_starmath(embed: Any) -> str:
    """Read the StarMath formula string from a Math embedded object."""
    try:
        inner = embed.getEmbeddedObject() if hasattr(embed, "getEmbeddedObject") else embed
        formula = inner.Formula if inner is not None else ""
        return str(formula or "").strip()
    except Exception:
        try:
            return str(embed.Formula or "").strip()
        except Exception:
            return ""


def _iter_body_paragraphs(text_obj: Any) -> Iterator[Any]:
    """Yield paragraphs in document order, descending into table cells."""
    try:
        enum = text_obj.createEnumeration()
    except Exception:
        return
    while enum.hasMoreElements() is True:
        try:
            el = enum.nextElement()
        except Exception:
            break
        if uno_read(lambda: bool(el.supportsService("com.sun.star.text.TextTable")), False):
            try:
                names = el.getCellNames() or ()
            except Exception:
                log.debug("math export: table getCellNames failed", exc_info=True)
                continue
            for name in names:
                try:
                    cell = el.getCellByName(name)
                except Exception:
                    log.debug("math export: getCellByName(%r) failed", name, exc_info=True)
                    continue
                yield from _iter_body_paragraphs(cell)
            continue
        if hasattr(el, "createEnumeration"):
            yield el


class _MathExportBatch:
    """Reuses one hidden Math document across formulas in a single export run."""

    def __init__(self, ctx: Any) -> None:
        self.ctx: Any = ctx
        self._doc: Any = None
        self._temp_path: str | None = None
        self._temp_url: str | None = None

    def _ensure_doc(self) -> Any:
        if self._doc is None:
            from plugin.writer.format import create_property_value

            desktop = get_desktop(self.ctx)
            hidden = (create_property_value("Hidden", True),)
            self._doc = desktop.loadComponentFromURL(_MATH_FACTORY_URL, "_blank", 0, hidden)
            if self._doc is not None:
                fd, path = tempfile.mkstemp(suffix=".mml", prefix="writeragent-math-export-", text=False)
                os.close(fd)
                self._temp_path = path
                self._temp_url = file_url(path)
        return self._doc

    def convert_starmath_to_latex(self, starmath: str) -> MathExportResult:
        doc = self._ensure_doc()
        if doc is None or not self._temp_path or not self._temp_url:
            return convert_starmath_to_latex(self.ctx, starmath)
        from plugin.writer.format import create_property_value

        try:
            try:
                doc.setPropertyValue("Formula", starmath.strip())
            except Exception:
                doc.Formula = starmath.strip()
            props = (
                create_property_value("FilterName", MATHML_EXPORT_FILTER),
                create_property_value("Overwrite", True),
            )
            doc.storeToURL(self._temp_url, props)
            with open(self._temp_path, "r", encoding="utf-8", errors="replace") as f:
                raw = f.read()
            mml = math_root(raw)
            if not mml:
                return convert_starmath_to_latex(self.ctx, starmath)
            res = convert_mathml_to_latex(mml)
            if not res.ok:
                return convert_starmath_to_latex(self.ctx, starmath)
            return MathExportResult(True, res.latex, mml, None)
        except Exception:
            return convert_starmath_to_latex(self.ctx, starmath)

    def close(self) -> None:
        if self._doc is not None:
            try:
                self._doc.close(True)
            except Exception as exc:
                log.debug("math batch doc close: %s", exc)
            self._doc = None
        if self._temp_path:
            try:
                os.unlink(self._temp_path)
            except OSError:
                pass
            self._temp_path = None


def _build_hit(
    embed: Any,
    para_index: int,
    display_block: bool,
    ctx: Any | None,
    batch: _MathExportBatch | None = None,
) -> WriterMathHit:
    starmath = read_starmath(embed)
    latex = None
    mathml = None
    err = None
    if starmath and (batch is not None or ctx is not None):
        if batch is not None:
            res = batch.convert_starmath_to_latex(starmath)
        else:
            res = convert_starmath_to_latex(ctx, starmath)
        latex = res.latex
        mathml = res.mathml
        err = None if res.ok else res.error_message
    elif not starmath:
        err = "empty_formula_property"
    return WriterMathHit(
        starmath=starmath,
        latex=latex,
        mathml=mathml,
        error_message=err,
        display_block=display_block,
        para_index=para_index,
    )


def _math_hits_in_paragraph(
    para: Any,
    para_index: int,
    ctx: Any | None,
    batch: _MathExportBatch | None = None,
) -> list[WriterMathHit]:
    embeds: list[Any] = []
    non_ws_text = False
    try:
        portion_enum = para.createEnumeration()
    except Exception:
        return []
    while portion_enum.hasMoreElements() is True:
        portion = portion_enum.nextElement()
        emb = portion_math_embed(portion)
        if emb is not None:
            # Writer can report one as-character object in two portions; count it once.
            if not any(uno_same(emb, seen) for seen in embeds):
                embeds.append(emb)
            continue
        try:
            chunk = portion.getString() or ""
        except Exception:
            chunk = ""
        if chunk.strip() and chunk != _OBJECT_REPLACEMENT:
            non_ws_text = True

    display_block = bool(embeds) and not non_ws_text
    return [_build_hit(emb, para_index, display_block, ctx, batch) for emb in embeds]
