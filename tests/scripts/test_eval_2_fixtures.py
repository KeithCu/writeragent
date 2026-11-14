# WriterAgent tests for eval-2 fixture and gold-tree parity
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Verify eval-2 experiment trees stay aligned with checked-in GDPval gold trees."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[2]
_GDPVAL = _REPO / "docs" / "eval" / "gdpval"
_EVAL2 = _REPO / "docs" / "eval" / "eval-2"

# Eval-2 smoother-path fixture intentionally renames Sheet1 -> Population, so it
# is not a byte copy of its GDPval reference file.
_MODIFIED_FIXTURES = {("afc-sample-83d10b06", "Population v2.xlsx")}

_ORACLE_TOTALS_BY_EXP = [
    ("writer-calc-peer-write", ("24,670.80", "1,320", "22,802.41")),
]


def _discover_experiments() -> list[tuple[str, Path]]:
    cases: list[tuple[str, Path]] = []
    for task_json in sorted(_EVAL2.glob("*/task.json")):
        row = json.loads(task_json.read_text(encoding="utf-8"))
        cases.append((row["task_id"], task_json.parent))
    return cases


def _discover_parity_pairs() -> list[tuple[str, Path, Path]]:
    pairs: list[tuple[str, Path, Path]] = []
    for task_id, exp_dir in _discover_experiments():
        gold_root = _GDPVAL / task_id
        for exp_sub, gold_sub in (
            ("gold", "deliverable_files"),
            ("fixtures", "reference_files"),
        ):
            exp_folder = exp_dir / exp_sub
            gold_folder = gold_root / gold_sub
            if not exp_folder.is_dir() or not gold_folder.is_dir():
                continue
            by_name = {p.name: p for p in gold_folder.rglob("*") if p.is_file()}
            for exp_file in sorted(exp_folder.iterdir()):
                if not exp_file.is_file() or exp_file.name not in by_name:
                    continue
                if exp_sub == "fixtures" and (exp_dir.name, exp_file.name) in _MODIFIED_FIXTURES:
                    continue
                pairs.append((exp_dir.name, exp_file, by_name[exp_file.name]))
    return pairs


_EXPERIMENTS = _discover_experiments()
_PARITY_PAIRS = _discover_parity_pairs()


def test_discovered_eval2_experiments_non_empty() -> None:
    assert len(_EXPERIMENTS) >= 8
    assert len(_PARITY_PAIRS) >= 20


@pytest.mark.parametrize(
    ("task_id", "exp_dir"),
    _EXPERIMENTS,
    ids=[exp.name for _, exp in _EXPERIMENTS],
)
def test_gold_prompt_is_byte_copy_of_hf_tree(task_id: str, exp_dir: Path) -> None:
    gold_dir = _GDPVAL / task_id
    gold = (gold_dir / "prompt.txt").read_bytes()
    exp = (exp_dir / "prompt.gdpval.txt").read_bytes()
    assert gold == exp
    row = json.loads((gold_dir / "task.json").read_text(encoding="utf-8"))
    assert row["task_id"] == task_id
    assert gold.decode("utf-8").rstrip("\n") == row["prompt"]


@pytest.mark.parametrize(
    ("exp_name", "exp_file", "gold_file"),
    _PARITY_PAIRS,
    ids=[f"{name}:{exp_f.name}" for name, exp_f, _ in _PARITY_PAIRS],
)
def test_experiment_file_matches_gold_bytes(
    exp_name: str, exp_file: Path, gold_file: Path
) -> None:
    del exp_name
    assert exp_file.read_bytes() == gold_file.read_bytes()


@pytest.mark.parametrize(
    ("exp_name", "forbidden_totals"),
    _ORACLE_TOTALS_BY_EXP,
    ids=[name for name, _ in _ORACLE_TOTALS_BY_EXP],
)
def test_writer_prompt_does_not_leak_oracle_totals(
    exp_name: str, forbidden_totals: tuple[str, ...]
) -> None:
    # Exact gold totals stay out of the product prompt (oracle cheat).
    text = (_EVAL2 / exp_name / "prompt.writeragent.txt").read_text(encoding="utf-8")
    for total in forbidden_totals:
        assert total not in text, f"{exp_name} prompt leaked oracle total {total!r}"


def test_native_slot_has_no_gdpval_gold_tree() -> None:
    # Per docs/eval/eval-2/long-writer-pack/SOURCE.md, this native slot must
    # not invent a fake GDPval id. Catalog short prefixes of claimed trees may
    # appear as rejected rows, so only full docs/eval/gdpval/<id>/ claims fail.
    claimed_ids = {task_id for task_id, _ in _EXPERIMENTS}
    gdpval_ids = {path.name for path in _GDPVAL.iterdir() if path.is_dir()}
    assert gdpval_ids <= claimed_ids

    exp_dir = _EVAL2 / "long-writer-pack"
    source = (exp_dir / "SOURCE.md").read_text(encoding="utf-8")
    for gold_id in claimed_ids:
        assert f"docs/eval/gdpval/{gold_id}/" not in source, gold_id
    assert not (exp_dir / "prompt.gdpval.txt").exists()
