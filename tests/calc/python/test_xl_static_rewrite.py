# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for save-time ``xl("A1")`` → polymorphic ``data`` / ``data[i]`` rewrite."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from plugin.calc.python.editor import _apply_cell_save, _maybe_apply_xl_static_rewrite
from plugin.calc.python.formula_edit import parse_python_formula
from plugin.calc.python.xl_static_rewrite import (
    apply_xl_static_rewrite,
    is_static_a1_literal,
)
from plugin.tests.testing_utils import CalcCellStub, CalcDocStub


def test_is_static_a1_literal_accepts_common_forms():
    assert is_static_a1_literal("A1")
    assert is_static_a1_literal("A1:A10")
    assert is_static_a1_literal("$A$1:$B$10")
    assert is_static_a1_literal("Sheet1.A1:B2")
    assert is_static_a1_literal("'My Sheet'.A1")
    assert is_static_a1_literal("Sheet1!A1:A2")


def test_apply_xl_static_rewrite_null_byte_is_issue_not_crash():
    """CPython rejects NUL in source (ValueError); must not leak TypeError/ValueError."""
    res = apply_xl_static_rewrite("\x00")
    assert res.changed is False
    assert res.issues
    assert "not parseable" in res.issues[0] or "syntax" in res.issues[0].lower()


def test_is_static_a1_literal_rejects_non_addresses():
    assert not is_static_a1_literal("%P2%")
    assert not is_static_a1_literal("Table1")
    assert not is_static_a1_literal("")
    assert not is_static_a1_literal("not a range")


def test_is_static_a1_literal_rejects_invalid_and_out_of_bounds():
    assert not is_static_a1_literal("A0")
    assert not is_static_a1_literal("XFE1")
    assert not is_static_a1_literal("A1048577")
    assert not is_static_a1_literal("ABCD1")


def test_is_static_a1_literal_accepts_calc_absolute_sheet():
    assert is_static_a1_literal("$Sheet1.$A$1")


def test_rewrite_single_range_headers_true():
    result = apply_xl_static_rewrite('df = xl("A1:A10", headers=True)')
    assert result.changed
    assert result.issues == []
    assert result.data_args == ["A1:A10"]
    assert result.code == "df = data.to_pandas()"


def test_rewrite_headers_false_and_omit_multi():
    code = 'a = xl("A1")\nb = xl("B1:B2", headers=False)\n'
    result = apply_xl_static_rewrite(code)
    assert result.changed
    assert result.data_args == ["A1", "B1:B2"]
    assert "a = data[0]\n" in result.code
    assert "b = data[1].to_pandas(header_row=None)" in result.code


def test_rewrite_dedups_identical_addresses():
    code = 'x = xl("A1:A10") + xl("A1:A10")'
    result = apply_xl_static_rewrite(code)
    assert result.data_args == ["A1:A10"]
    assert result.code == "x = data + data"


def test_rewrite_keeps_existing_data_args_first():
    result = apply_xl_static_rewrite('x = xl("C1")', existing_data_args=["A1:A5"])
    assert result.data_args == ["A1:A5", "C1"]
    assert result.code == "x = data[1]"


def test_rewrite_reuses_existing_matching_address():
    result = apply_xl_static_rewrite(
        'x = xl("$A$1:$A$5")',
        existing_data_args=["A1:A5"],
    )
    assert result.data_args == ["A1:A5"]
    assert result.code == "x = data"


def test_rewrite_leaves_percent_p_tokens_alone():
    code = 'x = xl("%P2%")\ny = xl("A1")\n'
    result = apply_xl_static_rewrite(code)
    assert result.changed
    assert 'xl("%P2%")' in result.code
    assert "y = data" in result.code


def test_rewrite_does_not_touch_user_data_when_bindings_grow_from_two():
    result = apply_xl_static_rewrite('x = xl("C1")\nresult = len(data)\n', existing_data_args=["A1", "B1"])
    assert result.data_args == ["A1", "B1", "C1"]
    assert result.code == "x = data[2]\nresult = len(data)\n"


def test_rewrite_skips_migration_when_data_is_bound():
    result = apply_xl_static_rewrite('def f(data):\n    return data\nx = xl("C1")', existing_data_args=["A1"])
    assert result.code == 'def f(data):\n    return data\nx = data[1]'


@pytest.mark.parametrize(
    "snippet",
    [
        "g = lambda data: data",
        "xs = [data for data in []]",
        "data = 1",
        "def f():\n    global data\n    data = 2",
        "import data",
        "class data:\n    pass",
    ],
)
def test_rewrite_skips_migration_when_data_is_shadowed_or_bound(snippet: str):
    code = snippet + "\n\n" + 'x = xl("C1")'
    result = apply_xl_static_rewrite(code, existing_data_args=["A1"])
    assert not result.issues
    assert snippet in result.code
    assert "x = data[1]" in result.code


def test_rewrite_leaves_user_defined_xl_alone():
    code = 'def xl(ref):\n    return ref\nx = xl("A1")'
    result = apply_xl_static_rewrite(code)
    assert result.changed is False
    assert result.issues == []
    assert result.code == code


def test_rewrite_is_idempotent():
    first = apply_xl_static_rewrite('x = xl("A1")\ny = xl("B1")')
    second = apply_xl_static_rewrite(first.code, existing_data_args=first.data_args)
    assert second.code == first.code
    assert second.data_args == first.data_args


def test_rewrite_rejects_extra_positional_xl_args():
    code = 'x = xl("A1", True)'
    result = apply_xl_static_rewrite(code)
    assert not result.changed
    assert result.code == code
    assert any("positional" in i for i in result.issues)


def test_rewrite_rejects_unknown_xl_keyword():
    code = 'x = xl("A1", foo=1)'
    result = apply_xl_static_rewrite(code)
    assert not result.changed
    assert result.code == code
    assert result.issues != []


def test_rewrite_rejects_star_kwargs():
    code = 'x = xl("A1", **kw)'
    result = apply_xl_static_rewrite(code)
    assert not result.changed
    assert result.code == code
    assert result.issues != []


def test_rewrite_rejects_non_constant_headers():
    code = 'x = xl("A1", headers=flag)'
    result = apply_xl_static_rewrite(code)
    assert not result.changed
    assert result.code == code
    assert result.issues != []


def test_rewrite_canonicalizes_case_and_keeps_first_spelling():
    res1 = apply_xl_static_rewrite('x = xl("A1")\ny = xl("a1")')
    assert res1.data_args == ["A1"]
    assert res1.code == "x = data\ny = data"
    res2 = apply_xl_static_rewrite('x = xl("SHEET1.A1")\ny = xl("Sheet1.A1")')
    assert res2.data_args == ["SHEET1.A1"]
    res3 = apply_xl_static_rewrite('x = xl("$A$1")\ny = xl("A1")')
    assert res3.data_args == ["$A$1"]
    res4 = apply_xl_static_rewrite('x = xl("$Sheet1.$A$1")', existing_data_args=["Sheet1.A1"])
    assert res4.data_args == ["Sheet1.A1"]
    assert res4.code == "x = data"


def test_rewrite_keeps_sheets_that_differ_apart():
    res = apply_xl_static_rewrite("x = xl(\"'A!B'!C1\")\ny = xl(\"'A.B'!C1\")")
    assert len(res.data_args) == 2
    assert "x = data[0]" in res.code
    assert "y = data[1]" in res.code
    res_sep = apply_xl_static_rewrite('x = xl("Sheet1!A1")\ny = xl("Sheet1.A1")')
    assert res_sep.data_args == ["Sheet1!A1"]


def test_rewrite_collapses_duplicate_existing_data_args():
    res = apply_xl_static_rewrite('x = xl("C1")', existing_data_args=["a1", "A1"])
    assert res.data_args == ["a1", "C1"]
    assert res.code == "x = data[1]"


def test_rewrite_collapses_single_cell_range():
    res = apply_xl_static_rewrite('x = xl("A1:A1")', existing_data_args=["A1"])
    assert res.data_args == ["A1"]
    assert res.code == "x = data"


def test_rewrite_one_to_many_migrates_prior_data():
    # Prior sugar left bare data; adding a second xl grows bindings.
    code = 'df = data.to_pandas()\nx = xl("C1")\n'
    result = apply_xl_static_rewrite(code, existing_data_args=["A1:A10"])
    assert result.data_args == ["A1:A10", "C1"]
    assert "df = data[0].to_pandas()" in result.code
    assert "x = data[1]" in result.code


def test_rewrite_fail_closed_on_dynamic():
    result = apply_xl_static_rewrite('x = xl(f"A1:A{n}")')
    assert not result.changed
    assert any("dynamic" in i for i in result.issues)


def test_rewrite_fail_closed_on_named_table():
    result = apply_xl_static_rewrite('x = xl("Table1")')
    assert not result.changed
    assert result.issues


def test_rewrite_no_xl_unchanged():
    result = apply_xl_static_rewrite("result = np.sum(data)", existing_data_args=["A1"])
    assert not result.changed
    assert result.code == "result = np.sum(data)"
    assert result.data_args == ["A1"]


def test_maybe_apply_skipped_when_flag_off():
    with patch("plugin.calc.python.editor.get_config", return_value=False):
        out = _maybe_apply_xl_static_rewrite('df = xl("A1:A10", headers=True)', "")
    assert out == ('df = xl("A1:A10", headers=True)', "")


def test_maybe_apply_rewrites_when_flag_on():
    with patch("plugin.calc.python.editor.get_config", return_value=True):
        out = _maybe_apply_xl_static_rewrite('df = xl("A1:A10", headers=True)', "")
    assert not isinstance(out, dict)
    code, binding = out
    assert code == "df = data.to_pandas()"
    assert binding == "A1:A10"


def test_maybe_apply_errors_on_dynamic_when_flag_on():
    with patch("plugin.calc.python.editor.get_config", return_value=True):
        out = _maybe_apply_xl_static_rewrite('x = xl(name)', "")
    assert isinstance(out, dict)
    assert out["type"] == "error"


def test_apply_cell_save_lifts_xl_when_flag_on():
    doc = CalcDocStub()
    cell = CalcCellStub()
    with patch("plugin.calc.python.editor.get_config", return_value=True):
        result = _apply_cell_save(
            doc,
            cell,
            parsed_parts=None,
            new_code='df = xl("D1:D10", headers=True)\nresult = len(df)',
            save_as_plain=False,
            data_binding_text="",
        )
    assert result["ok"] is True
    formula = cell.getFormula()
    assert "D1:D10" in formula
    assert "data.to_pandas()" in formula
    assert "xl(" not in formula


def test_apply_cell_save_merges_with_parsed_data_suffix():
    doc = CalcDocStub()
    cell = CalcCellStub()
    parts = parse_python_formula('=PY("x"; A1:A5)')
    assert parts is not None
    with patch("plugin.calc.python.editor.get_config", return_value=True):
        result = _apply_cell_save(
            doc,
            cell,
            parsed_parts=parts,
            new_code='y = xl("C1")',
            save_as_plain=False,
            data_binding_text=None,
        )
    assert result["ok"] is True
    formula = cell.getFormula()
    assert "A1:A5" in formula
    assert "C1" in formula
    assert "data[1]" in formula
