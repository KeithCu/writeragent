# WriterAgent tests for scripts/eval_2_headed.py
from __future__ import annotations

import json
import sys
import tempfile
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

import pytest

_SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

from eval_2_debug_log import DEBUG_LOG_FILENAME  # noqa: E402
from eval_2_headed import (  # noqa: E402
    CADAVER_BUDGET_XLSX_NAME,
    CADAVER_PROPOSAL_NAME,
    CBA_EXCERPT_ODT_NAME,
    DEFAULT_MAX_TOOL_ROUNDS,
    DRAW_PRIMARY_ODG_NAME,
    EVAL_2_CALC_PRIMARY_MAX_TOOL_ROUNDS,
    EVAL_2_GMP_MAX_TOOL_ROUNDS,
    EVAL_2_MAX_TOOL_ROUNDS,
    FLOORSTAND_BUDGET_ODS_NAME,
    FLOORSTAND_EMAIL_NAME,
    FLOORSTAND_EMAIL_TRAIL_ODT_NAME,
    FLOORSTAND_MATRIX_ODS_NAME,
    FLOORSTAND_ORIG_ODS_NAME,
    GMP_COA_PDF_NAME,
    GMP_FORM_ODG_NAME,
    GMP_MEMO_NAME,
    GMP_SPEC_ODT_NAME,
    LETTER_ODT_NAME,
    LONG_DECISIONS_ODT_NAME,
    LONG_FACTS_ODT_NAME,
    LONG_PACK_NAME,
    MAX_TOOL_ROUNDS_KEY,
    PYTHON_SHAPES_ODT_NAME,
    PYTHON_SHAPES_STAMP_ODT,
    POPULATION_ODS_NAME,
    RAW_DATA_ODS_NAME,
    ROSTER_XLSX_NAME,
    SURVEY_XLSX_NAME,
    TASK_AFC,
    TASK_CALC_PRIMARY,
    TASK_CADAVER,
    TASK_DRAW,
    TASK_GMP,
    TASK_LONG,
    TASK_PYTHON_SHAPES,
    TASK_REVERSE,
    TASK_TENANT,
    TASK_WRITER_CALC,
    TENANT_MEMO_NAME,
    THEATRE_CBA_ODS_NAME,
    _AFC_DIR,
    _CALC_PRIMARY_DIR,
    _CADAVER_DIR,
    _DRAW_DIR,
    _FLOORSTAND_DIR,
    _GMP_DIR,
    _LONG_DIR,
    _PYTHON_SHAPES_DIR,
    _REVERSE_DIR,
    _TENANT_DIR,
    apply_max_tool_rounds,
    default_eval2_run_dir,
    default_eval2_trial_dir,
    find_writeragent_json,
    main as headed_main,
    read_max_tool_rounds,
    resolve_headed_run_dir,
    task_doc_dir,
    restore_max_tool_rounds,
    stage_calc_primary_trial,
    stage_cadaver_trial,
    stage_clean_trial_ods,
    stage_draw_primary_trial,
    stage_floorstand_trial,
    stage_gmp_trial,
    stage_long_writer_trial,
    stage_python_shapes_flag_trial,
    stage_reverse_tenant_trial,
    stage_tenant_trial,
    task_max_tool_rounds,
    temporary_max_tool_rounds,
)


def _write(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8")


def test_read_max_tool_rounds_absent_uses_default() -> None:
    assert read_max_tool_rounds({}) == DEFAULT_MAX_TOOL_ROUNDS


def test_apply_and_restore_removes_absent_key() -> None:
    data: dict[str, object] = {"text_model": "keep"}
    previous = apply_max_tool_rounds(data, 50)
    assert previous is None
    assert data[MAX_TOOL_ROUNDS_KEY] == 50
    restore_max_tool_rounds(data, previous)
    assert MAX_TOOL_ROUNDS_KEY not in data
    assert data["text_model"] == "keep"


def test_apply_and_restore_keeps_prior_value() -> None:
    data: dict[str, object] = {MAX_TOOL_ROUNDS_KEY: 20}
    previous = apply_max_tool_rounds(data, EVAL_2_MAX_TOOL_ROUNDS)
    assert previous == 20
    assert data[MAX_TOOL_ROUNDS_KEY] == 50
    restore_max_tool_rounds(data, previous)
    assert data[MAX_TOOL_ROUNDS_KEY] == 20


def test_temporary_max_tool_rounds_removes_key_when_absent(tmp_path: Path) -> None:
    config = tmp_path / "writeragent.json"
    _write(config, '{\n    "text_model": "keep-me"\n}\n')
    with temporary_max_tool_rounds(config):
        data = json.loads(config.read_text(encoding="utf-8"))
        assert data[MAX_TOOL_ROUNDS_KEY] == EVAL_2_MAX_TOOL_ROUNDS
        assert data["text_model"] == "keep-me"
    restored = json.loads(config.read_text(encoding="utf-8"))
    assert MAX_TOOL_ROUNDS_KEY not in restored
    assert restored["text_model"] == "keep-me"


def test_temporary_max_tool_rounds_restores_prior_value(tmp_path: Path) -> None:
    config = tmp_path / "writeragent.json"
    _write(config, json.dumps({MAX_TOOL_ROUNDS_KEY: 20, "endpoint": "http://x"}, indent=4) + "\n")
    with temporary_max_tool_rounds(config):
        assert json.loads(config.read_text(encoding="utf-8"))[MAX_TOOL_ROUNDS_KEY] == 50
    assert json.loads(config.read_text(encoding="utf-8"))[MAX_TOOL_ROUNDS_KEY] == 20


def test_temporary_max_tool_rounds_restores_after_error(tmp_path: Path) -> None:
    config = tmp_path / "writeragent.json"
    _write(config, '{"ok": true}\n')
    with pytest.raises(RuntimeError, match="boom"):
        with temporary_max_tool_rounds(config):
            raise RuntimeError("boom")
    assert MAX_TOOL_ROUNDS_KEY not in json.loads(config.read_text(encoding="utf-8"))


def test_temporary_max_tool_rounds_keeps_comment_header(tmp_path: Path) -> None:
    config = tmp_path / "writeragent.json"
    _write(config, "// schema\n{\n    \"text_model\": \"gpt\"\n}\n")
    with temporary_max_tool_rounds(config):
        text = config.read_text(encoding="utf-8")
        assert text.startswith("// schema")
        assert json.loads(text.split("\n", 1)[1])[MAX_TOOL_ROUNDS_KEY] == 50
    restored = config.read_text(encoding="utf-8")
    assert restored.startswith("// schema")
    assert MAX_TOOL_ROUNDS_KEY not in json.loads(restored.split("\n", 1)[1])


def test_find_writeragent_json_explicit(tmp_path: Path) -> None:
    path = tmp_path / "custom.json"
    _write(path, "{}")
    assert find_writeragent_json(path) == path


def test_find_writeragent_json_missing() -> None:
    with pytest.raises(FileNotFoundError, match="writeragent.json"):
        find_writeragent_json(candidates=[])


def _afc_style_tree(root: Path) -> Path:
    """Task dir + fixtures siblings that document_research would otherwise see."""
    fixtures = root / "fixtures"
    fixtures.mkdir(parents=True)
    (fixtures / POPULATION_ODS_NAME).write_bytes(b"ODS-BYTES")
    (fixtures / "Population v2.xlsx").write_bytes(b"XLSX")
    (fixtures / "min-range-too-large.ods").write_bytes(b"MIN")
    (root / "prompt.writeragent.txt").write_text("PROMPT-LEAK", encoding="utf-8")
    (root / "rubric_pretty.txt").write_text("RUBRIC-LEAK", encoding="utf-8")
    (root / "notes.md").write_text("NOTES-LEAK", encoding="utf-8")
    gold = root / "gold"
    gold.mkdir()
    (gold / "Sample v2.xlsx").write_bytes(b"GOLD")
    return fixtures / POPULATION_ODS_NAME


def test_stage_clean_trial_dir_contains_only_population_ods(tmp_path: Path) -> None:
    source = _afc_style_tree(tmp_path / "afc-sample")
    dest = tmp_path / "trial"
    result = stage_clean_trial_ods(source, dest)
    names = sorted(p.name for p in dest.iterdir())
    assert names == [POPULATION_ODS_NAME]
    assert result == dest / POPULATION_ODS_NAME
    assert result.read_bytes() == b"ODS-BYTES"


def test_stage_clean_trial_dir_wipes_leftovers(tmp_path: Path) -> None:
    source = _afc_style_tree(tmp_path / "afc-sample")
    dest = tmp_path / "trial"
    dest.mkdir()
    (dest / "prompt.writeragent.txt").write_text("old", encoding="utf-8")
    (dest / "extra.ods").write_bytes(b"extra")
    leftover_dir = dest / "notes"
    leftover_dir.mkdir()
    (leftover_dir / "rubric.txt").write_text("old", encoding="utf-8")
    stage_clean_trial_ods(source, dest)
    assert sorted(p.name for p in dest.iterdir()) == [POPULATION_ODS_NAME]


def test_stage_clean_trial_dir_refuses_fixture_folder(tmp_path: Path) -> None:
    source = _afc_style_tree(tmp_path / "afc-sample")
    with pytest.raises(ValueError, match="protected"):
        stage_clean_trial_ods(source, source.parent)


def test_stage_clean_trial_dir_refuses_real_afc_task_dir(tmp_path: Path) -> None:
    source = _afc_style_tree(tmp_path / "afc-sample")
    with pytest.raises(ValueError, match="protected"):
        stage_clean_trial_ods(source, _AFC_DIR)


def test_stage_clean_trial_dir_missing_source(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError, match="Population fixture"):
        stage_clean_trial_ods(tmp_path / "missing.ods", tmp_path / "trial")


def test_default_eval2_trial_dir_is_tmp_subdir() -> None:
    tmp_root = Path(tempfile.gettempdir())
    default_dir = default_eval2_trial_dir()
    assert default_dir.name == "writeragent-eval2-afc"
    assert default_dir.parent == tmp_root

    tasks = (
        TASK_TENANT,
        TASK_CADAVER,
        TASK_GMP,
        TASK_WRITER_CALC,
        TASK_CALC_PRIMARY,
        TASK_REVERSE,
        TASK_LONG,
        TASK_DRAW,
        TASK_PYTHON_SHAPES,
    )
    dirs = [default_dir, *(default_eval2_trial_dir(task) for task in tasks)]
    assert all(path.parent == tmp_root for path in dirs)
    assert all(path.name.startswith("writeragent-eval2-") for path in dirs)
    assert len({path.name for path in dirs}) == len(dirs)


def test_task_max_tool_rounds_routes_by_task() -> None:
    assert task_max_tool_rounds(TASK_TENANT) == EVAL_2_MAX_TOOL_ROUNDS
    assert task_max_tool_rounds(TASK_CADAVER) == EVAL_2_MAX_TOOL_ROUNDS
    assert task_max_tool_rounds(TASK_LONG) == EVAL_2_MAX_TOOL_ROUNDS
    assert task_max_tool_rounds(TASK_DRAW) == EVAL_2_MAX_TOOL_ROUNDS
    assert task_max_tool_rounds(TASK_PYTHON_SHAPES) == EVAL_2_MAX_TOOL_ROUNDS
    assert task_max_tool_rounds(TASK_GMP) == EVAL_2_GMP_MAX_TOOL_ROUNDS
    assert task_max_tool_rounds(TASK_WRITER_CALC) == EVAL_2_GMP_MAX_TOOL_ROUNDS
    assert task_max_tool_rounds(TASK_REVERSE) == EVAL_2_GMP_MAX_TOOL_ROUNDS
    assert task_max_tool_rounds(TASK_CALC_PRIMARY) == EVAL_2_CALC_PRIMARY_MAX_TOOL_ROUNDS


_StageTrialFn = Callable[[Path], Path | tuple[Path, ...]]

_STAGE_TRIAL_CASES: list[tuple[str, _StageTrialFn, Path, list[str], list[str]]] = [
    (
        "tenant",
        stage_tenant_trial,
        _TENANT_DIR,
        [LETTER_ODT_NAME, SURVEY_XLSX_NAME, TENANT_MEMO_NAME],
        [TENANT_MEMO_NAME],
    ),
    (
        "cadaver",
        stage_cadaver_trial,
        _CADAVER_DIR,
        [CADAVER_BUDGET_XLSX_NAME, CADAVER_PROPOSAL_NAME],
        [CADAVER_PROPOSAL_NAME],
    ),
    (
        "gmp",
        stage_gmp_trial,
        _GMP_DIR,
        [GMP_COA_PDF_NAME, GMP_SPEC_ODT_NAME, GMP_FORM_ODG_NAME, GMP_MEMO_NAME],
        [GMP_MEMO_NAME, GMP_FORM_ODG_NAME],
    ),
    (
        "floorstand",
        stage_floorstand_trial,
        _FLOORSTAND_DIR,
        [
            FLOORSTAND_EMAIL_TRAIL_ODT_NAME,
            FLOORSTAND_ORIG_ODS_NAME,
            FLOORSTAND_MATRIX_ODS_NAME,
            FLOORSTAND_BUDGET_ODS_NAME,
            FLOORSTAND_EMAIL_NAME,
        ],
        [FLOORSTAND_EMAIL_NAME, FLOORSTAND_BUDGET_ODS_NAME],
    ),
    (
        "calc_primary",
        stage_calc_primary_trial,
        _CALC_PRIMARY_DIR,
        [RAW_DATA_ODS_NAME],
        [RAW_DATA_ODS_NAME],
    ),
    (
        "reverse_tenant",
        stage_reverse_tenant_trial,
        _REVERSE_DIR,
        [CBA_EXCERPT_ODT_NAME, ROSTER_XLSX_NAME, THEATRE_CBA_ODS_NAME],
        [THEATRE_CBA_ODS_NAME, CBA_EXCERPT_ODT_NAME],
    ),
    (
        "long_writer",
        stage_long_writer_trial,
        _LONG_DIR,
        [LONG_FACTS_ODT_NAME, LONG_DECISIONS_ODT_NAME, LONG_PACK_NAME],
        [LONG_PACK_NAME],
    ),
    (
        "draw_primary",
        stage_draw_primary_trial,
        _DRAW_DIR,
        [DRAW_PRIMARY_ODG_NAME],
        [DRAW_PRIMARY_ODG_NAME],
    ),
    (
        "python_shapes_flag",
        stage_python_shapes_flag_trial,
        _PYTHON_SHAPES_DIR,
        [PYTHON_SHAPES_ODT_NAME],
        [PYTHON_SHAPES_ODT_NAME],
    ),
]


@pytest.mark.parametrize(
    ("stage_fn", "expected_names", "expected_returns"),
    [
        pytest.param(fn, names, returns, id=case_id)
        for case_id, fn, _task_dir, names, returns in _STAGE_TRIAL_CASES
    ],
)
def test_stage_trial_contains_only_expected_files(
    tmp_path: Path,
    stage_fn: _StageTrialFn,
    expected_names: list[str],
    expected_returns: list[str],
) -> None:
    dest = tmp_path / "trial"
    dest.mkdir()
    (dest / "prompt.writeragent.txt").write_text("PROMPT-LEAK", encoding="utf-8")
    (dest / "rubric.eval2.md").write_text("RUBRIC-LEAK", encoding="utf-8")
    (dest / "Process Flow Map.pdf").write_bytes(b"GOLD-PDF-LEAK")
    result = stage_fn(dest)
    returned = result if isinstance(result, tuple) else (result,)
    assert sorted(p.name for p in dest.iterdir()) == sorted(expected_names)
    assert list(returned) == [dest / name for name in expected_returns]
    assert all(p.is_file() for p in returned)


@pytest.mark.parametrize(
    ("stage_fn", "task_dir"),
    [
        pytest.param(fn, task_dir, id=case_id)
        for case_id, fn, task_dir, _names, _returns in _STAGE_TRIAL_CASES
    ],
)
def test_stage_trial_refuses_real_task_dir(stage_fn: _StageTrialFn, task_dir: Path) -> None:
    with pytest.raises(ValueError, match="protected"):
        stage_fn(task_dir)


def test_default_eval2_run_dir_uses_task_runs_stamp() -> None:
    now = datetime(2026, 9, 9, 17, 48)
    path = default_eval2_run_dir(TASK_WRITER_CALC, now=now)
    assert path == _FLOORSTAND_DIR / "runs" / "20260909-1748"
    assert resolve_headed_run_dir(TASK_AFC, None, now=now) == _AFC_DIR / "runs" / "20260909-1748"
    explicit = Path("/tmp/my-stamp")
    assert resolve_headed_run_dir(TASK_AFC, explicit) == explicit
    assert task_doc_dir(TASK_AFC) == _AFC_DIR


def test_launch_exit_snapshots_debug_log_into_run_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    config = tmp_path / "writeragent.json"
    config.write_text("{}\n", encoding="utf-8")
    live = tmp_path / "profile" / DEBUG_LOG_FILENAME
    live.parent.mkdir()
    live.write_text("Tool loop round 1: sending\n", encoding="utf-8")
    run_dir = tmp_path / "runs" / "20260909-1748"
    trial_dir = tmp_path / "trial"
    monkeypatch.setattr("eval_2_debug_log.debug_log_candidates", lambda: [live])
    monkeypatch.setattr("eval_2_headed.launch_calc", lambda fixture: None)
    monkeypatch.setattr("eval_2_headed._wait_for_finish", lambda rounds=50: None)
    assert headed_main([
        "--config",
        str(config),
        "--launch",
        "--trial-dir",
        str(trial_dir),
        "--run-dir",
        str(run_dir),
    ]) == 0
    dest = run_dir / DEBUG_LOG_FILENAME
    assert dest.is_file()
    assert dest.read_text(encoding="utf-8") == "Tool loop round 1: sending\n"
    assert "Restored previous" in capsys.readouterr().out
    assert MAX_TOOL_ROUNDS_KEY not in json.loads(config.read_text(encoding="utf-8"))


def test_python_shapes_launch_copies_odt_into_stamp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    config = tmp_path / "writeragent.json"
    config.write_text("{}\n", encoding="utf-8")
    run_dir = tmp_path / "runs" / "stamp"
    trial_dir = tmp_path / "trial"
    monkeypatch.setattr("eval_2_debug_log.debug_log_candidates", lambda: [tmp_path / "absent.log"])
    monkeypatch.setattr("eval_2_headed.launch_office", lambda mode, fixture: None)
    monkeypatch.setattr("eval_2_headed._wait_for_finish", lambda rounds=50: None)
    assert headed_main([
        "--config",
        str(config),
        "--task",
        TASK_PYTHON_SHAPES,
        "--launch",
        "--trial-dir",
        str(trial_dir),
        "--run-dir",
        str(run_dir),
    ]) == 0
    staged = trial_dir / PYTHON_SHAPES_ODT_NAME
    stamped = run_dir / PYTHON_SHAPES_STAMP_ODT
    assert staged.is_file()
    assert stamped.is_file()
    assert stamped.read_bytes() == staged.read_bytes()
    assert f"Saved {PYTHON_SHAPES_ODT_NAME}" in capsys.readouterr().out
    assert MAX_TOOL_ROUNDS_KEY not in json.loads(config.read_text(encoding="utf-8"))


def test_launch_exit_warns_when_debug_log_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    config = tmp_path / "writeragent.json"
    config.write_text("{}\n", encoding="utf-8")
    missing = tmp_path / "absent.log"
    run_dir = tmp_path / "runs" / "stamp"
    trial_dir = tmp_path / "trial"
    monkeypatch.setattr("eval_2_debug_log.debug_log_candidates", lambda: [missing])
    monkeypatch.setattr("eval_2_headed.launch_calc", lambda fixture: None)
    monkeypatch.setattr("eval_2_headed._wait_for_finish", lambda rounds=50: None)
    assert headed_main([
        "--config",
        str(config),
        "--launch",
        "--trial-dir",
        str(trial_dir),
        "--run-dir",
        str(run_dir),
    ]) == 0
    assert not (run_dir / DEBUG_LOG_FILENAME).exists()
    captured = capsys.readouterr()
    assert "Warning:" in captured.err
    assert "Restored previous" in captured.out
    assert MAX_TOOL_ROUNDS_KEY not in json.loads(config.read_text(encoding="utf-8"))

