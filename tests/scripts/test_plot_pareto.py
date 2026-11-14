# WriterAgent tests for scripts/prompt_optimization/plot_pareto.py
from __future__ import annotations

import sys
from pathlib import Path

_PO = Path(__file__).resolve().parents[2] / "scripts" / "prompt_optimization"
if str(_PO) not in sys.path:
    sys.path.insert(0, str(_PO))

import plot_pareto  # noqa: E402


def test_short_label_uses_short_name_or_id_tail_and_three_decimals() -> None:
    known_id = next(iter(plot_pareto.SHORT_NAMES))
    known_row = {
        "openrouter_id": known_id,
        "avg_correctness": 0.8211764705882354,
    }
    unknown_row = {
        "openrouter_id": "acme/foo-bar",
        "avg_correctness": 0.8211764705882354,
    }
    assert plot_pareto._short_label(known_row) == f"{plot_pareto.SHORT_NAMES[known_id]} 0.821"
    assert plot_pareto._short_label(unknown_row) == "foo-bar 0.821"
