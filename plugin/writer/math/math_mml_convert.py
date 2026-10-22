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
from typing import Any, Iterator

import uno

from plugin.framework.uno_context import get_desktop

log = logging.getLogger("writeragent.writer")


# Same CLSID as documented in LibreOffice programming guides (FormulaDocument).
MATH_CLSID = "078B7ABA-54FC-457F-8551-6147e776a997"

# com.sun.star.text.ControlCharacter.PARAGRAPH_BREAK
_PARAGRAPH_BREAK = 0

# com.sun.star.text.TextContentAnchorType.AS_CHARACTER (inline / in-flow)
_ANCHOR_AS_CHARACTER = 1

# com.sun.star.style.ParagraphAdjust (CENTER=3, LEFT=0)
_PARA_CENTER = 3
_PARA_LEFT = 0


@dataclass(frozen=True)
class MathConversionResult:
    ok: bool
    starmath: str | None
    error_message: str | None


_MATH_ROOT_RE = re.compile(r"(?is)<math[\s>][\s\S]*?</math>")


def math_root(fragment: str) -> str | None:
    """Extract a <math>...</math> root element from MathML markup."""
    if not fragment or not isinstance(fragment, str):
        return None
    text = fragment.strip()
    m = _MATH_ROOT_RE.search(text)
    return m.group(0) if m else None


def iter_math_roots(html: str) -> Iterator[re.Match[str]]:
    """Yield every <math>...</math> root element match from MathML markup."""
    if not html or not isinstance(html, str):
        return
    yield from _MATH_ROOT_RE.finditer(html)


def _exception_message(exc: Exception) -> str:
    """Non-empty diagnostic for UNO / latex2mathml failures (some bridges return ``str(exc)==''``)."""
    name = type(exc).__name__
    raw = getattr(exc, "Message", None) or str(exc)
    text = raw.strip() if isinstance(raw, str) else ""
    return f"{name}: {text}" if text else name


def file_url(path: str) -> str:
    return uno.systemPathToFileUrl(os.path.abspath(path))



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


def convert_latex_to_starmath(ctx: Any, latex: str, *, display_block: bool = False) -> MathConversionResult:
    """Convert LaTeX to StarMath via ``latex2mathml`` then :func:`convert_mathml_to_starmath`."""
    assert isinstance(latex, str), type(latex)
    if not latex:
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
        return MathConversionResult(False, None, _exception_message(exc))
    assert isinstance(mathml, str)
    if not mathml.strip():
        return MathConversionResult(False, None, "latex2mathml_empty_output")
    return convert_mathml_to_starmath(ctx, mathml.strip())


def convert_mathml_to_starmath(ctx: Any, mathml_fragment: str) -> MathConversionResult:
    """Load a MathML document in LibreOffice Math and read the ``Formula`` string.

    *mathml_fragment* should be a well-formed ``<math>...</math>`` subtree (may
    include an XML declaration — not required).
    """
    if not mathml_fragment or not isinstance(mathml_fragment, str):
        return MathConversionResult(False, None, "empty_mathml")
    root = math_root(mathml_fragment)
    if not root:
        return MathConversionResult(False, None, "not_math_root")

    fd, path = tempfile.mkstemp(suffix=".mml", prefix="writeragent-math-", text=False)
    os.close(fd)
    try:
        payload = '<?xml version="1.0" encoding="UTF-8"?>\n' + root
        with open(path, "w", encoding="utf-8") as f:
            f.write(payload)

        from plugin.writer.format import create_property_value
        desktop = get_desktop(ctx)
        hidden = (create_property_value("Hidden", True),)
        url = file_url(path)
        doc = desktop.loadComponentFromURL(url, "_blank", 0, hidden)
        if doc is None:
            return MathConversionResult(False, None, "load_returned_none")
        try:
            formula = doc.getPropertyValue("Formula")
            assert isinstance(formula, str)
            if not formula.strip():
                return MathConversionResult(False, None, "empty_formula_property: LibreOffice Math opened the file but returned no formula text (unsupported or invalid MathML for this build).")
            _formula_stripped = formula.strip()
            _for_writer = collapse_starmath_newline_tokens_for_writer_embed(_formula_stripped)
            if _for_writer != _formula_stripped:
                log.debug("convert_mathml_to_starmath: collapsed newline operators for Writer embed (len %d -> %d)", len(_formula_stripped), len(_for_writer))
            return MathConversionResult(True, _for_writer, None)
        finally:
            try:
                doc.close(True)
            except Exception as exc:
                log.debug("math doc close: %s", exc)
    except Exception as exc:
        log.debug("convert_mathml_to_starmath failed: %s", exc, exc_info=True)
        return MathConversionResult(False, None, _exception_message(exc))
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass



def insert_writer_math_formula(model: Any, cursor: Any, starmath: str, *, display_block: bool) -> None:
    """Insert a Writer formula object at *cursor* and leave the cursor after it.

    *display_block*: surround the object with paragraph breaks so it occupies
    its own paragraph (display-style math) and center the formula paragraph.
    """
    # The cursor's own text: a formula in a frame, cell or header goes into that text, not the body.
    text = cursor.getText()
    # insertControlCharacter already leaves the cursor after the
    # paragraph break. A following goRight(1) is a no-op at the end of
    # the text (the only place display math used to land, while inserts
    # jumped to the body end); mid-paragraph it skips a character:
    # "AAA|BBB" becomes "AAA" / "B[F]" / "BB", with the next segment
    # before the formula (checked live). Without the extra step:
    # "AAA" / "[F]" / "BBB".
    if display_block:
        text.insertControlCharacter(cursor, _PARAGRAPH_BREAK, False)
        cursor.setPropertyValue("ParaAdjust", _PARA_CENTER)

    embed = model.createInstance("com.sun.star.text.TextEmbeddedObject")
    embed.CLSID = MATH_CLSID
    embed.AnchorType = _ANCHOR_AS_CHARACTER
    text.insertTextContent(cursor, embed, False)

    inner = embed.getEmbeddedObject()
    inner.Formula = starmath

    if display_block:
        text.insertControlCharacter(cursor, _PARAGRAPH_BREAK, False)
        cursor.setPropertyValue("ParaAdjust", _PARA_LEFT)


def replace_writer_math_formula(embed: Any, starmath: str) -> None:
    """Set StarMath on an existing Writer formula object.

    Must not insert a second embed or paragraph breaks: doing that while the
    OLE is selected is a native LibreOffice crash (no Python traceback).
    """
    if not starmath.strip():
        raise ValueError("empty_starmath")
    inner = embed.getEmbeddedObject() if hasattr(embed, "getEmbeddedObject") else embed
    if inner is None or not hasattr(inner, "Formula"):
        raise ValueError("not_a_formula_object")
    inner.Formula = starmath.strip()

