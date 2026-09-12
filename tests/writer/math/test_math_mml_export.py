# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later

from unittest.mock import patch

from plugin.writer.math.math_mml_export import (
    MathExportResult,
    convert_mathml_to_latex,
    inject_math_tex_into_html,
    wrap_latex_delimiters,
)

class _Hit:
    def __init__(self, latex: str, display_block: bool = False) -> None:
        self.starmath = "a over b"
        self.latex = latex
        self.mathml = None
        self.error_message = None
        self.display_block = display_block
        self.para_index = 0


def test_convert_mathml_to_latex_fraction() -> None:
    mml = (
        '<math xmlns="http://www.w3.org/1998/Math/MathML">'
        "<mfrac><mi>a</mi><mi>b</mi></mfrac>"
        "</math>"
    )
    res = convert_mathml_to_latex(mml)
    assert res.ok, res.error_message
    assert res.latex is not None
    assert "frac" in res.latex.lower()
    assert "a" in res.latex and "b" in res.latex


def test_convert_mathml_to_latex_sqrt() -> None:
    mml = (
        '<math xmlns="http://www.w3.org/1998/Math/MathML">'
        "<msqrt><mi>x</mi></msqrt>"
        "</math>"
    )
    res = convert_mathml_to_latex(mml)
    assert res.ok, res.error_message
    assert res.latex is not None
    assert "sqrt" in res.latex.lower()


def test_empty_and_non_math_root() -> None:
    assert convert_mathml_to_latex("").ok is False
    assert convert_mathml_to_latex("not math").ok is False
    assert convert_mathml_to_latex("<div>x</div>").ok is False


def test_lossy_roundtrip_via_latex2mathml() -> None:
    from latex2mathml.converter import convert as latex2mathml_convert

    mml = latex2mathml_convert(r"\frac{1}{2}")
    res = convert_mathml_to_latex(mml)
    assert res.ok, res.error_message
    assert res.latex is not None
    assert "frac" in res.latex.lower()
    assert "1" in res.latex and "2" in res.latex


def test_wrap_delimiters() -> None:
    assert wrap_latex_delimiters(r"\frac{1}{2}", display_block=False) == r"$\frac{1}{2}$"
    assert wrap_latex_delimiters(r"\frac{1}{2}", display_block=True) == r"$$\frac{1}{2}$$"


def test_inject_replaces_mathml_once() -> None:
    from plugin.writer.math import math_mml_export as exp

    html = (
        '<!--Next \'div\' was a \'text:p\'.-->\n'
        '<div class="paragraph-Standard">\n'
        '<!--Next \'span\' is a draw:frame. -->\n'
        '<span id="Object1"><math xmlns="http://www.w3.org/1998/Math/MathML" display="inline">'
        "<mrow><mi>x</mi></mrow>"
        "</math> </span></div>"
    )
    with patch.object(exp, "iter_writer_math_objects", return_value=[_Hit("x", True)]):
        out = inject_math_tex_into_html(object(), object(), html)
    assert out.count("$$x$$") == 1
    assert "<math" not in out.lower()
    assert out.count("$") == 4  # $$ … $$


def test_inject_count_mismatch_does_not_append() -> None:
    from plugin.writer.math import math_mml_export as exp

    html = "<p><math xmlns='http://www.w3.org/1998/Math/MathML'><mi>a</mi></math></p>"
    hits = [_Hit("a"), _Hit("b")]
    with patch.object(exp, "iter_writer_math_objects", return_value=hits), patch.object(
        exp.log, "error"
    ) as err:
        out = inject_math_tex_into_html(object(), object(), html)
    err.assert_called()
    assert "$a$" in out
    assert "$b$" not in out
    assert out.rstrip().endswith("</p>") or out.rstrip().endswith("</P>")


def test_inject_no_math_leaves_html() -> None:
    from plugin.writer.math import math_mml_export as exp

    html = '<div class="paragraph-Standard">prose</div>'
    with patch.object(exp, "iter_writer_math_objects", return_value=[_Hit("x")]), patch.object(
        exp.log, "error"
    ) as err:
        out = inject_math_tex_into_html(object(), object(), html)
    err.assert_called()
    assert out == html


def test_math_export_result_shape() -> None:
    r = MathExportResult(False, None, None, "empty_mathml")
    assert r.ok is False


def test_math_mml_export_uno_skips_windows_leftover_hidden_smath() -> None:
    """GHA 34678020608: leftover Hidden _blank smath / .mml hang."""
    from pathlib import Path

    src = Path(__file__).with_name("test_math_mml_export_uno.py").read_text(encoding="utf-8")
    assert "skip_windows_leftover_hidden_mathml" in src
    assert "math export Hidden _blank smath" in src
