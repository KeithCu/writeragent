# WriterAgent tests for scripts/prompt_optimization/model_configs.py
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_PO = Path(__file__).resolve().parents[2] / "scripts" / "prompt_optimization"
if str(_PO) not in sys.path:
    sys.path.insert(0, str(_PO))

import model_configs  # noqa: E402
from model_configs import (  # noqa: E402
    DEFAULT_EVAL_STUDENT_MODEL,
    DEFAULT_GOLD_MODEL,
    GOLD_ONLY_MODEL_IDS,
    MODEL_BY_ID,
    MODELS,
    get_default_models,
)


def test_get_default_models_drops_gold_only_ids(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        model_configs,
        "GOLD_ONLY_MODEL_IDS",
        frozenset({MODELS[0].openrouter_id}),
    )
    assert list(get_default_models()) == list(MODELS[1:])


def test_default_eval_student_and_gold_models_are_in_catalog() -> None:
    from plugin.framework.openrouter_model_id import resolve_openrouter_catalog_id

    resolved = resolve_openrouter_catalog_id(DEFAULT_EVAL_STUDENT_MODEL, set(MODEL_BY_ID))
    assert resolved in MODEL_BY_ID
    assert resolved not in GOLD_ONLY_MODEL_IDS
    assert DEFAULT_GOLD_MODEL in MODEL_BY_ID
