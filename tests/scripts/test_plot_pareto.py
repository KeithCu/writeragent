# WriterAgent tests for scripts/prompt_optimization/plot_pareto.py
from __future__ import annotations

import sys
from pathlib import Path

_PO = Path(__file__).resolve().parents[2] / "scripts" / "prompt_optimization"
if str(_PO) not in sys.path:
    sys.path.insert(0, str(_PO))

import plot_pareto  # noqa: E402


def test_short_names_cover_nemotron_ultra_and_super() -> None:
    assert plot_pareto.SHORT_NAMES["nvidia/nemotron-3-ultra-550b-a55b"] == "Nemotron Ultra"
    assert plot_pareto.SHORT_NAMES["nvidia/nemotron-3-super-120b-a12b"] == "Nemotron Super"


def test_short_label_uses_catalog_correctness() -> None:
    ultra = {
        "openrouter_id": "nvidia/nemotron-3-ultra-550b-a55b",
        "avg_correctness": 0.8211764705882354,
    }
    super_row = {
        "openrouter_id": "nvidia/nemotron-3-super-120b-a12b",
        "avg_correctness": 0.9035294117647058,
    }
    assert plot_pareto._short_label(ultra) == "Nemotron Ultra 0.821"
    assert plot_pareto._short_label(super_row) == "Nemotron Super 0.904"
