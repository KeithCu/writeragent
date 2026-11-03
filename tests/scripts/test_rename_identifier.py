from __future__ import annotations
# SPDX-License-Identifier: GPL-3.0-or-later
"""Unit tests for scripts/rename_identifier.py."""


from scripts.rename_identifier import rewrite_import_from, rewrite_text


def test_rewrite_identifier_and_attr_prefix():
    src = 'import plugin.scripting.calc_functions as xl\nassert xl.sumif(a, b)\ns = "xl.countif("\n'
    out = rewrite_text(src, "xl", "calc")
    assert out is not None
    assert "as calc" in out
    assert "calc.sumif" in out
    assert '"calc.countif("' in out
    assert "as xl" not in out


def test_rewrite_skips_openpyxl_and_xlws_lines():
    src = "from openpyxl.styles import Font\n_xlws.PY(0,1)\nxl.sumif(x)\n"
    out = rewrite_text(src, "xl", "calc")
    assert out is not None
    assert "openpyxl.styles" in out
    assert "_xlws.PY" in out
    assert "calc.sumif" in out


def test_rewrite_noop():
    assert rewrite_text("np.sum(data)\n", "xl", "calc") is None


def test_rewrite_import_from_exclusive():
    # Keep this fixture on document_helpers so the rewriter has something to move.
    src = "from plugin.doc.document_helpers import is_calc, is_writer\n"
    out = rewrite_import_from(
        src,
        "plugin.doc.document_helpers",
        "plugin.framework.doc_type",
        frozenset({"is_calc", "is_writer", "is_draw"}),
    )
    assert out == "from plugin.framework.doc_type import is_calc, is_writer\n"


def test_rewrite_import_from_mixed_split():
    src = (
        "from plugin.doc.document_helpers import get_document_property, is_calc, is_writer\n"
        "x = 1\n"
    )
    out = rewrite_import_from(
        src,
        "plugin.doc.document_helpers",
        "plugin.framework.doc_type",
        frozenset({"is_calc", "is_writer"}),
    )
    assert out is not None
    assert "from plugin.framework.doc_type import is_calc, is_writer\n" in out
    assert "from plugin.doc.document_helpers import get_document_property\n" in out
    assert "x = 1\n" in out


def test_rewrite_import_from_preserves_alias():
    src = "from plugin.doc.document_helpers import is_calc as _is_calc\n"
    out = rewrite_import_from(
        src,
        "plugin.doc.document_helpers",
        "plugin.framework.doc_type",
        frozenset({"is_calc"}),
    )
    assert out == "from plugin.framework.doc_type import is_calc as _is_calc\n"


def test_rewrite_import_from_noop_when_no_matching_names():
    src = "from plugin.doc.document_helpers import get_string_without_tracked_deletions\n"
    assert (
        rewrite_import_from(
            src,
            "plugin.doc.document_helpers",
            "plugin.framework.doc_type",
            frozenset({"is_calc"}),
        )
        is None
    )


def test_rewrite_import_from_string_literal():
    src = '    "from plugin.doc.document_helpers import is_calc, is_writer, is_draw",\n'
    out = rewrite_import_from(
        src,
        "plugin.doc.document_helpers",
        "plugin.framework.doc_type",
        frozenset({"is_calc", "is_writer", "is_draw"}),
    )
    assert out == '    "from plugin.framework.doc_type import is_calc, is_writer, is_draw",\n'
