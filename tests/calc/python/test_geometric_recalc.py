# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Phase 1 Geometric Recalc Order: list-diff, splice, unanimous-ours (no UNO)."""

from __future__ import annotations

from unittest.mock import patch

from plugin.calc.calc_addin_data import split_python_addin_data_args
from plugin.calc.python.cell_discovery import _MAX_PYTHON_CELLS_FOUND
from plugin.calc.python.formula_edit import (
    parse_python_formula,
    py_formula_has_unquoted_code_ref,
)
from plugin.calc.python.geometric_recalc import (
    CONFIG_KEY,
    GEOMETRIC_DISCOVERY_CAP,
    GEOMETRIC_REGISTRY_PROP,
    EvalIndexKey,
    GeometricCell,
    GeometricRecord,
    compute_sheet_repair,
    discovery_cap_hit,
    eval_n_args_from_data,
    formula_data_args,
    geometric_cap_hit_user_message,
    local_a1,
    notify_geometric_cap_hit,
    rebuild_formula_with_data_args,
    repair_n_args,
    resolved_code_for_formula,
    same_cell_ref,
    should_strip_eval_args,
)


WB = "calc:file:///pipe.ods"


def _cell(addr: str, formula: str, code: str | None = None) -> GeometricCell:
    if code is None:
        code = resolved_code_for_formula(formula)
    return GeometricCell(address=addr, formula=formula, resolved_code=code)


def _repair(
    cells: list[GeometricCell],
    records: dict[str, GeometricRecord] | None = None,
    *,
    workbook_key: str = WB,
    sheet_name: str = "Sheet1",
):
    return compute_sheet_repair(
        cells, records, workbook_key=workbook_key, sheet_name=sheet_name
    )


def _patch_map(result):
    return {p.address: p for p in result.patches}


def test_constants_match_discovery_and_spill_pattern():
    assert GEOMETRIC_DISCOVERY_CAP == _MAX_PYTHON_CELLS_FOUND == 100
    assert GEOMETRIC_REGISTRY_PROP == "WriterAgentGeometricRegistry"
    assert CONFIG_KEY == "scripting.python_geometric_recalc_order"


def test_local_a1_and_same_cell_ref():
    assert local_a1("A1") == "A1"
    assert local_a1("$A$1") == "A1"
    assert local_a1("Sheet1.A1") == "A1"
    assert local_a1("Sheet1.$B$2") == "B2"
    assert same_cell_ref("$A$1", "A1")
    assert same_cell_ref("Sheet1.A1", "A1")
    assert not same_cell_ref("A1", "A2")


def test_empty_list():
    result = _repair([])
    assert result.skipped is False
    assert result.patches == ()
    assert result.records == {}
    assert result.strip_safe == frozenset()


def test_one_cell_no_predecessor():
    result = _repair([_cell("A1", '=PY("df = load()")')])
    assert result.patches == ()
    assert result.records == {}
    assert not result.strip_safe


def test_two_cells_append_predecessor():
    result = _repair(
        [
            _cell("A1", '=PY("df = load()")'),
            _cell("A2", '=PY("df = clean(df)")'),
        ]
    )
    patches = _patch_map(result)
    assert "A1" not in patches
    assert patches["A2"].action == "append"
    assert patches["A2"].new_formula == '=PY("df = clean(df)";A1)'
    assert result.records["A2"].predecessor == "A1"


def test_insert_in_middle_retargets_successor():
    cells = [
        _cell("A1", '=PY("a")'),
        _cell("A2", '=PY("b")'),
        _cell("A3", '=PY("c";A1)'),
    ]
    records = {"A3": GeometricRecord(predecessor="A1")}
    result = _repair(cells, records)
    patches = _patch_map(result)
    assert patches["A2"].new_formula == '=PY("b";A1)'
    assert patches["A3"].action == "replace"
    assert patches["A3"].new_formula == '=PY("c";A2)'
    assert result.records["A2"].predecessor == "A1"
    assert result.records["A3"].predecessor == "A2"


def test_delete_middle_retargets_successor():
    cells = [
        _cell("A1", '=PY("a")'),
        _cell("A3", '=PY("c";A2)'),
    ]
    records = {"A3": GeometricRecord(predecessor="A2")}
    result = _repair(cells, records)
    patches = _patch_map(result)
    assert patches["A3"].action == "replace"
    assert patches["A3"].new_formula == '=PY("c";A1)'
    assert result.records["A3"].predecessor == "A1"


def test_delete_first_remove_field():
    cells = [_cell("A2", '=PY("b";A1)')]
    records = {"A2": GeometricRecord(predecessor="A1")}
    result = _repair(cells, records)
    patches = _patch_map(result)
    assert patches["A2"].action == "remove"
    assert patches["A2"].new_formula == '=PY("b")'
    assert "A2" not in result.records


def test_remove_field_is_idempotent():
    first = _repair(
        [_cell("A1", '=PY("x";Z9)')],
        {"A1": GeometricRecord(predecessor="Z9")},
    )
    stripped = first.patches[0].new_formula
    second = _repair([_cell("A1", stripped)])
    assert first.patches[0].action == "remove"
    assert second.patches == ()
    assert second.records == {}


def test_reorder_retargets_to_new_previous():
    cells = [
        _cell("B1", '=PY("first")'),
        _cell("A2", '=PY("second";C9)'),
    ]
    records = {"A2": GeometricRecord(predecessor="C9")}
    result = _repair(cells, records)
    assert _patch_map(result)["A2"].new_formula == '=PY("second";B1)'


def test_splice_no_args_appends():
    result = _repair(
        [_cell("A1", '=PY("x")'), _cell("A2", '=PY("y")')]
    )
    assert _patch_map(result)["A2"].new_formula == '=PY("y";A1)'


def test_splice_preserves_user_range():
    result = _repair(
        [
            _cell("A1", '=PY("x")'),
            _cell("A2", '=PY("np.mean(data)";B1:B10)'),
        ]
    )
    assert _patch_map(result)["A2"].new_formula == '=PY("np.mean(data)";B1:B10;A1)'
    assert formula_data_args(result.patches[0].new_formula) == ["B1:B10", "A1"]


def test_already_correct_predecessor_is_noop():
    result = _repair(
        [
            _cell("A1", '=PY("x")'),
            _cell("A2", '=PY("y";A1)'),
        ],
        {"A2": GeometricRecord(predecessor="A1")},
    )
    assert result.patches == ()
    assert result.records["A2"].predecessor == "A1"


def test_already_correct_dollar_ref_is_noop():
    result = _repair(
        [
            _cell("A1", '=PY("x")'),
            _cell("A2", '=PY("y";$A$1)'),
        ],
        {"A2": GeometricRecord(predecessor="A1")},
    )
    assert result.patches == ()


def test_user_single_cell_data_is_appended_not_overwritten():
    result = _repair(
        [
            _cell("A1", '=PY("x")'),
            _cell("A2", '=PY("y";C5)'),
        ]
    )
    assert _patch_map(result)["A2"].action == "append"
    assert _patch_map(result)["A2"].new_formula == '=PY("y";C5;A1)'


def test_user_already_passed_previous_py_not_recorded():
    result = _repair(
        [
            _cell("A1", '=PY("x")'),
            _cell("A2", '=PY("y";A1)'),
        ]
    )
    assert result.patches == ()
    assert "A2" not in result.records
    assert not result.strip_safe


def test_code_in_cell_splice_keeps_unquoted_ref():
    raw = "=PY($A$1; B1:B10)"
    assert py_formula_has_unquoted_code_ref(raw)
    rebuilt = rebuild_formula_with_data_args(raw, ["B1:B10", "C1"])
    assert rebuilt is not None
    assert rebuilt.startswith("=PY($A$1")
    assert '"$A$1"' not in rebuilt
    assert "B1:B10" in rebuilt
    assert rebuilt.endswith(";C1)")
    parts = parse_python_formula(rebuilt)
    assert parts is not None
    assert parts.code == "$A$1"


def test_code_in_cell_repair_and_resolved_source():
    source = "df = clean(df)"
    result = _repair(
        [
            _cell("B1", '=PY("df = load()")'),
            _cell("B2", "=PY($A$1; C1:C10)", source),
        ]
    )
    new = _patch_map(result)["B2"].new_formula
    assert new.startswith("=PY($A$1")
    assert '"$A$1"' not in new
    assert formula_data_args(new) == ["C1:C10", "B1"]
    assert resolved_code_for_formula("=PY($A$1; C1:C10)", code_cell_text=source) == source
    assert resolved_code_for_formula('=PY("df = clean(df)")') == "df = clean(df)"
    key = EvalIndexKey(WB, source, 2)
    assert key in result.strip_safe


def test_repair_n_args_is_not_semicolon_count():
    assert repair_n_args('=PY("a; b; c")') == 0
    assert repair_n_args('=PY("np.mean(data)"; B1:B10)') == 1
    assert repair_n_args('=PY("np.mean(data)"; B1:B10; A1)') == 2
    assert repair_n_args("=PY($A$1; B1:B10; C1)") == 2


def test_n_args_matches_split_python_addin_data_args_for_range_plus_1x1():
    """(range, 1×1 pred) stays two args — inner of the 1×1 is a sequence."""
    col = ((1.0,), (2.0,), (3.0,))
    pred = ((0.0,),)
    split = split_python_addin_data_args((col, pred))
    assert len(split) == 2
    assert eval_n_args_from_data((col, pred)) == 2
    assert repair_n_args('=PY("np.mean(data)"; B1:B10; A1)') == 2


def test_fill_down_unanimous_ours_is_strip_safe():
    code = "np.mean(data)"
    result = _repair(
        [
            _cell("A1", f'=PY("{code}"; B1:B10)'),
            _cell("A2", f'=PY("{code}"; B1:B10)'),
            _cell("A3", f'=PY("{code}"; B1:B10)'),
        ]
    )
    assert formula_data_args(_patch_map(result)["A2"].new_formula) == ["B1:B10", "A1"]
    assert formula_data_args(_patch_map(result)["A3"].new_formula) == ["B1:B10", "A2"]
    key = EvalIndexKey(WB, code, 2)
    assert key in result.strip_safe
    assert should_strip_eval_args(
        workbook_key=WB,
        resolved_code=code,
        n_args=2,
        strip_safe=result.strip_safe,
        unambiguous=True,
    )
    first_key = EvalIndexKey(WB, code, 1)
    assert first_key not in result.strip_safe


def test_mixed_matrix_index_poisons_the_triple():
    """Same (code, n_args=2), one ours and one not → neither is strip-safe."""
    from plugin.calc.python.geometric_recalc import compute_eval_index

    code = "f"
    cells = [
        GeometricCell("A2", f'=PY("{code}"; B1:B10; A1)', code),
        GeometricCell("A3", f'=PY("{code}"; B1:B10; C5)', code),
    ]
    records = {"A2": GeometricRecord(predecessor="A1")}
    formulas = {c.address: c.formula for c in cells}
    safe = compute_eval_index(cells, formulas, records, WB)
    assert EvalIndexKey(WB, code, 2) not in safe
    assert not should_strip_eval_args(
        workbook_key=WB,
        resolved_code=code,
        n_args=2,
        strip_safe=safe,
        unambiguous=True,
    )


def test_user_passed_previous_mixed_with_mapped_poisons():
    code = "f"
    cells = [
        _cell("A1", f'=PY("{code}")'),
        _cell("A2", f'=PY("{code}"; A1)', code),
        _cell("A3", f'=PY("{code}"; A2)', code),
    ]
    result = _repair(cells, {"A3": GeometricRecord(predecessor="A2")})
    # A2 last==desired, not ours → not recorded. A3 ours n_args=1. A2 n_args=1.
    key = EvalIndexKey(WB, code, 1)
    assert "A2" not in result.records
    assert "A3" in result.records
    assert key not in result.strip_safe


def test_two_workbooks_unambiguous_false_no_strip():
    result = _repair(
        [
            _cell("A1", '=PY("np.mean(data)"; B1:B10)'),
            _cell("A2", '=PY("np.mean(data)"; B1:B10)'),
        ]
    )
    assert EvalIndexKey(WB, "np.mean(data)", 2) in result.strip_safe
    assert not should_strip_eval_args(
        workbook_key=WB,
        resolved_code="np.mean(data)",
        n_args=2,
        strip_safe=result.strip_safe,
        unambiguous=False,
    )
    assert not should_strip_eval_args(
        workbook_key="",
        resolved_code="np.mean(data)",
        n_args=2,
        strip_safe=result.strip_safe,
        unambiguous=True,
    )


def test_cap_hit_skips_sheet_no_patches_no_strip_safe():
    cells = [_cell(f"A{i + 1}", '=PY("x")') for i in range(GEOMETRIC_DISCOVERY_CAP)]
    result = _repair(cells, sheet_name="Data")
    assert discovery_cap_hit(len(cells))
    assert result.skipped is True
    assert result.skip_reason == "discovery_cap"
    assert result.patches == ()
    assert result.strip_safe == frozenset()
    assert result.user_message is not None
    assert "Data" in result.user_message
    assert str(GEOMETRIC_DISCOVERY_CAP) in result.user_message


def test_cap_hit_over_skips_exact_100():
    assert discovery_cap_hit(99) is False
    assert discovery_cap_hit(100) is True
    assert discovery_cap_hit(101) is True


def test_notify_geometric_cap_hit_one_box_per_sheet_ui_thread_only():
    notified: set[str] = set()
    with (
        patch("plugin.framework.thread_guard.on_main_thread", return_value=True),
        patch("plugin.chatbot.dialogs.msgbox") as mock_box,
    ):
        assert notify_geometric_cap_hit("ctx", "Sheet1", already_notified=notified)
        assert notify_geometric_cap_hit("ctx", "Sheet1", already_notified=notified) is False
        assert notify_geometric_cap_hit("ctx", "Sheet2", already_notified=notified)
        assert mock_box.call_count == 2
        titles = [c.args[1] for c in mock_box.call_args_list]
        assert all(t == "Geometric Recalc Order" or "Geometric" in t for t in titles)
        assert mock_box.call_args_list[0].kwargs.get("box_type") == 3

    with (
        patch("plugin.framework.thread_guard.on_main_thread", return_value=False),
        patch("plugin.chatbot.dialogs.msgbox") as mock_box,
    ):
        shown = notify_geometric_cap_hit("ctx", "Other", already_notified=set())
        assert shown is False
        mock_box.assert_not_called()


def test_cap_hit_user_message_names_the_sheet():
    text = geometric_cap_hit_user_message("Pivots")
    assert "Pivots" in text
    assert str(GEOMETRIC_DISCOVERY_CAP) in text
