# WriterAgent tests for scripts/benchmark.py
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[2]
_SCRIPTS = _REPO / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

from benchmark import build_eval_argv  # noqa: E402


def test_build_eval_argv_single_model() -> None:
    argv = build_eval_argv(
        model="qwen/qwen3-coder-next",
        api_key="sk-test",
        endpoint="https://openrouter.ai/api/v1",
        output="out.json",
        examples=2,
        parallel=1,
        verbose=True,
    )
    assert "run_eval_multi.py" in argv[1]
    assert "--models" in argv and "qwen/qwen3-coder-next" in argv
    assert "--api-key" in argv and "sk-test" in argv
    assert "--api-base" in argv
    assert "-n" in argv and "2" in argv
    assert "--verbose" in argv
    assert "-j" in argv and "1" in argv


def test_build_eval_argv_passthrough_extra() -> None:
    argv = build_eval_argv(
        models="a,b",
        extra=["--no-bust-cache", "--example", "table_from_mess"],
    )
    assert "--no-bust-cache" in argv
    assert "--example" in argv
    assert "table_from_mess" in argv


def test_build_eval_argv_allow_unknown() -> None:
    argv = build_eval_argv(model="llama3.2", allow_unknown_model=True)
    assert "--allow-unknown-model" in argv


def test_build_eval_argv_task_not_implemented() -> None:
    with pytest.raises(ValueError, match="not implemented"):
        build_eval_argv(task="x", document="y")
