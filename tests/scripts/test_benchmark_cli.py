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


@pytest.mark.parametrize(
    ("env_var", "env_value", "expected_api_key"),
    [
        pytest.param("WRITERAGENT_API_KEY", "wa-key", "wa-key", id="writeragent"),
        pytest.param("OPENAI_API_KEY", "oai-key", "oai-key", id="openai"),
        pytest.param("OPENROUTER_API_KEY", "or-key", "or-key", id="openrouter"),
        pytest.param("ANTHROPIC_API_KEY", "ant-key", None, id="anthropic"),
        pytest.param("EVAL_ANTHROPIC_API_KEY", "eval-ant-key", None, id="eval_anthropic"),
    ],
)
def test_benchmark_main_api_key_reaches_subprocess(
    monkeypatch: pytest.MonkeyPatch, env_var: str, env_value: str, expected_api_key: str | None
) -> None:
    from unittest.mock import MagicMock
    import benchmark

    for name in (
        "EVAL_ANTHROPIC_API_KEY",
        "ANTHROPIC_API_KEY",
        "OPENROUTER_API_KEY",
        "OPENAI_API_KEY",
        "WRITERAGENT_API_KEY",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv(env_var, env_value)

    captured_cmd: list[str] = []

    def mock_run(cmd, **kwargs):
        captured_cmd[:] = list(cmd)
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
    assert captured_cmd
    if expected_api_key is None:
        assert "--api-key" not in captured_cmd
    else:
        assert "--api-key" in captured_cmd
        assert captured_cmd[captured_cmd.index("--api-key") + 1] == expected_api_key
