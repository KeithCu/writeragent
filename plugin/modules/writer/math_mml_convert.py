# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Convert MathML fragments to LibreOffice StarMath using the Office MathML importer."""

from __future__ import annotations

import logging
import os
import re
import tempfile
from dataclasses import dataclass
from typing import Any, cast

import uno

from plugin.framework.uno_context import get_desktop

log = logging.getLogger("writeragent.writer")


@dataclass(frozen=True)
class MathConversionResult:
    ok: bool
    starmath: str | None
    error_message: str | None


def _file_url(path: str) -> str:
    return uno.systemPathToFileUrl(os.path.abspath(path))


def _create_property_value(name: str, value: Any) -> Any:
    p = cast("Any", uno.createUnoStruct("com.sun.star.beans.PropertyValue"))
    p.Name = name
    p.Value = value
    return p


def collapse_starmath_newline_tokens_for_writer_embed(starmath: str) -> str:
    """Remove StarMath ``newline`` keywords so Writer can paint the formula.

    LibreOffice's MathML importer builds a root ``SmTableNode`` (lines as rows;
    see ``starmath/source/mathml/mathmlimport.cxx`` → ``SmXMLDocContext_Impl``).
    When that tree is turned back into text, ``SmNodeToTextVisitor`` inserts the
    StarMath **operator** spelled ``newline`` between rows (``visitors.cxx``).

    That token is legitimate in standalone LibreOffice Math, but **embedded**
    formulas in Writer often draw each ``newline`` node as a missing-glyph
    placeholder (``?``) on the page, while the Math editor still shows the word
    ``newline`` in the command text. Users never authored ``<mtable>``; this
    comes entirely from LO's internal representation, not from WriterAgent.

    For our HTML→Math insert path we only need single-line, in-flow formulas, so
    we collapse each ``newline`` token (and surrounding whitespace) to a single
    space and normalize runs of spaces.
    """
    s = re.sub(r"\s*\bnewline\b\s*", " ", starmath)
    s = re.sub(r" {2,}", " ", s).strip()
    return s


def _debug_newline_stats(prefix: str, s: str, *, max_repr: int = 400) -> None:
    """Emit one debug line: newline counts and a short repr (for diagnosis)."""
    if not log.isEnabledFor(logging.DEBUG):
        return
    head = s[:max_repr]
    tail = " …[truncated]" if len(s) > max_repr else ""
    log.debug(
        "%s len=%d nl=%d cr=%d crlf=%d tab=%d repr_head=%s%s",
        prefix,
        len(s),
        s.count("\n"),
        s.count("\r"),
        len(re.findall(r"\r\n", s)),
        s.count("\t"),
        repr(head),
        tail,
    )


def convert_latex_to_starmath(
    ctx: Any, latex: str, *, display_block: bool = False
) -> MathConversionResult:
    """Convert LaTeX to StarMath via ``latex2mathml`` then :func:`convert_mathml_to_starmath`."""
    if not latex or not isinstance(latex, str):
        return MathConversionResult(False, None, "empty_latex")
    trimmed = latex.strip()
    if not trimmed:
        return MathConversionResult(False, None, "empty_latex")
    try:
        from latex2mathml.converter import convert as latex2mathml_convert
    except ImportError as exc:
        return MathConversionResult(False, None, f"latex2mathml_import:{exc}")
    display_mode = "block" if display_block else "inline"
    try:
        mathml = latex2mathml_convert(trimmed, display=display_mode)
    except Exception as exc:
        log.debug("latex2mathml convert failed: %s", exc, exc_info=True)
        return MathConversionResult(False, None, str(exc))
    if not isinstance(mathml, str) or not mathml.strip():
        return MathConversionResult(False, None, "latex2mathml_empty_output")
    return convert_mathml_to_starmath(ctx, mathml.strip())


def convert_mathml_to_starmath(ctx: Any, mathml_fragment: str) -> MathConversionResult:
    """Load a MathML document in LibreOffice Math and read the ``Formula`` string.

    *mathml_fragment* should be a well-formed ``<math>...</math>`` subtree (may
    include an XML declaration — not required).
    """
    if not mathml_fragment or not isinstance(mathml_fragment, str):
        return MathConversionResult(False, None, "empty_mathml")
    text = mathml_fragment.strip()
    if not text.lower().startswith("<math"):
        return MathConversionResult(False, None, "not_math_root")

    _debug_newline_stats("convert_mathml_to_starmath: input MathML fragment", text)

    fd, path = tempfile.mkstemp(suffix=".mml", prefix="writeragent-math-", text=False)
    os.close(fd)
    try:
        payload = text
        if not text.lstrip().lower().startswith("<?xml"):
            payload = '<?xml version="1.0" encoding="UTF-8"?>\n' + text
        _debug_newline_stats(
            "convert_mathml_to_starmath: .mml payload written to temp file", payload
        )
        with open(path, "w", encoding="utf-8") as f:
            f.write(payload)

        desktop = get_desktop(ctx)
        hidden = (_create_property_value("Hidden", True),)
        url = _file_url(path)
        doc = desktop.loadComponentFromURL(url, "_blank", 0, hidden)
        if doc is None:
            return MathConversionResult(False, None, "load_returned_none")
        try:
            formula = doc.getPropertyValue("Formula")
            if not isinstance(formula, str) or not formula.strip():
                return MathConversionResult(False, None, "empty_formula_property")
            _formula_stripped = formula.strip()
            _debug_newline_stats(
                "convert_mathml_to_starmath: Formula from LO (before return.strip)",
                formula,
                max_repr=500,
            )
            _for_writer = collapse_starmath_newline_tokens_for_writer_embed(
                _formula_stripped
            )
            if _for_writer != _formula_stripped:
                log.debug(
                    "convert_mathml_to_starmath: collapsed newline operators for "
                    "Writer embed (len %d -> %d)",
                    len(_formula_stripped),
                    len(_for_writer),
                )
            return MathConversionResult(True, _for_writer, None)
        finally:
            try:
                doc.close(True)
            except Exception as exc:
                log.debug("math doc close: %s", exc)
    except Exception as exc:
        log.debug("convert_mathml_to_starmath failed: %s", exc, exc_info=True)
        return MathConversionResult(False, None, str(exc))
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass
