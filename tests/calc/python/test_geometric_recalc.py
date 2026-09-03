# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Geometric Recalc Order: list-diff, splice, attach map, unanimous-ours strip."""

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
    after_py_cell_save,
    compute_sheet_repair,
    current_geometric_strip_safe,
    discovery_cap_hit,
    ensure_geometric_strip_index_for_eval,
    eval_n_args_from_data,
    formula_data_args,
    geometric_cap_hit_user_message,
    geometric_workbook_key,
    load_geometric_registry_for_doc,
    local_a1,
    maybe_geometric_on_document_open,
    maybe_strip_geometric_eval_args,
    notify_geometric_cap_hit,
    record_geometric_calc_session,
    reconcile_geometric_document,
    replace_geometric_strip_safe,
    reset_geometric_runtime_for_tests,
    resolved_code_for_formula,
    rebuild_formula_with_data_args,
    repair_n_args,
    save_geometric_registry_for_doc,
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
    truncated: bool = False,
):
    return compute_sheet_repair(
        cells,
        records,
        workbook_key=workbook_key,
        sheet_name=sheet_name,
        truncated=truncated,
    )


def _patch_map(result):
    return {p.address: p for p in result.patches}


def _key(code: str, n_args: int, workbook_key: str = WB) -> EvalIndexKey:
    return EvalIndexKey(workbook_key, code, n_args)


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


def test_splice_preserves_absolute_user_data_ref():
    """Existing user args stay verbatim — attach must not strip $ from $C$5."""
    result = _repair(
        [
            _cell("A1", '=PY("x")'),
            _cell("A2", '=PY("y"; $C$5)'),
        ]
    )
    new = _patch_map(result)["A2"].new_formula
    assert "$C$5" in new
    assert formula_data_args(new) == ["$C$5", "A1"]


def test_splice_quoted_code_skips_calc_sanitizer():
    """Geometric splice must not rewrite hand-written float( inside quotes."""
    result = _repair(
        [
            _cell("A1", '=PY("x")'),
            _cell("A2", '=PY("float(1)"; $C$5)'),
        ]
    )
    new = _patch_map(result)["A2"].new_formula
    assert "float(1)" in new
    assert "+0.0" not in new
    assert "$C$5" in new
    assert formula_data_args(new) == ["$C$5", "A1"]
    # Live Classic getFormula() stores =py( (lowercase), not =PY( or OriginalName.
    # parts.prefix must round-trip that spelling — do not force CALC_PYTHON_FN.
    live = rebuild_formula_with_data_args('=py("float(1)"; $C$5)', ["$C$5", "A1"])
    assert live is not None
    assert live.startswith('=py("')
    assert "float(1)" in live
    assert "$C$5" in live


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
    key = _key(source, 2)
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
    key = _key(code, 2)
    assert key in result.strip_safe
    assert should_strip_eval_args(
        workbook_key=WB,
        resolved_code=code,
        n_args=2,
        strip_safe=result.strip_safe,
        unambiguous=True,
    )
    first_key = _key(code, 1)
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
    mixed_key = _key(code, 2)
    assert mixed_key not in safe
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
    key = _key(code, 1)
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
    assert _key("np.mean(data)", 2) in result.strip_safe
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
    result = _repair(cells, sheet_name="Data", truncated=True)
    assert discovery_cap_hit(len(cells), truncated=True)
    assert result.skipped is True
    assert result.skip_reason == "discovery_cap"
    assert result.patches == ()
    assert result.strip_safe == frozenset()
    assert result.user_message is not None
    assert "Data" in result.user_message
    assert str(GEOMETRIC_DISCOVERY_CAP) in result.user_message


def test_exact_100_without_truncated_is_chained():
    """Phase 3: an exact 100 that finished the scan is not a cap-hit."""
    cells = [_cell(f"A{i + 1}", '=PY("x")') for i in range(GEOMETRIC_DISCOVERY_CAP)]
    result = _repair(cells, sheet_name="Data", truncated=False)
    assert result.skipped is False
    assert result.patches  # A2..A100 get a predecessor
    assert discovery_cap_hit(len(cells), truncated=False) is False


def test_discovery_cap_hit_uses_truncated_flag():
    assert discovery_cap_hit(99, truncated=False) is False
    assert discovery_cap_hit(100, truncated=False) is False
    assert discovery_cap_hit(100, truncated=True) is True
    assert discovery_cap_hit(50, truncated=True) is True
    # Count-only fallback (no truncated) still treats len >= 100 as cap-hit.
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


# ---------------------------------------------------------------------------
# Phase 2 — Isolated session record, UDProp, attach on flag-on / save
# ---------------------------------------------------------------------------


def _reset_geo(monkeypatch=None):
    reset_geometric_runtime_for_tests()
    from plugin.scripting import session_manager as sm

    sm.clear_active_calc_session()


def test_isolated_record_active_calc_session_never_empty():
    """Isolated UI load/repair records calc:+_workbook_session_key (same string eval reads).

    Isolated + no init still never records on its own — this geometric call is
    required for that case. Do not assert Isolated always leaves
    ``_RECORDED_CALC_SESSION_IDS`` empty: a non-empty init script already
    records via ``build_python_eval_init_kwargs`` → ``calc_init_session_id``.
    """
    from plugin.scripting import session_manager as sm
    from plugin.tests.testing_utils import CalcDocStub

    _reset_geo()
    doc = CalcDocStub(url="file:///isolated.ods")
    with patch(
        "plugin.scripting.session_manager.python_session_mode", return_value="isolated"
    ):
        assert sm.workbook_session_id(None, doc) is None
        sid = record_geometric_calc_session(doc)
    assert sid == "calc:file:///isolated.ods"
    assert sm.get_cached_calc_session_id() == sid
    assert sm.off_main_calc_session_is_unambiguous()
    assert sid.startswith("calc:")
    assert sid != "calc:"
    assert geometric_workbook_key(doc) == sid


def test_unsaved_workbook_key_is_never_empty():
    from plugin.tests.testing_utils import CalcDocStub

    _reset_geo()
    doc = CalcDocStub(url="")
    with patch(
        "plugin.scripting.session_manager._workbook_session_key",
        return_value="unsaved-stable-id",
    ):
        sid = record_geometric_calc_session(doc)
        assert geometric_workbook_key(doc) == sid
    assert sid == "calc:unsaved-stable-id"
    assert sid != "calc:"


def test_load_and_save_geometric_registry(monkeypatch):
    import json

    from plugin.tests.testing_utils import CalcDocStub

    _reset_geo()
    saved_payload = None

    def mock_get_prop(model, name, default=None):
        if name == GEOMETRIC_REGISTRY_PROP:
            return json.dumps(
                {"workbook_key": "calc:file:///geo.ods", "sheets": {"Sheet1": {"A2": "A1"}}}
            )
        return default

    def mock_set_prop(model, name, value):
        nonlocal saved_payload
        if name == GEOMETRIC_REGISTRY_PROP:
            saved_payload = value

    monkeypatch.setattr("plugin.doc.udprops.get_document_property", mock_get_prop)
    monkeypatch.setattr("plugin.doc.udprops.set_document_property", mock_set_prop)

    doc = CalcDocStub(url="file:///geo.ods")
    key = load_geometric_registry_for_doc(doc)
    assert key == "calc:file:///geo.ods"
    assert GEOMETRIC_REGISTRY_PROP == "WriterAgentGeometricRegistry"
    from plugin.calc.python.geometric_recalc import records_for_sheet

    recs = records_for_sheet(key, "Sheet1")
    assert recs["A2"].predecessor == "A1"

    recs["A3"] = GeometricRecord(predecessor="A2")
    from plugin.calc.python.geometric_recalc import replace_records_for_sheet

    replace_records_for_sheet(key, "Sheet1", recs)
    save_geometric_registry_for_doc(doc, key)
    assert saved_payload is not None
    data = json.loads(saved_payload)
    assert data["workbook_key"] == key
    assert data["sheets"]["Sheet1"]["A2"] == "A1"
    assert data["sheets"]["Sheet1"]["A3"] == "A2"


def test_flag_on_attaches_all_sheets(monkeypatch):
    from plugin.tests.testing_utils import CalcDocStub, CalcSheetStub

    _reset_geo()
    sheet1 = CalcSheetStub("Sheet1")
    sheet2 = CalcSheetStub("Data")
    sheet1.getCellByPosition(0, 0).setFormula('=PY("a")')
    sheet1.getCellByPosition(0, 1).setFormula('=PY("b")')
    sheet2.getCellByPosition(0, 0).setFormula('=PY("c")')
    sheet2.getCellByPosition(0, 1).setFormula('=PY("d")')
    doc = CalcDocStub(sheets=[sheet1, sheet2], url="file:///multi-geo.ods")
    monkeypatch.setattr(
        "plugin.calc.python.geometric_recalc.geometric_flag_enabled", lambda: True
    )
    monkeypatch.setattr("plugin.doc.udprops.get_document_property", lambda *_a, **_k: None)
    monkeypatch.setattr("plugin.doc.udprops.set_document_property", lambda *_a, **_k: None)
    reconcile_geometric_document("ctx", doc)
    assert sheet1.getCellByPosition(0, 1).getFormula() == '=PY("b";A1)'
    assert sheet2.getCellByPosition(0, 1).getFormula() == '=PY("d";A1)'
    assert sheet1.getCellByPosition(0, 0).getFormula() == '=PY("a")'


def test_after_save_attaches_predecessor(monkeypatch):
    from plugin.tests.testing_utils import CalcDocStub

    _reset_geo()
    doc = CalcDocStub(url="file:///save-geo.ods")
    sheet = doc.getSheets().getByName("Sheet1")
    sheet.getCellByPosition(0, 0).setFormula('=PY("first")')
    cell = sheet.getCellByPosition(0, 1)
    cell.setFormula('=PY("second")')
    monkeypatch.setattr(
        "plugin.calc.python.geometric_recalc.geometric_flag_enabled", lambda: True
    )
    monkeypatch.setattr("plugin.doc.udprops.get_document_property", lambda *_a, **_k: None)
    monkeypatch.setattr("plugin.doc.udprops.set_document_property", lambda *_a, **_k: None)
    after_py_cell_save(doc, cell, ctx="ctx")
    assert cell.getFormula() == '=PY("second";A1)'


def test_flag_off_does_not_attach_on_save(monkeypatch):
    from plugin.tests.testing_utils import CalcDocStub

    _reset_geo()
    doc = CalcDocStub(url="file:///off-geo.ods")
    sheet = doc.getSheets().getByName("Sheet1")
    sheet.getCellByPosition(0, 0).setFormula('=PY("first")')
    cell = sheet.getCellByPosition(0, 1)
    cell.setFormula('=PY("second")')
    monkeypatch.setattr(
        "plugin.calc.python.geometric_recalc.geometric_flag_enabled", lambda: False
    )
    after_py_cell_save(doc, cell, ctx="ctx")
    assert cell.getFormula() == '=PY("second")'


def test_document_open_flag_on_reconciles(monkeypatch):
    from plugin.tests.testing_utils import CalcDocStub

    _reset_geo()
    doc = CalcDocStub(url="file:///open-geo.ods")
    sheet = doc.getSheets().getByName("Sheet1")
    sheet.getCellByPosition(0, 0).setFormula('=PY("a")')
    sheet.getCellByPosition(0, 1).setFormula('=PY("b")')
    monkeypatch.setattr(
        "plugin.calc.python.geometric_recalc.geometric_flag_enabled", lambda: True
    )
    monkeypatch.setattr("plugin.doc.udprops.get_document_property", lambda *_a, **_k: None)
    monkeypatch.setattr("plugin.doc.udprops.set_document_property", lambda *_a, **_k: None)
    maybe_geometric_on_document_open("ctx", doc)
    assert sheet.getCellByPosition(0, 1).getFormula() == '=PY("b";A1)'


def test_reentrancy_skips_nested_repair(monkeypatch):
    from plugin.calc.python import geometric_recalc as geo
    from plugin.tests.testing_utils import CalcDocStub

    _reset_geo()
    doc = CalcDocStub(url="file:///reenter.ods")
    sheet = doc.getSheets().getByName("Sheet1")
    sheet.getCellByPosition(0, 0).setFormula('=PY("a")')
    sheet.getCellByPosition(0, 1).setFormula('=PY("b")')
    monkeypatch.setattr(geo, "geometric_flag_enabled", lambda: True)
    monkeypatch.setattr("plugin.doc.udprops.get_document_property", lambda *_a, **_k: None)
    monkeypatch.setattr("plugin.doc.udprops.set_document_property", lambda *_a, **_k: None)
    geo._GEOMETRIC_REPAIRING = True
    try:
        geo.reconcile_geometric_document("ctx", doc)
        assert sheet.getCellByPosition(0, 1).getFormula() == '=PY("b")'
    finally:
        geo._GEOMETRIC_REPAIRING = False


def test_cap_hit_sheet_skipped_on_reconcile(monkeypatch):
    from plugin.tests.testing_utils import CalcDocStub

    _reset_geo()
    doc = CalcDocStub(url="file:///cap-geo.ods")
    sheet = doc.getSheets().getByName("Sheet1")
    for i in range(GEOMETRIC_DISCOVERY_CAP + 1):
        sheet.getCellByPosition(0, i).setFormula(f'=PY("x{i}")')
    monkeypatch.setattr(
        "plugin.calc.python.geometric_recalc.geometric_flag_enabled", lambda: True
    )
    monkeypatch.setattr("plugin.doc.udprops.get_document_property", lambda *_a, **_k: None)
    monkeypatch.setattr("plugin.doc.udprops.set_document_property", lambda *_a, **_k: None)
    with (
        patch("plugin.framework.thread_guard.on_main_thread", return_value=True),
        patch("plugin.chatbot.dialogs.msgbox") as mock_box,
    ):
        reconcile_geometric_document("ctx", doc)
        assert mock_box.call_count == 1
    # First two cells stay unchained (whole sheet skipped).
    assert sheet.getCellByPosition(0, 1).getFormula() == '=PY("x1")'
    assert current_geometric_strip_safe() == frozenset()


# ---------------------------------------------------------------------------
# Phase 4 — strip before index heuristic / calc_addin_args_from_split
# ---------------------------------------------------------------------------


def _mean_range_and_pred():
    col = ((1.0,), (2.0,), (3.0,))
    pred = ((0.0,),)
    return col, pred


def test_strip_np_mean_data_stays_single_range():
    """=PY("np.mean(data)"; B1:B10; pred) still packs one CalcRange, not a list."""
    from plugin.calc.python.function import execute_python_addin
    from plugin.scripting import session_manager as sm
    from plugin.scripting.calc_range import is_calc_range_payload
    from plugin.scripting.payload_codec import is_multi_data

    _reset_geo()
    sm.record_active_calc_session(WB)
    col, pred = _mean_range_and_pred()
    replace_geometric_strip_safe(WB, frozenset({_key("np.mean(data)", 2)}))
    with patch("plugin.calc.python.function.run_code_in_user_venv") as mock_run:
        mock_run.return_value = {"status": "ok", "result": 2.0}
        out = execute_python_addin(object(), "np.mean(data)", (col, pred))
        assert out == 2.0
        wire = mock_run.call_args.kwargs["data"]
        assert not is_multi_data(wire)
        assert is_calc_range_payload(wire)
        assert wire["shape"] == [3, 1]


def test_strip_ranges_minus_one_is_user_range_not_pred():
    """Indexed multi-data branch: ranges[-1] is B1:B10, not the predecessor."""
    from plugin.calc.python.function import execute_python_addin
    from plugin.scripting import session_manager as sm
    from plugin.scripting.payload_codec import is_multi_data

    _reset_geo()
    sm.record_active_calc_session(WB)
    code = "ranges[-1].shape"
    col, pred = _mean_range_and_pred()
    replace_geometric_strip_safe(WB, frozenset({_key(code, 2)}))
    with patch("plugin.calc.python.function.run_code_in_user_venv") as mock_run:
        mock_run.return_value = {"status": "ok", "result": (3, 1)}
        execute_python_addin(object(), code, (col, pred))
        wire = mock_run.call_args.kwargs["data"]
        # After strip, only B1:B10 remains. Indexed multi-data must not see pred.
        from plugin.scripting.calc_range import is_calc_range_payload

        assert not is_multi_data(wire)
        assert is_calc_range_payload(wire)
        assert wire["shape"] == [3, 1]


def test_strip_runs_before_matrix_index_peel():
    """Last geometric 1-cell must not become index_arg (silent wrong numbers)."""
    from plugin.calc.python.function import execute_python_addin
    from plugin.scripting import session_manager as sm

    _reset_geo()
    sm.record_active_calc_session(WB)
    col, pred = _mean_range_and_pred()
    replace_geometric_strip_safe(WB, frozenset({_key("np.mean(data)", 2)}))
    with patch("plugin.calc.python.function.run_code_in_user_venv") as mock_run:
        mock_run.return_value = {"status": "ok", "result": [10, 20, 30]}
        out = execute_python_addin(object(), "np.mean(data)", (col, pred))
        # Without strip, pred 0 would be index_arg and return 10. Strip first
        # so finalize sees no index and returns the first scalar of the list.
        assert out == 10
        # And the worker saw a single range (not list-of-ranges).
        from plugin.scripting.payload_codec import is_multi_data

        assert not is_multi_data(mock_run.call_args.kwargs["data"])


def test_fill_down_both_strip():
    from plugin.scripting import session_manager as sm

    _reset_geo()
    sm.record_active_calc_session(WB)
    code = "np.mean(data)"
    col, pred = _mean_range_and_pred()
    replace_geometric_strip_safe(WB, frozenset({_key(code, 2)}))
    stripped = maybe_strip_geometric_eval_args(code, [col, pred])
    assert stripped == [col]
    assert maybe_strip_geometric_eval_args(code, [col, ((1.0,),)]) == [col]


def test_mixed_poison_neither_strips():
    from plugin.scripting import session_manager as sm

    _reset_geo()
    sm.record_active_calc_session(WB)
    # Triple not in the map → no strip (mixed poisons the whole triple).
    col, pred = _mean_range_and_pred()
    assert maybe_strip_geometric_eval_args("f", [col, pred]) == [col, pred]


def test_two_workbooks_unambiguous_false_no_strip_at_eval():
    from plugin.scripting import session_manager as sm

    _reset_geo()
    sm.record_active_calc_session("calc:file:///a.ods")
    sm.record_active_calc_session("calc:file:///b.ods")
    col, pred = _mean_range_and_pred()
    replace_geometric_strip_safe(
        "calc:file:///a.ods",
        frozenset({_key("np.mean(data)", 2, "calc:file:///a.ods")}),
    )
    assert maybe_strip_geometric_eval_args("np.mean(data)", [col, pred]) == [col, pred]


def test_isolated_unambiguous_session_strips():
    from plugin.tests.testing_utils import CalcDocStub

    _reset_geo()
    doc = CalcDocStub(url="file:///iso-strip.ods")
    with patch(
        "plugin.scripting.session_manager.python_session_mode", return_value="isolated"
    ):
        sid = record_geometric_calc_session(doc)
    col, pred = _mean_range_and_pred()
    replace_geometric_strip_safe(sid, frozenset({_key("np.mean(data)", 2, sid)}))
    assert maybe_strip_geometric_eval_args("np.mean(data)", [col, pred]) == [col]


def test_ensure_strip_index_hydrates_from_udprop():
    """UI-thread eval rebuilds ``_STRIP_SAFE`` from UDProp (late attach / URP)."""
    import json

    from plugin.tests.testing_utils import CalcDocStub

    _reset_geo()
    doc = CalcDocStub(url="file:///hydrate-geo.ods")
    sheet = doc.getSheets().getByName("Sheet1")
    sheet.getCellByPosition(0, 0).setFormula('=PY("x = 41")')
    sheet.getCellByPosition(0, 2).setFormula('=PY("x";A1)')
    payload = json.dumps(
        {
            "workbook_key": "calc:file:///hydrate-geo.ods",
            "sheets": {"Sheet1": {"A3": "A1"}},
        }
    )
    with (
        patch(
            "plugin.doc.udprops.get_document_property",
            lambda _doc, name, default=None: (
                payload if name == GEOMETRIC_REGISTRY_PROP else default
            ),
        ),
        patch("plugin.framework.thread_guard.on_main_thread", return_value=True),
    ):
        ensure_geometric_strip_index_for_eval(doc, ctx=None)
    assert maybe_strip_geometric_eval_args("x", [((41.0,),)]) == []


def test_ensure_strip_index_off_main_is_noop():
    from plugin.tests.testing_utils import CalcDocStub

    _reset_geo()
    doc = CalcDocStub(url="file:///hydrate-off.ods")
    with patch("plugin.framework.thread_guard.on_main_thread", return_value=False):
        ensure_geometric_strip_index_for_eval(doc, ctx=None)
    assert current_geometric_strip_safe() == frozenset()


def test_user_1x1_not_in_map_no_strip():
    from plugin.scripting import session_manager as sm

    _reset_geo()
    sm.record_active_calc_session(WB)
    col, pred = _mean_range_and_pred()
    # User 1×1 last arg, no map record, no mixed chain → do not strip.
    assert maybe_strip_geometric_eval_args("g", [col, pred]) == [col, pred]


def test_strip_helper_does_not_use_1x1_or_uniqueness():
    """Never fall back to 1×1, uniqueness, or ≥1-hit — only unanimous-ours."""
    from plugin.scripting import session_manager as sm

    _reset_geo()
    sm.record_active_calc_session(WB)
    col, pred = _mean_range_and_pred()
    # Unique code + 1×1 last arg is still no-strip without a map mark.
    assert maybe_strip_geometric_eval_args("unique_snippet_xyz", [col, pred]) == [col, pred]
    # ≥1-hit would strip if any cell is ours; we require the triple in strip_safe.
    from plugin.calc.python.geometric_recalc import GEOMETRIC_RECORDS

    GEOMETRIC_RECORDS[(WB, "Sheet1", "A2")] = GeometricRecord(predecessor="A1")
    assert maybe_strip_geometric_eval_args("f", [col, pred]) == [col, pred]


def test_mixed_same_code_n_args_overpoisons_other_range():
    """Same (code, n_args): mixed on range A also poisons range B.

    Fingerprint isolation is dropped. Residual is safe: no strip on B either,
    so numbers stay correct. A value hash was the worse trade (data edits
    missed the key and skipped strip on the edited cell).
    """
    from plugin.calc.python.geometric_recalc import compute_eval_index
    from plugin.scripting import session_manager as sm

    _reset_geo()
    sm.record_active_calc_session(WB)
    code = "np.mean(data)"
    range_a = ((1.0,), (2.0,), (3.0,))
    range_b = ((10.0,), (20.0,), (30.0,))
    pred = ((0.0,),)
    cells = [
        GeometricCell("A2", f'=PY("{code}"; B1:B10; A1)', code),
        GeometricCell("A3", f'=PY("{code}"; B1:B10; C5)', code),
        GeometricCell("D2", f'=PY("{code}"; C1:C10; D1)', code),
    ]
    records = {
        "A2": GeometricRecord(predecessor="A1"),
        "D2": GeometricRecord(predecessor="D1"),
    }
    formulas = {cell.address: cell.formula for cell in cells}
    safe = compute_eval_index(cells, formulas, records, WB)
    assert _key(code, 2) not in safe
    replace_geometric_strip_safe(WB, safe)
    assert maybe_strip_geometric_eval_args(code, [range_a, pred]) == [range_a, pred]
    assert maybe_strip_geometric_eval_args(code, [range_b, pred]) == [range_b, pred]


def test_data_value_edit_still_strips():
    """Strip-safe from a formula; eval args have different live range values.

    A value fingerprint of args[:-1] missed after a range edit. The 3-field
    key still strips. Phase 3 rebuilds the index on modify; do not inject
    a fingerprint.
    """
    from plugin.scripting import session_manager as sm

    _reset_geo()
    sm.record_active_calc_session(WB)
    code = "np.mean(data)"
    replace_geometric_strip_safe(WB, frozenset({_key(code, 2)}))
    edited_col = ((1.0,), (99.0,), (3.0,))
    pred = ((0.0,),)
    assert maybe_strip_geometric_eval_args(code, [edited_col, pred]) == [edited_col]


def test_flag_off_still_strips_leftover_attached_arg(monkeypatch):
    """§9.4: flag-off leaves leftover refs; strip must still drop the last arg."""
    from plugin.scripting import session_manager as sm

    _reset_geo()
    sm.record_active_calc_session(WB)
    monkeypatch.setattr(
        "plugin.calc.python.geometric_recalc.geometric_flag_enabled", lambda: False
    )
    col, pred = _mean_range_and_pred()
    replace_geometric_strip_safe(WB, frozenset({_key("np.mean(data)", 2)}))
    assert maybe_strip_geometric_eval_args("np.mean(data)", [col, pred]) == [col]


def test_strip_safe_snapshot_is_rebound_not_mutated():
    """§3.5: swap in a new frozenset; the previous snapshot stays unchanged."""
    _reset_geo()
    first = frozenset({EvalIndexKey(WB, "a", 2)})
    replace_geometric_strip_safe(WB, first)
    old = current_geometric_strip_safe()
    replace_geometric_strip_safe(WB, frozenset({EvalIndexKey(WB, "b", 2)}))
    new = current_geometric_strip_safe()
    assert new is not old
    assert EvalIndexKey(WB, "a", 2) in old
    assert EvalIndexKey(WB, "a", 2) not in new
    assert EvalIndexKey(WB, "b", 2) in new


# ---------------------------------------------------------------------------
# Phase 3 — shared sheet-modify dispatcher / debounce
# ---------------------------------------------------------------------------


class _DeferredTimer:
    """Timer that records the callback and fires only when asked."""

    instances: list["_DeferredTimer"] = []

    def __init__(self, interval, function, args=(), kwargs=None):
        self.interval = interval
        self.function = function
        self.args = args
        self.kwargs = kwargs or {}
        self.cancelled = False
        self.started = False
        _DeferredTimer.instances.append(self)

    def start(self) -> None:
        self.started = True

    def cancel(self) -> None:
        self.cancelled = True

    def fire(self) -> None:
        if not self.cancelled:
            self.function(*self.args, **self.kwargs)


def _install_deferred_timer(monkeypatch):
    _DeferredTimer.instances.clear()
    monkeypatch.setattr("plugin.calc.python.function.threading.Timer", _DeferredTimer)
    monkeypatch.setattr(
        "plugin.framework.queue_executor.post_to_main_thread",
        lambda fn, *a, **k: fn(*a, **k),
    )
    monkeypatch.setattr(
        "plugin.framework.thread_guard.on_main_thread", lambda: True
    )


def test_dispatcher_debounce_coalesces_two_modifies(monkeypatch):
    from types import SimpleNamespace

    from plugin.calc.python.sheet_modify import (
        dispatch_sheet_modified,
        reset_sheet_modify_runtime_for_tests,
    )
    from plugin.tests.testing_utils import CalcDocStub

    _reset_geo()
    reset_sheet_modify_runtime_for_tests()
    _install_deferred_timer(monkeypatch)
    doc = CalcDocStub(url="file:///debounce-geo.ods")
    sheet = doc.getSheets().getByName("Sheet1")
    monkeypatch.setattr("plugin.calc.python.function._get_calc_doc", lambda _ctx: doc)
    monkeypatch.setattr(
        "plugin.calc.python.geometric_recalc.geometric_flag_enabled", lambda: True
    )
    monkeypatch.setattr("plugin.doc.udprops.get_document_property", lambda *_a, **_k: None)
    monkeypatch.setattr("plugin.doc.udprops.set_document_property", lambda *_a, **_k: None)

    event = SimpleNamespace(Source=sheet)
    dispatch_sheet_modified("ctx", "file:///debounce-geo.ods", "Sheet1", event)
    dispatch_sheet_modified("ctx", "file:///debounce-geo.ods", "Sheet1", event)
    live = [t for t in _DeferredTimer.instances if t.started and not t.cancelled]
    assert len(live) == 1
    assert live[0].interval == 0.1
    cancelled = [t for t in _DeferredTimer.instances if t.cancelled]
    assert len(cancelled) == 1


def test_dispatcher_flag_off_does_not_rewrite(monkeypatch):
    from plugin.calc.python.sheet_modify import (
        flush_sheet_modify_pass_for_tests,
        reset_sheet_modify_runtime_for_tests,
    )
    from plugin.tests.testing_utils import CalcDocStub

    _reset_geo()
    reset_sheet_modify_runtime_for_tests()
    doc = CalcDocStub(url="file:///flag-off-mod.ods")
    sheet = doc.getSheets().getByName("Sheet1")
    sheet.getCellByPosition(0, 0).setFormula('=PY("first")')
    sheet.getCellByPosition(0, 1).setFormula('=PY("second")')
    monkeypatch.setattr(
        "plugin.calc.python.geometric_recalc.geometric_flag_enabled", lambda: False
    )
    monkeypatch.setattr("plugin.calc.python.function._get_calc_doc", lambda _ctx: doc)
    flush_sheet_modify_pass_for_tests("ctx", doc, sheet)
    assert sheet.getCellByPosition(0, 1).getFormula() == '=PY("second")'


def test_dispatcher_insert_retargets_successor(monkeypatch):
    from plugin.calc.python.formula_edit import parse_python_formula
    from plugin.calc.python.geometric_recalc import formula_data_args, same_cell_ref
    from plugin.calc.python.sheet_modify import (
        flush_sheet_modify_pass_for_tests,
        reset_sheet_modify_runtime_for_tests,
    )
    from plugin.tests.testing_utils import CalcDocStub

    _reset_geo()
    reset_sheet_modify_runtime_for_tests()
    doc = CalcDocStub(url="file:///insert-geo.ods")
    sheet = doc.getSheets().getByName("Sheet1")
    sheet.getCellByPosition(0, 0).setFormula('=PY("first")')
    sheet.getCellByPosition(0, 2).setFormula('=PY("third")')
    monkeypatch.setattr(
        "plugin.calc.python.geometric_recalc.geometric_flag_enabled", lambda: True
    )
    monkeypatch.setattr("plugin.doc.udprops.get_document_property", lambda *_a, **_k: None)
    monkeypatch.setattr("plugin.doc.udprops.set_document_property", lambda *_a, **_k: None)
    monkeypatch.setattr("plugin.calc.python.function._get_calc_doc", lambda _ctx: doc)
    flush_sheet_modify_pass_for_tests("ctx", doc, sheet)
    third = sheet.getCellByPosition(0, 2).getFormula()
    args = formula_data_args(third)
    assert args and same_cell_ref(args[-1], "A1")

    sheet.getCellByPosition(0, 1).setFormula('=PY("mid")')
    flush_sheet_modify_pass_for_tests("ctx", doc, sheet)
    mid = sheet.getCellByPosition(0, 1).getFormula()
    mid_args = formula_data_args(mid)
    assert mid_args and same_cell_ref(mid_args[-1], "A1")
    third_after = sheet.getCellByPosition(0, 2).getFormula()
    third_args = formula_data_args(third_after)
    assert third_args and same_cell_ref(third_args[-1], "A2")
    assert parse_python_formula(mid) is not None


def test_dispatcher_delete_first_removes_field(monkeypatch):
    from plugin.calc.python.geometric_recalc import formula_data_args
    from plugin.calc.python.sheet_modify import (
        flush_sheet_modify_pass_for_tests,
        reset_sheet_modify_runtime_for_tests,
    )
    from plugin.tests.testing_utils import CalcDocStub

    _reset_geo()
    reset_sheet_modify_runtime_for_tests()
    doc = CalcDocStub(url="file:///delete-first.ods")
    sheet = doc.getSheets().getByName("Sheet1")
    sheet.getCellByPosition(0, 0).setFormula('=PY("first")')
    sheet.getCellByPosition(0, 1).setFormula('=PY("second")')
    monkeypatch.setattr(
        "plugin.calc.python.geometric_recalc.geometric_flag_enabled", lambda: True
    )
    monkeypatch.setattr("plugin.doc.udprops.get_document_property", lambda *_a, **_k: None)
    monkeypatch.setattr("plugin.doc.udprops.set_document_property", lambda *_a, **_k: None)
    monkeypatch.setattr("plugin.calc.python.function._get_calc_doc", lambda _ctx: doc)
    flush_sheet_modify_pass_for_tests("ctx", doc, sheet)
    assert formula_data_args(sheet.getCellByPosition(0, 1).getFormula())

    sheet.getCellByPosition(0, 0).setFormula("")
    flush_sheet_modify_pass_for_tests("ctx", doc, sheet)
    leftover = formula_data_args(sheet.getCellByPosition(0, 1).getFormula())
    assert leftover == []


def test_dispatcher_reentrancy_skips_nested_schedule(monkeypatch):
    from types import SimpleNamespace

    from plugin.calc.python import geometric_recalc as geo
    from plugin.calc.python.sheet_modify import (
        _PENDING_TIMERS,
        dispatch_sheet_modified,
        reset_sheet_modify_runtime_for_tests,
    )
    from plugin.tests.testing_utils import CalcDocStub

    _reset_geo()
    reset_sheet_modify_runtime_for_tests()
    _install_deferred_timer(monkeypatch)
    doc = CalcDocStub(url="file:///reenter-mod.ods")
    sheet = doc.getSheets().getByName("Sheet1")
    monkeypatch.setattr("plugin.calc.python.function._get_calc_doc", lambda _ctx: doc)
    geo._GEOMETRIC_REPAIRING = True
    try:
        dispatch_sheet_modified(
            "ctx", "file:///reenter-mod.ods", "Sheet1", SimpleNamespace(Source=sheet)
        )
        assert _PENDING_TIMERS == {}
        assert _DeferredTimer.instances == []
    finally:
        geo._GEOMETRIC_REPAIRING = False


def test_data_edit_rebuilds_strip_safe_index(monkeypatch):
    """A data-edit that changes the PY list must rebuild unanimous-ours."""
    from plugin.calc.python.geometric_recalc import (
        current_geometric_strip_safe,
        geometric_workbook_key,
    )
    from plugin.calc.python.sheet_modify import (
        flush_sheet_modify_pass_for_tests,
        reset_sheet_modify_runtime_for_tests,
    )
    from plugin.tests.testing_utils import CalcDocStub

    _reset_geo()
    reset_sheet_modify_runtime_for_tests()
    doc = CalcDocStub(url="file:///data-edit-idx.ods")
    sheet = doc.getSheets().getByName("Sheet1")
    sheet.getCellByPosition(0, 0).setFormula('=PY("np.mean(data)"; B1:B10)')
    sheet.getCellByPosition(0, 1).setFormula('=PY("np.mean(data)"; B1:B10)')
    monkeypatch.setattr(
        "plugin.calc.python.geometric_recalc.geometric_flag_enabled", lambda: True
    )
    monkeypatch.setattr("plugin.doc.udprops.get_document_property", lambda *_a, **_k: None)
    monkeypatch.setattr("plugin.doc.udprops.set_document_property", lambda *_a, **_k: None)
    monkeypatch.setattr("plugin.calc.python.function._get_calc_doc", lambda _ctx: doc)
    flush_sheet_modify_pass_for_tests("ctx", doc, sheet)
    wk = geometric_workbook_key(doc)
    assert _key("np.mean(data)", 2, wk) in current_geometric_strip_safe()

    # User already passed the previous PY cell as real data — do not record.
    # Same (code, n_args=2) triple, mixed, poisons the chain.
    sheet.getCellByPosition(0, 2).setFormula('=PY("np.mean(data)"; B1:B10; A2)')
    flush_sheet_modify_pass_for_tests("ctx", doc, sheet)
    assert _key("np.mean(data)", 2, wk) not in current_geometric_strip_safe()


def test_ensure_listener_is_idempotent(monkeypatch):
    from plugin.calc.python.function import SHEET_MODIFY_LISTENERS
    from plugin.calc.python.sheet_modify import (
        ensure_sheet_modify_listener,
        reset_sheet_modify_runtime_for_tests,
    )
    from plugin.tests.testing_utils import CalcDocStub

    reset_sheet_modify_runtime_for_tests()
    SHEET_MODIFY_LISTENERS.clear()
    doc = CalcDocStub(url="file:///one-listener.ods")
    sheet = doc.getSheets().getByName("Sheet1")
    first = ensure_sheet_modify_listener("ctx", doc, sheet)
    second = ensure_sheet_modify_listener("ctx", doc, sheet)
    assert first is second
    assert len(sheet._modify_listeners) == 1
    SHEET_MODIFY_LISTENERS.clear()
