# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Replay --student scripted on the full ALL_EXAMPLES pack (no API key)."""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[2]
_PO = _REPO / "scripts" / "prompt_optimization"
if str(_PO) not in sys.path:
    sys.path.insert(0, str(_PO))
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from dataset import ALL_EXAMPLES, to_eval_examples  # noqa: E402
from eval_core import example_passed, run_eval_on_examples_llm  # noqa: E402
from eval_scheduler import select_pack  # noqa: E402
from scripted_student import SCRIPTS  # noqa: E402

_RUN_EVAL = _PO / "run_eval.py"
_LO_CMD = (
    "python scripts/prompt_optimization/run_eval.py "
    "--backend lo --student scripted --no-bust-cache -v"
)


def test_scripts_cover_expanded_pack() -> None:
    ids = {ex["task_id"] for ex in ALL_EXAMPLES}
    assert len(ids) == 19
    assert {
        "style_consistency",
        "smart_summarization",
        "section_refactor",
        "comment_management",
        "py_refuse_overlap",
        "py_no_bulk_read",
        "org_chart_gen",
        "python_shapes_flag",
    } <= ids
    assert "py_unique_beside" not in ids
    assert "py_inplace_reframe" not in ids
    string_ids = {
        ex["task_id"] for ex in ALL_EXAMPLES if ex.get("backend", "string") != "lo"
    }
    assert string_ids <= set(SCRIPTS)
    # Flag is live-model LO only. org_chart_gen has a scripted LO replay.
    assert "python_shapes_flag" not in SCRIPTS
    assert "org_chart_gen" in SCRIPTS
    flag = next(ex for ex in ALL_EXAMPLES if ex["task_id"] == "python_shapes_flag")
    assert flag.get("backend") == "lo"
    org = next(ex for ex in ALL_EXAMPLES if ex["task_id"] == "org_chart_gen")
    assert org.get("backend") == "lo"


def test_scripted_string_pack_all_pass() -> None:
    # --backend string drops backend=lo rows (flag + org_chart). Replay the rest.
    selected = select_pack(
        to_eval_examples(ALL_EXAMPLES),
        cli_backend="string",
        explicit=False,
        student="scripted",
    )
    assert selected.error is None
    examples = selected.examples
    assert len(examples) == 17
    lo_ids = {"python_shapes_flag", "org_chart_gen"}
    assert all(getattr(ex, "task_id", "") not in lo_ids for ex in examples)
    results = run_eval_on_examples_llm(
        examples,
        endpoint="https://openrouter.ai/api/v1",
        api_key="",
        model="scripted",
        backend="string",
        student="scripted",
        no_judge=True,
        bust_cache=False,
        quiet=True,
    )
    assert len(results) == len(examples)
    failed = [
        (
            r.task_id,
            r.error,
            r.missing_expected,
            r.found_reject,
            r.oracle_failures,
        )
        for r in results
        if not example_passed(r)
    ]
    assert failed == [], failed
    process_failed = [
        (r.task_id, r.process_failures, r.agent_score)
        for r in results
        if r.agent_score != 1.0
    ]
    assert process_failed == [], process_failed


def test_eval_task_banner_includes_model(capsys: pytest.CaptureFixture[str]) -> None:
    one = [ex for ex in ALL_EXAMPLES if ex["task_id"] == "comment_management"]
    assert one
    run_eval_on_examples_llm(
        to_eval_examples(one),
        endpoint="https://openrouter.ai/api/v1",
        api_key="",
        model="openai/gpt-oss-120b",
        backend="string",
        student="scripted",
        no_judge=True,
        bust_cache=False,
        quiet=False,
    )
    out = capsys.readouterr().out
    assert "--- [1/1] comment_management  model=openai/gpt-oss-120b ---" in out


def _lo_eval_available() -> str | None:
    """Return a skip reason, or None if headless LO eval can run.

    Probe a fresh interpreter so tests/conftest.py's mocked ``uno`` is not used.
    Never set WRITERAGENT_TESTING=1 (QueueExecutor on the wrong thread).
    """
    if os.environ.get("WRITERAGENT_TESTING") == "1":
        return "WRITERAGENT_TESTING=1 uses QueueExecutor; unset it for LO eval"
    if not shutil.which("soffice"):
        return f"soffice not on PATH. Local: {_LO_CMD}"
    if not (_REPO / "plugin" / "_manifest.py").is_file():
        return f"plugin._manifest.py missing (make manifest). Local: {_LO_CMD}"
    probe = subprocess.run(
        [sys.executable, "-c", "import uno, unohelper"],
        cwd=_REPO,
        capture_output=True,
        text=True,
        env={k: v for k, v in os.environ.items() if k != "WRITERAGENT_TESTING"},
    )
    if probe.returncode != 0:
        return f"python-uno not importable (make ensure-uno). Local: {_LO_CMD}"
    return None


# Live soffice eval. Keep off `make pytest` (`-m "not integration"`); run via
# `-m integration` or `python scripts/prompt_optimization/run_eval.py --backend lo`.
@pytest.mark.integration
def test_scripted_lo_pack_all_pass() -> None:
    reason = _lo_eval_available()
    if reason:
        pytest.skip(reason)
    env = {k: v for k, v in os.environ.items() if k != "WRITERAGENT_TESTING"}
    env["WRITERAGENT_EVAL_HARNESS"] = "1"
    proc = subprocess.run(
        [
            sys.executable,
            str(_RUN_EVAL),
            "--backend",
            "lo",
            "--student",
            "scripted",
            "--no-bust-cache",
        ],
        cwd=_REPO,
        capture_output=True,
        text=True,
        env=env,
        timeout=300,
    )
    out = (proc.stdout or "") + "\n" + (proc.stderr or "")
    assert proc.returncode == 0, out
    # =PY dest rows are string-harness only. Flag has no script (dropped).
    # org_chart_gen has a scripted LO replay and stays in the pack.
    selected = select_pack(
        to_eval_examples(ALL_EXAMPLES),
        cli_backend="lo",
        explicit=False,
        student="scripted",
    )
    n = len(selected.examples)
    ids = {getattr(ex, "task_id", "") for ex in selected.examples}
    assert "python_shapes_flag" not in ids
    assert "org_chart_gen" in ids
    assert f"Scripted result pass: {n}/{n}" in out, out
    assert "Skipping python_shapes_flag" in out
