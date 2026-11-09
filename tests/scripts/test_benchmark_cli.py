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
        model="openai/gpt-oss-120b:nitro",
        api_key="sk-test",
        endpoint="https://openrouter.ai/api/v1",
        output="out.json",
        examples=2,
        parallel=1,
        verbose=True,
    )
    assert "run_eval_multi.py" in argv[1]
    assert "--models" in argv and "openai/gpt-oss-120b:nitro" in argv
    assert "--api-key" in argv and "sk-test" in argv
    assert "--api-base" in argv
    assert "-n" in argv and "2" in argv
    assert "--verbose" in argv
    assert "-j" in argv and "1" in argv


def test_build_eval_argv_default_parallel_is_20() -> None:
    argv = build_eval_argv(model="openai/gpt-oss-120b")
    assert argv[argv.index("-j") + 1] == "20"


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


def test_build_eval_argv_repeats_and_yes_all_models() -> None:
    argv = build_eval_argv(
        model="openai/gpt-oss-120b:nitro",
        repeats=3,
        yes_all_models=True,
    )
    assert "--repeats" in argv and "3" in argv
    assert "--yes-all-models" in argv
    assert "--temperature" not in argv


def test_build_eval_argv_task_not_implemented() -> None:
    with pytest.raises(ValueError, match="not implemented"):
        build_eval_argv(task="x", document="y")


def test_benchmark_main_anthropic_no_judge(monkeypatch: pytest.MonkeyPatch) -> None:
    from unittest.mock import MagicMock
    import benchmark

    monkeypatch.setenv("ANTHROPIC_API_KEY", "ant-key")
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("WRITERAGENT_API_KEY", raising=False)

    called = False

    def mock_run(cmd, **kwargs):
        nonlocal called
        called = True
        mock_res = MagicMock()
        mock_res.returncode = 0
        return mock_res

    monkeypatch.setattr("subprocess.run", mock_run)
    monkeypatch.setattr(
        sys,
        "argv",
        ["benchmark.py", "--model", "anthropic/claude-sonnet-4.5", "--no-judge"],
    )
    rc = benchmark.main()
    assert rc == 0
    assert called


def test_benchmark_main_anthropic_with_default_judge_missing_key_exits(monkeypatch: pytest.MonkeyPatch) -> None:
    import benchmark

    monkeypatch.setenv("ANTHROPIC_API_KEY", "ant-key")
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("WRITERAGENT_API_KEY", raising=False)

    monkeypatch.setattr(
        sys,
        "argv",
        ["benchmark.py", "--model", "anthropic/claude-sonnet-4.5"],
    )
    with pytest.raises(SystemExit):
        benchmark.main()


def test_benchmark_main_no_models_missing_key_exits(monkeypatch: pytest.MonkeyPatch) -> None:
    import benchmark

    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("WRITERAGENT_API_KEY", raising=False)

    monkeypatch.setattr(sys, "argv", ["benchmark.py"])
    with pytest.raises(SystemExit):
        benchmark.main()
