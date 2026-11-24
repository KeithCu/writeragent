# WriterAgent tests for eval dataset helpers
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

_PO = Path(__file__).resolve().parents[2] / "scripts" / "prompt_optimization"
if str(_PO) not in sys.path:
    sys.path.insert(0, str(_PO))

from dataset import ALL_EXAMPLES, to_dspy_examples, to_eval_examples  # noqa: E402


class _FakeExample:
    def __init__(self, **kwargs: object) -> None:
        self._fields = dict(kwargs)
        self._inputs: tuple[str, ...] = ()

    def with_inputs(self, *names: str) -> _FakeExample:
        self._inputs = names
        return self


def test_to_dspy_examples_matches_to_eval_examples(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "dspy", SimpleNamespace(Example=_FakeExample))
    eval_rows = to_eval_examples()
    dspy_rows = to_dspy_examples(ALL_EXAMPLES)
    assert len(dspy_rows) == len(eval_rows)
    for d_row, e_row in zip(dspy_rows, eval_rows, strict=True):
        assert d_row._fields == vars(e_row)
        assert d_row._inputs == ("document_content", "user_question", "task_id")
