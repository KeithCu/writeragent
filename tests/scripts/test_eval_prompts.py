# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Eval system prompt is the production chat builder plus the eval note."""

import pytest

from plugin.framework.prompts import (
    get_chat_system_prompt_for_document,
    get_chat_system_prompt_for_kind,
)
from scripts.prompt_optimization.eval_prompts import (
    EVAL_HARNESS_NOTE,
    FLAG_HARNESS_NOTE,
    _stub_calc,
    _stub_draw,
    _stub_writer,
    get_calc_eval_chat_system_prompt,
    get_draw_eval_chat_system_prompt,
    get_eval_system_prompt,
    get_writer_eval_chat_system_prompt,
    replace_prompt_slice,
)


@pytest.fixture(autouse=True)
def _register_prompt_hooks_for_eval_prompt_tests():
    from plugin.writeragent_hooks import register_writeragent_prompt_hooks

    register_writeragent_prompt_hooks()


def test_writer_eval_prompt_equals_production_plus_note() -> None:
    stub = _stub_writer()
    production = get_chat_system_prompt_for_document(stub, "", ctx=None)
    eval_p = get_writer_eval_chat_system_prompt()
    assert eval_p == get_chat_system_prompt_for_document(stub, EVAL_HARNESS_NOTE, ctx=None)
    assert eval_p.startswith(production)
    assert EVAL_HARNESS_NOTE in eval_p


def test_flag_prompt_requires_python_domain() -> None:
    prompt = get_eval_system_prompt("python_shapes_flag")
    assert FLAG_HARNESS_NOTE in prompt
    assert get_eval_system_prompt("table_from_mess") != prompt
    assert EVAL_HARNESS_NOTE in get_eval_system_prompt("table_from_mess")


def test_calc_draw_eval_prompts_use_production_builder() -> None:
    assert get_calc_eval_chat_system_prompt() == get_chat_system_prompt_for_document(
        _stub_calc(), EVAL_HARNESS_NOTE, ctx=None
    )
    assert get_draw_eval_chat_system_prompt() == get_chat_system_prompt_for_document(
        _stub_draw(), EVAL_HARNESS_NOTE, ctx=None
    )


def test_calc_eval_sort_line_omits_formula_tools() -> None:
    # Naming write_formula_range / =PY in the because-clause primed weak
    # models to call the tool we want them to avoid (glm-5.3-flash; see docs/eval/oss-20b-eval.md).
    calc = get_calc_eval_chat_system_prompt()
    sort_line = next(line for line in calc.splitlines() if "then range_sort" in line)
    assert "write_formula_range" not in sort_line
    assert "=PY" not in sort_line


def test_replace_prompt_slice_swaps_once_and_ignores_missing() -> None:
    assert replace_prompt_slice("aa CORE bb CORE", "CORE", "NEW") == "aa NEW bb CORE"
    assert replace_prompt_slice("aa bb", "CORE", "NEW") == "aa bb"
    assert replace_prompt_slice("aa CORE", "CORE", "CORE") == "aa CORE"


def test_eval_prompts_match_kind_builder_without_document_model() -> None:
    """Eval assembly must not need a MagicMock document (LoLane is off UNO thread)."""
    assert get_writer_eval_chat_system_prompt() == get_chat_system_prompt_for_kind(
        "writer", EVAL_HARNESS_NOTE, ctx=None
    )
    assert get_calc_eval_chat_system_prompt() == get_chat_system_prompt_for_kind(
        "calc", EVAL_HARNESS_NOTE, ctx=None
    )
    assert get_draw_eval_chat_system_prompt() == get_chat_system_prompt_for_kind(
        "draw", EVAL_HARNESS_NOTE, ctx=None
    )


def test_flag_prompt_safe_off_designated_thread_with_guard_on(monkeypatch) -> None:
    """GUARD on + LoLane-like thread must not raise on get_eval_system_prompt."""
    import threading

    from plugin.framework.queue_executor import main
    import plugin.framework.thread_guard as tg

    monkeypatch.setattr(tg, "GUARD_ON", True)
    # Designate a different thread as UNO home (simulates _lo_thread).
    holder: list[threading.Thread] = []
    ready = threading.Event()
    done = threading.Event()
    result: list[object] = []

    def uno_home() -> None:
        main.designated = threading.current_thread()
        holder.append(threading.current_thread())
        ready.set()
        done.wait(timeout=30)

    home = threading.Thread(target=uno_home, name="fake-lo-thread", daemon=True)
    home.start()
    assert ready.wait(timeout=30)

    def lane() -> None:
        try:
            prompt = get_eval_system_prompt("python_shapes_flag")
            result.append(prompt)
        except Exception as exc:
            result.append(exc)

    worker = threading.Thread(target=lane, name="eval-lo-lane-0", daemon=True)
    worker.start()
    worker.join(timeout=30)
    done.set()
    home.join(timeout=30)
    main.designated = None
    assert worker.is_alive() is False
    assert result and isinstance(result[0], str)
    assert FLAG_HARNESS_NOTE in result[0]
