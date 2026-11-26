# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""CLI guards for run_eval_multi (no paid API)."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_PO = Path(__file__).resolve().parents[2] / "scripts" / "prompt_optimization"
if str(_PO) not in sys.path:
    sys.path.insert(0, str(_PO))

import run_eval_multi  # noqa: E402


@pytest.fixture(autouse=True)
def _clear_provider_env_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in (
        "EVAL_ANTHROPIC_API_KEY",
        "ANTHROPIC_API_KEY",
        "EVAL_GEMINI_API_KEY",
        "GEMINI_API_KEY",
    ):
        monkeypatch.delenv(var, raising=False)


def test_parse_args_defaults_and_overrides() -> None:
    defaults = run_eval_multi.parse_args([])
    assert defaults.tools == "full"
    assert defaults.schema_density == "full"
    assert defaults.lo_workers == 4

    args = run_eval_multi.parse_args(
        [
            "--models",
            "openai/gpt-oss-20b",
            "--tools",
            "calc_core",
            "--schema-density",
            "skinny",
            "--lo-workers",
            "5",
        ]
    )
    assert args.tools == "calc_core"
    assert args.schema_density == "skinny"
    assert args.models == "openai/gpt-oss-20b"
    assert args.lo_workers == 5


def test_out_path_relative_is_cwd(monkeypatch, tmp_path) -> None:
    monkeypatch.chdir(tmp_path)
    args = type("A", (), {"out": "scripts/prompt_optimization/eval_results_17task.csv"})()
    got = run_eval_multi._out_path(args)
    assert got == tmp_path / "scripts/prompt_optimization/eval_results_17task.csv"
    # A Unix-only abs path is not drive-absolute on Windows (Path.cwd() / p
    # becomes C:/tmp/eval.csv). Use a real filesystem abs path like _out_path.
    abs_out = str(tmp_path / "abs_eval.csv")
    abs_args = type("A", (), {"out": abs_out})()
    assert run_eval_multi._out_path(abs_args) == Path(abs_out).resolve()


def test_nitro_student_reuses_base_catalog_pricing(monkeypatch) -> None:
    from model_configs import ModelConfig

    base = ModelConfig(
        openrouter_id="synthetic/base-model",
        display_name="Synthetic Base",
        context_window_tokens=131_072,
        input_cost_per_million=0.25,
        output_cost_per_million=1.50,
    )
    monkeypatch.setitem(run_eval_multi.MODEL_BY_ID, base.openrouter_id, base)
    cfg = run_eval_multi._model_config_for_id("synthetic/base-model:nitro")
    assert cfg.openrouter_id == "synthetic/base-model:nitro"
    assert cfg.input_cost_per_million == base.input_cost_per_million
    assert cfg.output_cost_per_million == base.output_cost_per_million


def test_model_config_for_anthropic_zero_pricing() -> None:
    cfg = run_eval_multi._model_config_for_id("anthropic/claude-sonnet-4.5")
    assert cfg.input_cost_per_million == 0.0
    assert cfg.output_cost_per_million == 0.0


def test_model_config_for_gemini_zero_pricing() -> None:
    cfg = run_eval_multi._model_config_for_id("google/gemini-2.5-flash")
    assert cfg.input_cost_per_million == 0.0
    assert cfg.output_cost_per_million == 0.0


@pytest.mark.parametrize(
    ("endpoint", "env_var", "model_id", "accepted"),
    [
        ("https://openrouter.ai/api/v1", "ANTHROPIC_API_KEY", "claude-sonnet-4-5", True),
        (
            "https://openrouter.ai/api/v1",
            "EVAL_ANTHROPIC_API_KEY",
            "anthropic/claude-sonnet-4.5:nitro",
            True,
        ),
        ("https://openrouter.ai/api/v1", "GEMINI_API_KEY", "gemini-2.5-flash", True),
        (
            "https://openrouter.ai/api/v1",
            "EVAL_GEMINI_API_KEY",
            "google/gemini-2.5-flash:free",
            True,
        ),
        ("https://openrouter.ai/api/v1", None, "google/gemini-2.5-flash", False),
        ("https://openrouter.ai/api/v1", None, "anthropic/claude-sonnet-4.5", False),
        ("http://127.0.0.1:11434/v1", "GEMINI_API_KEY", "google/gemini-2.5-flash", False),
        ("http://127.0.0.1:11434/v1", "ANTHROPIC_API_KEY", "claude-sonnet-4-5", False),
        ("https://api.anthropic.com/v1", None, "claude-sonnet-4-5", True),
        ("https://api.anthropic.com/v1", None, "google/gemini-2.5-flash", False),
    ],
)
def test_filter_unknown_models_follows_route(
    monkeypatch: pytest.MonkeyPatch,
    endpoint: str,
    env_var: str | None,
    model_id: str,
    accepted: bool,
) -> None:
    if env_var is not None:
        monkeypatch.setenv(env_var, "sk-test")
    unknown = run_eval_multi._filter_unknown_models([model_id, "not-a-model"], endpoint=endpoint)
    expected = ["not-a-model"] if accepted else [model_id, "not-a-model"]
    assert unknown == expected


def test_main_refuses_unlisted_gemini_staying_on_openrouter(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import llm_chat_eval

    monkeypatch.setattr(llm_chat_eval, "enable_default_transcripts", lambda: None)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "run_eval_multi.py",
            "--models",
            "google/gemini-2.5-flash",
            "--api-base",
            "https://openrouter.ai/api/v1",
            "--student",
            "scripted",
        ],
    )
    assert run_eval_multi.main() == 1
    assert "google/gemini-2.5-flash" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("env_key", "extra_args", "expected_label", "forbidden_label"),
    [
        ("sk-gem", [], "native Gemini", "(--allow-unknown-model)"),
        (None, ["--allow-unknown-model"], "(--allow-unknown-model)", "native Gemini"),
    ],
)
def test_main_pricing_label_follows_route(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    env_key: str | None,
    extra_args: list[str],
    expected_label: str,
    forbidden_label: str,
) -> None:
    import llm_chat_eval

    if env_key is not None:
        monkeypatch.setenv("GEMINI_API_KEY", env_key)
    monkeypatch.setattr(llm_chat_eval, "enable_default_transcripts", lambda: None)
    monkeypatch.setattr(
        run_eval_multi,
        "_run_one_model",
        lambda model_id, *_args, **_kwargs: {
            "summary": _pareto_row(
                openrouter_id=model_id,
                pricing_known=False,
                total_cost_usd=0.0,
            ),
            "details": [],
        },
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "run_eval_multi.py",
            "--models",
            "google/gemini-2.5-flash",
            "--api-base",
            "https://openrouter.ai/api/v1",
            "--student",
            "scripted",
            "-e",
            "table_from_mess",
            "-j",
            "1",
            "--out",
            str(tmp_path / "o.json"),
            *extra_args,
        ],
    )
    assert run_eval_multi.main() == 0
    out = capsys.readouterr().out
    assert expected_label in out
    assert forbidden_label not in out


def test_refuse_catalog_sweep_without_models(monkeypatch, capsys) -> None:
    monkeypatch.setattr(
        sys, "argv", ["run_eval_multi.py", "--student", "scripted"]
    )
    assert run_eval_multi.main() == 1
    err = capsys.readouterr().err
    assert "--models" in err
    assert "--yes-all-models" in err


def test_generate_golds_passes_task_id_and_kind_prompt(monkeypatch, tmp_path) -> None:
    """Draw/Calc golds pass their task_id and get their own system prompt, not Writer's."""
    import llm_chat_eval
    from eval_prompts import get_eval_system_prompt

    captured: list[dict] = []

    def fake_run(**kwargs):
        captured.append(kwargs)
        return '{"status": "ok"}', {"total_tokens": 1}, None, []

    monkeypatch.setattr(llm_chat_eval, "run_llm_chat_eval", fake_run)
    monkeypatch.setattr(run_eval_multi, "SCRIPT_DIR", tmp_path)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "run_eval_multi.py",
            "--generate-golds",
            "-e",
            "flowchart_gen",
            "--student",
            "scripted",
        ],
    )
    assert run_eval_multi.main() == 0
    assert captured
    assert captured[0]["task_id"] == "flowchart_gen"
    assert captured[0]["system_prompt"] == get_eval_system_prompt("flowchart_gen")
    assert captured[0]["system_prompt"] != get_eval_system_prompt("table_from_mess")
    golds = (tmp_path / "gold_standards.json").read_text(encoding="utf-8")
    assert "flowchart_gen" in golds


def _pareto_row(**overrides: object) -> dict:
    row: dict = {
        "openrouter_id": "model/a",
        "pricing_known": True,
        "n_examples": 17,
        "n_error": 0,
        "avg_correctness": 0.90,
        "avg_cost_per_example": 0.01,
    }
    row.update(overrides)
    return row


def test_pareto_strict_dominance() -> None:
    cheap = _pareto_row(openrouter_id="cheap", avg_correctness=0.95, avg_cost_per_example=0.001)
    expensive = _pareto_row(
        openrouter_id="expensive", avg_correctness=0.80, avg_cost_per_example=0.01
    )
    run_eval_multi.annotate_pareto_status([cheap, expensive])
    assert cheap["pareto_status"] == run_eval_multi.PARETO_FRONTIER
    assert expensive["pareto_status"] == run_eval_multi.PARETO_DOMINATED


def test_pareto_quality_cost_tradeoff_both_frontier() -> None:
    cheap = _pareto_row(openrouter_id="oss", avg_correctness=0.971, avg_cost_per_example=0.00054)
    better = _pareto_row(openrouter_id="grok", avg_correctness=0.982, avg_cost_per_example=0.04653)
    run_eval_multi.annotate_pareto_status([cheap, better])
    assert cheap["pareto_status"] == run_eval_multi.PARETO_FRONTIER
    assert better["pareto_status"] == run_eval_multi.PARETO_FRONTIER


def test_pareto_equal_cost_higher_correctness_dominates() -> None:
    worse = _pareto_row(openrouter_id="worse", avg_correctness=0.80, avg_cost_per_example=0.01)
    better = _pareto_row(openrouter_id="better", avg_correctness=0.90, avg_cost_per_example=0.01)
    run_eval_multi.annotate_pareto_status([worse, better])
    assert better["pareto_status"] == run_eval_multi.PARETO_FRONTIER
    assert worse["pareto_status"] == run_eval_multi.PARETO_DOMINATED


def test_pareto_equal_correctness_cheaper_dominates() -> None:
    cheap = _pareto_row(openrouter_id="cheap", avg_correctness=0.90, avg_cost_per_example=0.001)
    dear = _pareto_row(openrouter_id="dear", avg_correctness=0.90, avg_cost_per_example=0.01)
    run_eval_multi.annotate_pareto_status([cheap, dear])
    assert cheap["pareto_status"] == run_eval_multi.PARETO_FRONTIER
    assert dear["pareto_status"] == run_eval_multi.PARETO_DOMINATED


def test_pareto_exact_ties_remain_frontier() -> None:
    a = _pareto_row(openrouter_id="a", avg_correctness=0.90, avg_cost_per_example=0.01)
    b = _pareto_row(openrouter_id="b", avg_correctness=0.90, avg_cost_per_example=0.01)
    run_eval_multi.annotate_pareto_status([a, b])
    assert a["pareto_status"] == run_eval_multi.PARETO_FRONTIER
    assert b["pareto_status"] == run_eval_multi.PARETO_FRONTIER


def test_pareto_unknown_pricing_and_failed_runs_unavailable() -> None:
    unknown = _pareto_row(openrouter_id="unknown", pricing_known=False)
    failed = _pareto_row(openrouter_id="failed", n_error=17, avg_correctness=0.0)
    empty = _pareto_row(openrouter_id="empty", n_examples=0, n_error=1)
    ok = _pareto_row(openrouter_id="ok")
    run_eval_multi.annotate_pareto_status([unknown, failed, empty, ok])
    assert unknown["pareto_status"] == run_eval_multi.PARETO_UNAVAILABLE
    assert failed["pareto_status"] == run_eval_multi.PARETO_UNAVAILABLE
    assert empty["pareto_status"] == run_eval_multi.PARETO_UNAVAILABLE
    assert ok["pareto_status"] == run_eval_multi.PARETO_FRONTIER


def test_pareto_write_results_includes_status(tmp_path: Path) -> None:
    import json

    cheap = _pareto_row(openrouter_id="cheap", avg_correctness=0.95, avg_cost_per_example=0.001)
    dear = _pareto_row(openrouter_id="dear", avg_correctness=0.80, avg_cost_per_example=0.02)
    summaries = [cheap, dear]
    run_eval_multi.annotate_pareto_status(summaries)
    out = tmp_path / "pareto.json"
    run_eval_multi._write_results(out, summaries)
    loaded = json.loads(out.read_text(encoding="utf-8"))
    assert loaded[0]["n_examples"] == 17
    assert {row["openrouter_id"]: row["pareto_status"] for row in loaded} == {
        "cheap": run_eval_multi.PARETO_FRONTIER,
        "dear": run_eval_multi.PARETO_DOMINATED,
    }


def test_pareto_successive_fronts_chain() -> None:
    a = _pareto_row(openrouter_id="a", avg_correctness=0.95, avg_cost_per_example=0.001)
    b = _pareto_row(openrouter_id="b", avg_correctness=0.85, avg_cost_per_example=0.005)
    c = _pareto_row(openrouter_id="c", avg_correctness=0.75, avg_cost_per_example=0.01)
    run_eval_multi.annotate_pareto_fronts([a, b, c])
    assert a["pareto_front"] == 1
    assert b["pareto_front"] == 2
    assert c["pareto_front"] == 3


def test_pareto_tradeoff_and_dominated_cheap_model() -> None:
    cheap = _pareto_row(openrouter_id="oss", avg_correctness=0.971, avg_cost_per_example=0.00054)
    better = _pareto_row(openrouter_id="grok", avg_correctness=0.982, avg_cost_per_example=0.04653)
    worse = _pareto_row(openrouter_id="solar", avg_correctness=0.682, avg_cost_per_example=0.00065)
    run_eval_multi.annotate_pareto_fronts([cheap, better, worse])
    assert cheap["pareto_front"] == 1
    assert better["pareto_front"] == 1
    assert worse["pareto_front"] >= 2


def test_pareto_f1_distance_zero_and_ordering() -> None:
    cheap = _pareto_row(openrouter_id="oss", avg_correctness=0.971, avg_cost_per_example=0.00054)
    better = _pareto_row(openrouter_id="grok", avg_correctness=0.982, avg_cost_per_example=0.04653)
    near = _pareto_row(openrouter_id="near", avg_correctness=0.90, avg_cost_per_example=0.002)
    far = _pareto_row(openrouter_id="far", avg_correctness=0.60, avg_cost_per_example=0.02)
    summaries = [cheap, better, near, far]
    run_eval_multi.annotate_pareto_fronts(summaries)
    distances = run_eval_multi.pareto_f1_distances(summaries)
    assert distances[id(cheap)] == 0.0
    assert distances[id(better)] == 0.0
    assert distances[id(near)] < distances[id(far)]


def test_pareto_tradeoff_score_f1_is_one() -> None:
    cheap = _pareto_row(openrouter_id="oss", avg_correctness=0.971, avg_cost_per_example=0.00054)
    better = _pareto_row(openrouter_id="grok", avg_correctness=0.982, avg_cost_per_example=0.04653)
    summaries = [cheap, better]
    run_eval_multi.annotate_pareto_fronts(summaries)
    scores = run_eval_multi.pareto_tradeoff_scores(summaries)
    assert scores[id(cheap)] == 1.0
    assert scores[id(better)] == 1.0


def test_catalog_pricing_from_token_total_does_not_change_correctness() -> None:
    """Summaries that recorded only total_tokens get an 85/15 input/output estimate without altering correctness."""
    from model_configs import ModelConfig

    cfg = ModelConfig(
        openrouter_id="synthetic/ultra",
        display_name="Synthetic Ultra",
        context_window_tokens=131_072,
        input_cost_per_million=1.0,
        output_cost_per_million=4.0,
    )
    corr = 0.80
    row = {
        "openrouter_id": cfg.openrouter_id,
        "avg_correctness": corr,
        "avg_metric_score": 0.50,
        "hard_pass_rate": 0.82,
        "n_examples": 10,
        "total_tokens": 1_000_000,
        "pricing_known": False,
        "avg_cost_per_example": 0.0,
        "intelligence_per_dollar_correctness": 0.0,
    }

    run_eval_multi.apply_catalog_pricing_to_summary(row, cfg)

    assert row["avg_correctness"] == corr
    assert row["hard_pass_rate"] == 0.82
    assert row["pricing_known"] is True
    assert row["input_cost_per_million"] == cfg.input_cost_per_million
    assert row["output_cost_per_million"] == cfg.output_cost_per_million
    expected_cost = (0.85 * 1.0 + 0.15 * 4.0) / 10
    assert row["avg_cost_per_example"] == pytest.approx(expected_cost)
    assert row["intelligence_per_dollar_correctness"] == pytest.approx(
        (corr ** 2) / row["avg_cost_per_example"]
    )


def test_pareto_tradeoff_score_decreases_with_distance() -> None:
    cheap = _pareto_row(openrouter_id="oss", avg_correctness=0.971, avg_cost_per_example=0.00054)
    better = _pareto_row(openrouter_id="grok", avg_correctness=0.982, avg_cost_per_example=0.04653)
    near = _pareto_row(openrouter_id="near", avg_correctness=0.90, avg_cost_per_example=0.002)
    far = _pareto_row(openrouter_id="far", avg_correctness=0.60, avg_cost_per_example=0.02)
    summaries = [cheap, better, near, far]
    run_eval_multi.annotate_pareto_fronts(summaries)
    distances = run_eval_multi.pareto_f1_distances(summaries)
    scores = run_eval_multi.pareto_tradeoff_scores(summaries)
    assert scores[id(near)] > scores[id(far)]
    for row_id, dist in distances.items():
        assert scores[row_id] == max(0.0, 1.0 - dist)


def test_scripted_run_needs_no_dspy(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import json
    import llm_chat_eval

    monkeypatch.setitem(sys.modules, "dspy", None)
    monkeypatch.setattr(llm_chat_eval, "enable_default_transcripts", lambda: None)
    out_path = tmp_path / "o.json"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "run_eval_multi.py",
            "--models",
            "openai/gpt-oss-20b",
            "--api-base",
            "https://openrouter.ai/api/v1",
            "--student",
            "scripted",
            "-e",
            "table_from_mess",
            "-j",
            "1",
            "--no-bust-cache",
            "--out",
            str(out_path),
        ],
    )
    assert run_eval_multi.main() == 0
    payload = json.loads(out_path.read_text(encoding="utf-8"))
    summary = payload[0]
    assert summary["n_examples"] == 1
    assert summary["n_error"] == 0
    assert summary["hard_pass_rate"] == 1.0
