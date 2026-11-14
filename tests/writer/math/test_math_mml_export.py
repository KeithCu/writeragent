# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later

from unittest.mock import patch

import pytest

from plugin.framework.errors import DocumentDisposedError
from plugin.writer.math.math_mml_export import (
    MathExportResult,
    convert_mathml_to_latex,
    inject_math_tex_into_html,
    wrap_latex_delimiters,
)

class _Hit:
    def __init__(
        self, display_block: bool = False, embed_name: str | None = None
    ) -> None:
        self.starmath = "a over b"
        self.display_block = display_block
        self.embed_name = embed_name


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
    with patch.object(exp, "iter_writer_math_objects", return_value=[_Hit(display_block=True)]):
        out = inject_math_tex_into_html(object(), object(), html)
    assert out.count("$$x$$") == 1
    assert "<math" not in out.lower()
    assert out.count("$") == 4  # $$ … $$


def test_inject_count_mismatch_does_not_append() -> None:
    from plugin.writer.math import math_mml_export as exp

    html = "<p><math xmlns='http://www.w3.org/1998/Math/MathML'><mi>a</mi></math></p>"
    hits = [_Hit(), _Hit()]
    with patch.object(exp, "iter_writer_math_objects", return_value=hits):
        out = inject_math_tex_into_html(object(), object(), html)
    assert "$a$" in out
    assert "$b$" not in out
    assert out.rstrip().endswith("</p>") or out.rstrip().endswith("</P>")


def test_inject_converts_xhtml_math_without_walker_hits() -> None:
    from plugin.writer.math import math_mml_export as exp

    html = (
        '<p><math xmlns="http://www.w3.org/1998/Math/MathML">'
        "<mfrac><mi>a</mi><mn>2</mn></mfrac>"
        "</math></p>"
    )
    with patch.object(exp, "iter_writer_math_objects", return_value=[]):
        out = inject_math_tex_into_html(object(), object(), html)
    assert "<math" not in out.lower()
    assert "frac" in out.lower()
    assert out.count("$") == 2


def test_inject_falls_back_to_starmath_when_mathml_fails() -> None:
    from plugin.writer.math import math_mml_export as exp

    html = "<p><math xmlns='http://www.w3.org/1998/Math/MathML'><mi>x</mi></math></p>"
    hit = _Hit()
    hit.starmath = "a over b"
    with patch.object(exp, "iter_writer_math_objects", return_value=[hit]), patch.object(
        exp, "convert_mathml_to_latex", return_value=MathExportResult(False, None, "boom")
    ), patch.object(
        exp.MathExportBatch, "convert_starmath_to_latex", return_value=MathExportResult(True, "y", None)
    ):
        out = inject_math_tex_into_html(object(), object(), html)
    assert "$y$" in out


def test_xhtml_id_mirrors_export_sanitizer() -> None:
    from plugin.writer.math.math_mml_export import _xhtml_id

    assert _xhtml_id("Object 1") == "Object_1"
    assert _xhtml_id("2x") == "a_2x"
    assert _xhtml_id("Object_1") == "Object_1"


def test_inject_pairs_display_flag_by_wrapper_id() -> None:
    from plugin.writer.math import math_mml_export as exp

    html = (
        "<p><math xmlns='http://www.w3.org/1998/Math/MathML'><mi>a</mi></math></p>\n"
        '<span id="Object_2"><math xmlns=\'http://www.w3.org/1998/Math/MathML\'><mi>b</mi></math></span>\n'
        "<p><math xmlns='http://www.w3.org/1998/Math/MathML'><mi>c</mi></math></p>"
    )
    hits = [
        _Hit(display_block=True, embed_name="Object 9"),
        _Hit(display_block=True, embed_name="Object 2"),
    ]
    with patch.object(exp, "iter_writer_math_objects", return_value=hits):
        out = inject_math_tex_into_html(object(), object(), html)
    assert "$a$" in out
    assert "$$b$$" in out
    assert "$c$" in out


def test_substitute_uses_one_batch() -> None:
    from plugin.writer.math import math_mml_export as exp

    records = [
        (chr(0xE000), "a + b", False),
        (chr(0xE001), "x", True),
    ]
    html = f"<p>{chr(0xE000)} and {chr(0xE001)}</p>"

    instances = []

    class FakeBatch:
        def __init__(self, ctx: object) -> None:
            self.calls: list[str] = []
            self.closed = 0
            instances.append(self)

        def __enter__(self) -> "FakeBatch":
            return self

        def __exit__(self, exc_type: object, exc: object, tb: object) -> bool:
            self.close()
            return False

        def convert_starmath_to_latex(self, starmath: str) -> exp.MathExportResult:
            self.calls.append(starmath)
            return exp.MathExportResult(True, starmath, None)

        def close(self) -> None:
            self.closed += 1

    with patch.object(exp, "MathExportBatch", FakeBatch):
        out = exp.substitute_math_placeholders(html, records, object())

    assert len(instances) == 1
    assert instances[0].calls == ["a + b", "x"]
    assert instances[0].closed == 1
    assert "$a + b$" in out
    assert "$$x$$" in out


def test_math_export_batch_converts_repeated_formula_once() -> None:
    from unittest.mock import MagicMock
    from plugin.writer.math import math_mml_export as exp

    fake_doc = MagicMock()

    def fake_store(url: str, props: object) -> None:
        with open(url, "w", encoding="utf-8") as f:
            f.write('<math xmlns="http://www.w3.org/1998/Math/MathML"><mi>x</mi></math>')

    fake_doc.storeToURL.side_effect = fake_store
    fake_desktop = MagicMock()
    fake_desktop.loadComponentFromURL.return_value = fake_doc

    with patch.object(exp, "get_desktop", return_value=fake_desktop), patch.object(
        exp, "path_to_file_url", side_effect=lambda path: path
    ):
        with exp.MathExportBatch(object()) as batch:
            res1 = batch.convert_starmath_to_latex("x")
            res2 = batch.convert_starmath_to_latex("x")

    assert res1.ok and res2.ok
    assert res1.latex == "x" and res2.latex == "x"
    assert fake_desktop.loadComponentFromURL.call_count == 1
    assert fake_doc.storeToURL.call_count == 1


def test_math_export_batch_failure_does_not_open_second_doc() -> None:
    from unittest.mock import MagicMock
    from plugin.writer.math import math_mml_export as exp

    fake_doc = MagicMock()
    fake_doc.storeToURL.side_effect = RuntimeError("store failed")
    fake_desktop = MagicMock()
    fake_desktop.loadComponentFromURL.return_value = fake_doc

    with patch.object(exp, "get_desktop", return_value=fake_desktop), patch.object(
        exp, "path_to_file_url", side_effect=lambda path: path
    ):
        with exp.MathExportBatch(object()) as batch:
            res = batch.convert_starmath_to_latex("x")

    assert not res.ok
    assert "store failed" in (res.error_message or "")
    # Exactly one hidden document was opened for the batch, none on failure
    assert fake_desktop.loadComponentFromURL.call_count == 1


def test_substitute_math_failure_is_not_tex() -> None:
    from unittest.mock import MagicMock
    from plugin.writer.math import math_mml_export as exp

    fake_desktop = MagicMock()
    fake_desktop.loadComponentFromURL.return_value = None

    with patch.object(exp, "get_desktop", return_value=fake_desktop):
        out = exp.substitute_math_placeholders(
            "<p>\ue000</p>", [(chr(0xE000), "x^", False)], object()
        )
    assert "$" not in out
    assert 'data-math-export-error="' in out
    assert 'data-math-export-error="smath_factory_returned_none"' in out
    assert "x^" in out


def test_inject_no_math_leaves_html() -> None:
    from plugin.writer.math import math_mml_export as exp

    html = '<div class="paragraph-Standard">prose</div>'
    with patch.object(exp, "iter_writer_math_objects") as mock_iter:
        out = inject_math_tex_into_html(object(), object(), html)
    mock_iter.assert_not_called()
    assert out == html


def test_portion_math_embed_and_read_starmath() -> None:
    from unittest.mock import MagicMock
    from plugin.writer.math.math_mml_convert import MATH_CLSID
    from plugin.writer.math.math_mml_export import portion_math_embed, read_starmath

    class _Enum:
        def __init__(self, items: list[object]) -> None:
            self._items = list(items)

        def hasMoreElements(self) -> bool:
            return bool(self._items)

        def nextElement(self) -> object:
            return self._items.pop(0)

    embed = MagicMock()
    embed.CLSID = MATH_CLSID
    embed.getEmbeddedObject.return_value.Formula = "x + 1"

    frame_portion = MagicMock()
    frame_portion.getPropertyValue.return_value = "Frame"
    frame_portion.createContentEnumeration.return_value = _Enum([embed])

    assert portion_math_embed(frame_portion) is embed
    assert read_starmath(embed) == "x + 1"

    text_portion = MagicMock()
    text_portion.getPropertyValue.return_value = "Text"
    assert portion_math_embed(text_portion) is None
    text_portion.createContentEnumeration.assert_not_called()


def test_inject_exported_math_tex_raises_on_disposed_document() -> None:
    from unittest.mock import MagicMock
    from plugin.writer.math.math_mml_export import inject_exported_math_tex

    class DisposedException(Exception):
        pass

    model = MagicMock()
    model.getText.return_value.createEnumeration.side_effect = DisposedException("disposed")
    html = "<p><math xmlns='http://www.w3.org/1998/Math/MathML'><mi>x</mi></math></p>"

    with pytest.raises(DocumentDisposedError):
        inject_exported_math_tex(model, object(), html)


def test_read_starmath_raises_on_disposed_embed() -> None:
    from unittest.mock import MagicMock
    from plugin.writer.math.math_mml_export import read_starmath

    class DisposedException(Exception):
        pass

    embed = MagicMock()
    embed.getEmbeddedObject.side_effect = DisposedException("gone")

    with pytest.raises(DocumentDisposedError):
        read_starmath(embed)
