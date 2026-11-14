# WriterAgent tests for scripts/prompt_optimization/model_configs.py
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_PO = Path(__file__).resolve().parents[2] / "scripts" / "prompt_optimization"
if str(_PO) not in sys.path:
    sys.path.insert(0, str(_PO))

import model_configs  # noqa: E402
from model_configs import MODELS, get_default_models  # noqa: E402


def test_get_default_models_drops_gold_only_ids(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        model_configs,
        "GOLD_ONLY_MODEL_IDS",
        frozenset({MODELS[0].openrouter_id}),
    )
    assert list(get_default_models()) == list(MODELS[1:])
