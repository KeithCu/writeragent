# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Unit tests for native-runner progress and CLI filter helpers."""

from __future__ import annotations

import json
import os
import sys
import types
from pathlib import Path
from typing import Any

import pytest

import plugin.framework.config as config_mod
from plugin.framework.event_bus import get_event_bus
import plugin.testing_runner as tr

from plugin.testing_runner import (
    _APPLICATION_ERROR_MARKER,
    _cli_filters,
    _exit_after_summary,
    _fail_reason,
    _fail_reason_with_lifecycle,
    _function_name_matches,
    _is_case_id,
    _module_matches_filters,
    _native_suite_set_config,
    _native_suite_sort_key,
    _test_function_filters,
    collect_post_test_death,
    consume_application_error,
    consume_office_recycle_request,
    expand_soak_pair,
    format_lifecycle_breadcrumb,
    note_office_stderr_line,
    probe_uno_bridge,
    record_test_end,
    record_test_start,
    request_office_recycle_after_suite,
    reset_lifecycle_breadcrumb,
    reset_office_death_signals,
    soffice_exit_code,
)


def test_test_function_filters_skips_module_path_tokens() -> None:
    assert _test_function_filters(["test_cells_uno", "tests/calc/foo.py"]) == []
    assert _test_function_filters(["test_read_range_format_info_performance"]) == [
        "test_read_range_format_info_performance"
    ]


def test_test_function_filters_accepts_packet_letter_and_case_id() -> None:
    assert _test_function_filters(["E", "f3a", "B"]) == ["E", "f3a", "B"]
    assert _test_function_filters(["tests/chatbot/test_mock_llm_sidebar_uno.py", "E"]) == ["E"]


def test_is_case_id() -> None:
    assert _is_case_id("f3a") is True
    assert _is_case_id("e9") is True
    assert _is_case_id("b1a") is True
    assert _is_case_id("f") is False
    assert _is_case_id("f10") is True
    assert _is_case_id("test_f1") is False


def test_function_name_matches_packet_letter() -> None:
    assert _function_name_matches("test_f18_event_ping_then_hello", ["F"]) is True
    assert _function_name_matches("test_f3a_hang_the_stream_then_hello", ["f"]) is True
    assert _function_name_matches("test_b1a_stop_ramble_then_hello", ["B"]) is True
    assert _function_name_matches("test_e9c_hitl_change", ["E"]) is True
    assert _function_name_matches("test_c1_say_nothing_truncated_then_hello", ["C"]) is True
    assert _function_name_matches("test_d1_think_out_loud_thinking_then_html", ["D"]) is True
    assert _function_name_matches("test_foo_bar", ["F"]) is False
    assert _function_name_matches("test_e7_outline_delegate", ["B"]) is False
    assert _function_name_matches("test_p1_total_row_peer_roundtrip", ["P"]) is True
    assert _function_name_matches("test_p2_wait_after_accepted_deadlocks_peer", ["p"]) is True
    assert _function_name_matches("test_p3_busy_then_queue_reply", ["P"]) is True
    assert _function_name_matches("test_p3_writer_busy_queues_calc_reply", ["P"]) is True
    assert _function_name_matches("test_p3_writer_busy_queues_calc_reply", ["p3"]) is True
    assert _function_name_matches("test_panel_factory", ["P"]) is False


def test_function_name_matches_case_id_no_prefix_bleed() -> None:
    assert _function_name_matches("test_f1_crash_the_stream_then_hello", ["f1"]) is True
    assert _function_name_matches("test_f10_truncated_json_then_hello", ["f1"]) is False
    assert _function_name_matches("test_f10_truncated_json_then_hello", ["f10"]) is True
    assert _function_name_matches("test_e9a_hitl_accept", ["e9"]) is False
    assert _function_name_matches("test_e9a_hitl_accept", ["e9a"]) is True


def test_function_name_matches_full_test_name() -> None:
    name = "test_e7_outline_delegate"
    assert _function_name_matches(name, [name]) is True
    assert _function_name_matches("test_e7_outline_delegate_extra", ["test_e7_outline_delegate"]) is True
    assert _function_name_matches("test_e70_other", ["test_e7"]) is False


def test_module_matches_filters_by_path_or_def_name(tmp_path: Path) -> None:
    path = tmp_path / "test_cells_uno.py"
    path.write_text("def test_read_range_format_info_performance(ctx, doc):\n    return\n", encoding="utf-8")
    full = str(path)
    assert _module_matches_filters(full, path.name, ["test_cells_uno"]) is True
    assert _module_matches_filters(full, path.name, ["test_read_range_format_info_performance"]) is True
    assert _module_matches_filters(full, path.name, ["test_unrelated_other"]) is False


def test_module_matches_filters_by_packet_letter(tmp_path: Path) -> None:
    path = tmp_path / "test_mock_llm_sidebar_uno.py"
    path.write_text(
        "def test_f1_crash(ctx):\n    return\n\ndef test_e7_outline(ctx):\n    return\n",
        encoding="utf-8",
    )
    full = str(path)
    assert _module_matches_filters(full, path.name, ["E"]) is True
    assert _module_matches_filters(full, path.name, ["B"]) is False
    assert _module_matches_filters(full, path.name, ["f1"]) is True
    assert _module_matches_filters(full, path.name, ["f10"]) is False


def test_cli_filters_default_empty() -> None:
    assert isinstance(_cli_filters, list)


def test_lifecycle_breadcrumb_names_previous_and_current(monkeypatch) -> None:
    """DisposedException on the next open must name the last TEST end, not only the victim."""
    monkeypatch.setattr("plugin.testing_runner._soffice_pids", lambda: "4242")
    reset_lifecycle_breadcrumb()
    assert "previous=-" in format_lifecycle_breadcrumb()
    assert "current=-" in format_lifecycle_breadcrumb()

    record_test_start("draw.test_draw_uno.test_get_draw_tree")
    crumb = format_lifecycle_breadcrumb()
    assert "current=draw.test_draw_uno.test_get_draw_tree" in crumb
    assert "start_pids=4242" in crumb
    assert "now_pids=4242" in crumb

    record_test_end("draw.test_draw_uno.test_get_draw_tree", "OK")
    record_test_start("draw.test_draw_uno.test_insert_math_draw")
    crumb = format_lifecycle_breadcrumb()
    assert "previous=draw.test_draw_uno.test_get_draw_tree" in crumb
    assert "result=OK" in crumb
    assert "end_pids=4242" in crumb
    assert "last_ok=draw.test_draw_uno.test_get_draw_tree" in crumb
    assert "current=draw.test_draw_uno.test_insert_math_draw" in crumb
    assert "pids_changed=0" in crumb
    assert "dt_ms=" in crumb


def test_lifecycle_breadcrumb_marks_pid_change_and_last_ok(monkeypatch) -> None:
    pids = ["11"]

    def fake_pids() -> str:
        return pids[0]

    monkeypatch.setattr("plugin.testing_runner._soffice_pids", fake_pids)
    reset_lifecycle_breadcrumb()
    record_test_end("suite.test_ok", "OK")
    pids[0] = "-"
    record_test_start("suite.test_victim")
    crumb = format_lifecycle_breadcrumb()
    assert "last_ok=suite.test_ok" in crumb
    assert "pids_changed=1" in crumb
    assert "now_pids=-" in crumb


def test_probe_uno_bridge_states() -> None:
    assert probe_uno_bridge(None) == "no_probe"
    assert probe_uno_bridge(object()) == "no_probe"

    class _Alive:
        def getServiceManager(self) -> object:
            return object()

    assert probe_uno_bridge(_Alive()) == "alive"

    class _Dead:
        def getServiceManager(self) -> None:
            raise RuntimeError("Binary URP bridge disposed during call")

    assert probe_uno_bridge(_Dead()) == "disposed"

    class _Boom:
        def getServiceManager(self) -> None:
            raise ValueError("no desktop")

    assert probe_uno_bridge(_Boom()) == "error:ValueError"


def test_expand_soak_pair_aliases() -> None:
    assert expand_soak_pair("tree-math") == ["test_get_draw_tree", "test_insert_math_draw"]
    assert expand_soak_pair("dup-move") == [
        "test_duplicate_slide_copies_shapes",
        "test_duplicate_rename_move_slide",
    ]
    assert expand_soak_pair("unknown") == []


def test_function_name_matches_draw_tree_prefix_bleed() -> None:
    """FILTER=test_get_draw_tree also selects the blank/label test; --pair must not."""
    assert _function_name_matches(
        "test_get_draw_tree_marks_blank_and_label_hint",
        ["test_get_draw_tree"],
    ) is True
    assert "test_get_draw_tree_marks_blank_and_label_hint" not in expand_soak_pair("tree-math")


def test_note_office_stderr_line_sets_salabort_flag() -> None:
    reset_office_death_signals(clear_proc=True)
    assert consume_application_error() is False
    note_office_stderr_line("warn: something else")
    assert consume_application_error() is False
    note_office_stderr_line("  %s  " % _APPLICATION_ERROR_MARKER)
    assert consume_application_error() is True
    assert consume_application_error() is False
    reset_office_death_signals(clear_proc=True)


def test_collect_post_test_death_application_error() -> None:
    reset_lifecycle_breadcrumb()
    reset_office_death_signals(clear_proc=True)
    note_office_stderr_line(_APPLICATION_ERROR_MARKER)
    reason = collect_post_test_death(None)
    assert reason is not None
    assert "Unspecified Application Error" in reason
    assert "Binary URP bridge" in reason
    assert "VCL SalAbort" in reason
    reset_office_death_signals(clear_proc=True)


def test_collect_post_test_death_soffice_exit() -> None:
    import plugin.testing_runner as tr

    class _DeadProc:
        def poll(self) -> int:
            return 1

    reset_lifecycle_breadcrumb()
    reset_office_death_signals(clear_proc=True)
    tr._soffice_proc = _DeadProc()
    assert soffice_exit_code() == 1
    reason = collect_post_test_death(None)
    assert reason is not None
    assert "soffice exited 1" in reason
    assert "Binary URP bridge" in reason
    reset_office_death_signals(clear_proc=True)


def test_native_suite_sort_key_windows_puts_peer_last(monkeypatch) -> None:
    """GHA 34551644954: other suites must run before leftover peer docs."""
    import plugin.testing_runner as tr

    monkeypatch.setattr(tr.sys, "platform", "win32")
    paths = [
        "/tmp/tests/chatbot/test_peer_message_uno.py",
        "/tmp/tests/doc/test_text_helpers_uno.py",
        "/tmp/tests/chatbot/test_slash_popup_uno.py",
    ]
    ordered = sorted(paths, key=_native_suite_sort_key)
    assert ordered[-1].endswith("test_peer_message_uno.py")
    assert ordered[0].endswith("test_slash_popup_uno.py")


def test_native_suite_sort_key_windows_puts_draw_uno_before_peer(
    monkeypatch,
) -> None:
    """GHA 34616287301: leftover Math Draw hung notebook import-filter load."""
    import plugin.testing_runner as tr

    monkeypatch.setattr(tr.sys, "platform", "win32")
    paths = [
        "/tmp/tests/chatbot/test_peer_message_uno.py",
        "/tmp/tests/draw/test_draw_uno.py",
        "/tmp/tests/notebook/test_import_filter_uno.py",
        "/tmp/tests/draw/test_draw_forms_uno.py",
        "/tmp/tests/impress/test_impress_uno.py",
    ]
    ordered = sorted(paths, key=_native_suite_sort_key)
    names = [p.rsplit("/", 1)[-1] for p in ordered]
    assert names[-1] == "test_peer_message_uno.py"
    assert names[-2] == "test_draw_uno.py"
    assert names.index("test_import_filter_uno.py") < names.index("test_draw_uno.py")
    assert names.index("test_draw_forms_uno.py") < names.index("test_draw_uno.py")


def test_native_suite_sort_key_windows_defers_html_paste_after_doc_research(
    monkeypatch,
) -> None:
    """GHA 34648929578: leftover paste Writers poisoned Hidden Budget_read."""
    import plugin.testing_runner as tr

    monkeypatch.setattr(tr.sys, "platform", "win32")
    paths = [
        "/tmp/tests/calc/test_formulas_uno.py",
        "/tmp/tests/calc/test_rich_html_uno.py",
        "/tmp/tests/chatbot/test_slash_popup_uno.py",
        "/tmp/tests/doc/test_document_research_uno.py",
        "/tmp/tests/notebook/test_import_filter_uno.py",
        "/tmp/tests/draw/test_draw_uno.py",
        "/tmp/tests/chatbot/test_peer_message_uno.py",
    ]
    ordered = sorted(paths, key=_native_suite_sort_key)
    names = [p.rsplit("/", 1)[-1] for p in ordered]
    assert names.index("test_slash_popup_uno.py") < names.index("test_formulas_uno.py")
    assert names.index("test_document_research_uno.py") < names.index(
        "test_formulas_uno.py"
    )
    assert names.index("test_import_filter_uno.py") < names.index("test_rich_html_uno.py")
    assert names.index("test_formulas_uno.py") < names.index("test_draw_uno.py")
    assert names.index("test_rich_html_uno.py") < names.index("test_draw_uno.py")
    assert names[-1] == "test_peer_message_uno.py"


def test_native_suite_sort_key_windows_runs_designs_before_bridge(
    monkeypatch,
) -> None:
    """GHA 37406913485: designs must not inherit the bridge suite's recycle."""
    import plugin.testing_runner as tr

    monkeypatch.setattr(tr.sys, "platform", "win32")
    paths = [
        "/tmp/tests/draw/test_draw_forms_uno.py",
        "/tmp/tests/draw/test_bridge_uno.py",
        "/tmp/tests/doc/test_visual_helpers_uno.py",
        "/tmp/tests/draw/test_designs_uno.py",
        "/tmp/tests/calc/test_cells_uno.py",
        "/tmp/tests/draw/test_draw_uno.py",
    ]
    ordered = sorted(paths, key=_native_suite_sort_key)
    names = [p.rsplit("/", 1)[-1] for p in ordered]
    assert names == [
        "test_cells_uno.py",
        "test_visual_helpers_uno.py",
        "test_designs_uno.py",
        "test_bridge_uno.py",
        "test_draw_forms_uno.py",
        "test_draw_uno.py",
    ]


def test_native_suite_sort_key_posix_keeps_path_order(monkeypatch) -> None:
    """Linux PR CI must not defer draw_uno; POSIX still closes Math OLE."""
    import plugin.testing_runner as tr

    monkeypatch.setattr(tr.sys, "platform", "linux")
    paths = [
        "/tmp/tests/chatbot/test_peer_message_uno.py",
        "/tmp/tests/draw/test_draw_uno.py",
        "/tmp/tests/notebook/test_import_filter_uno.py",
        "/tmp/tests/draw/test_designs_uno.py",
        "/tmp/tests/draw/test_bridge_uno.py",
    ]
    assert sorted(paths, key=_native_suite_sort_key) == sorted(paths)


def test_office_recycle_request_is_consumed_once() -> None:
    """Windows Impress teardown asks the runner to recycle after the suite."""
    import plugin.testing_runner as tr

    tr._recycle_office_after_suite = False
    assert consume_office_recycle_request() is False
    request_office_recycle_after_suite()
    assert consume_office_recycle_request() is True
    assert consume_office_recycle_request() is False


def test_alias_runner_module_points_package_name_at_main(monkeypatch) -> None:
    fake_main = types.ModuleType("__main__")
    monkeypatch.setattr(tr, "__name__", "__main__")
    monkeypatch.setitem(sys.modules, "__main__", fake_main)
    monkeypatch.delitem(sys.modules, "plugin.testing_runner", raising=False)
    tr._alias_runner_module()
    assert sys.modules["plugin.testing_runner"] is fake_main


def test_alias_runner_module_is_noop_when_imported() -> None:
    tr._alias_runner_module()
    assert sys.modules["plugin.testing_runner"] is tr


def test_exit_after_summary_hard_exits_on_clean_pass(monkeypatch) -> None:
    """GHA 35527291175: pyuno GC SIGABRT after total_passed must not fail make."""
    import plugin.testing_runner as tr

    seen: list[int] = []

    def fake_exit(code: int) -> None:
        seen.append(code)
        raise SystemExit("hard-exit")

    monkeypatch.setattr(tr.os, "_exit", fake_exit)
    try:
        _exit_after_summary(0)
    except SystemExit as exc:
        assert exc.args == ("hard-exit",)
    else:
        raise AssertionError("expected os._exit on clean pass")
    assert seen == [0]


def test_exit_after_summary_keeps_systemexit_on_failure(monkeypatch) -> None:
    import plugin.testing_runner as tr

    def boom(code: int) -> None:
        raise AssertionError(f"os._exit must not hide failures, got {code}")

    monkeypatch.setattr(tr.os, "_exit", boom)
    try:
        _exit_after_summary(1)
    except SystemExit as exc:
        assert exc.code == 1
    else:
        raise AssertionError("expected SystemExit(1) on failure")


def test_exit_after_summary_minus_m_uses_helper() -> None:
    import plugin.testing_runner as tr

    text = Path(tr.__file__).read_text(encoding="utf-8")
    assert "if __name__ == \"__main__\":" in text
    assert "_exit_after_summary(main())" in text
    assert "raise SystemExit(main())" not in text


def test_native_suite_set_config_forwards_text_model_event_key(monkeypatch) -> None:
    """Sidebar setup calls set_text_model, which always passes event_key."""
    from plugin.framework.client.model_fetcher import set_text_model

    captured: list[tuple[str, str, str | None]] = []

    def original(key: str, value: object, *, event_key: str | None = None) -> None:
        captured.append((key, str(value), event_key))

    override: dict[str, object] = {}

    def wrapper(key: str, value: object, *, event_key: str | None = None) -> None:
        _native_suite_set_config(
            key,
            value,
            event_key=event_key,
            ctx=object(),
            review_mode_override=override,
            original_set_config=original,
        )

    monkeypatch.setattr("plugin.framework.client.model_fetcher.get_config", lambda key: "")
    monkeypatch.setattr("plugin.framework.client.model_fetcher.set_config", wrapper)

    set_text_model("mock-model", update_lru=False)
    assert captured == [("text_model", "mock-model", None)]

    set_text_model("mapped-model", update_lru=False, event_key="ai.text_model")
    assert captured[-1] == ("text_model", "mapped-model", "ai.text_model")


def test_native_suite_set_config_review_mode_keeps_mapped_event_key(monkeypatch) -> None:
    emitted: list[tuple[str, dict[str, object]]] = []

    def emit(event: str, **data: object) -> None:
        emitted.append((event, dict(data)))

    monkeypatch.setattr(get_event_bus(), "emit", emit)
    override: dict[str, object] = {}
    ctx = object()

    def original(key: str, value: object, *, event_key: str | None = None) -> None:
        raise AssertionError("review mode stays in memory")

    _native_suite_set_config(
        "doc.agent_edit_review_mode",
        "record",
        event_key=None,
        ctx=ctx,
        review_mode_override=override,
        original_set_config=original,
    )
    assert override["doc.agent_edit_review_mode"] == "record"
    assert emitted == [("config:changed", {"ctx": ctx})]

    _native_suite_set_config(
        "doc.agent_edit_review_mode",
        "wait",
        event_key="ai.review",
        ctx=ctx,
        review_mode_override=override,
        original_set_config=original,
    )
    assert override["doc.agent_edit_review_mode"] == "wait"
    assert emitted[-1] == ("config:changed", {"key": "ai.review", "value": "wait", "ctx": ctx})


def test_fail_reason_with_lifecycle_keeps_crumb_after_cap() -> None:
    reset_lifecycle_breadcrumb()
    record_test_end("suite.test_ok", "OK")
    record_test_start("suite.test_victim")
    long_exc = AssertionError("n" * 500)
    out = _fail_reason_with_lifecycle(long_exc)
    assert _fail_reason(long_exc).endswith("...")
    assert "previous=suite.test_ok" in out
    assert "result=OK" in out
    assert "current=suite.test_victim" in out


def test_uno_module_import_error_fails_the_run(monkeypatch, tmp_path, capsys) -> None:
    """A *_uno.py that fails to import must set total_failed so main() exits 1.

    The loader used to print and continue, so the process exited 0.
    """
    plugin_dir = tmp_path / "plugin"
    tests_dir = tmp_path / "tests"
    plugin_dir.mkdir()
    tests_dir.mkdir()
    (tests_dir / "test_r48_import_fail_uno.py").write_text(
        "from os import no_such_writeragent_pkg_r48a\n",
        encoding="utf-8",
    )
    (tests_dir / "test_r48_syntax_uno.py").write_text("def (\n", encoding="utf-8")
    (tests_dir / "test_r48_skip_native_uno.py").write_text(
        "SKIP_NATIVE_RUN_ALL = True\n",
        encoding="utf-8",
    )

    monkeypatch.setattr(tr, "use_user_profile", False)
    monkeypatch.setattr(tr, "_parse_cli_args", lambda _argv: [])
    monkeypatch.setattr(tr, "_bootstrap_office", lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("unit test")))
    monkeypatch.setattr("plugin.framework.constants.get_plugin_dir", lambda: str(plugin_dir))
    monkeypatch.setattr("plugin.framework.uno_context.set_fallback_ctx", lambda _ctx: None)
    monkeypatch.setattr("plugin.framework.config.init_config", lambda _ctx=None: "")
    monkeypatch.setattr("plugin.main.bootstrap", lambda ctx=None: None)
    monkeypatch.setattr("plugin.framework.uno_context.get_desktop", lambda ctx=None: None)

    saved = (config_mod.get_config, config_mod.set_config, config_mod.get_config_dict)
    leaked = (
        "plugin.tests.test_r48_import_fail_uno",
        "plugin.tests.test_r48_syntax_uno",
        "plugin.tests.test_r48_skip_native_uno",
    )
    try:
        summary = json.loads(tr.run_all_tests(object()))
    finally:
        config_mod.get_config, config_mod.set_config, config_mod.get_config_dict = saved
        for key in leaked:
            sys.modules.pop(key, None)

    assert int(summary.get("total_failed", 0) or 0) == 2
    names = {entry["name"] for entry in summary["suites"]}
    assert "test_r48_import_fail_uno" in names
    assert "test_r48_syntax_uno" in names
    assert "test_r48_skip_native_uno" not in names
    by_name = {entry["name"]: entry for entry in summary["suites"]}
    assert by_name["test_r48_import_fail_uno"]["failed"] == 1
    assert any("ImportError" in line for line in by_name["test_r48_import_fail_uno"]["log"])
    assert by_name["test_r48_syntax_uno"]["failed"] == 1
    assert any("SyntaxError" in line for line in by_name["test_r48_syntax_uno"]["log"])
    out = capsys.readouterr().out
    assert "Skipping" not in out



def test_mark_urp_dead_prints_office_log_tail(monkeypatch, tmp_path):
    cfg = tmp_path / "user" / "config"
    cfg.mkdir(parents=True)
    (cfg / "writeragent_debug.log").write_text("\n".join("line %d" % i for i in range(50)) + "\n", encoding="utf-8")
    seen: list[str] = []
    monkeypatch.setattr(tr, "_throwaway_profile_dir", tmp_path)
    monkeypatch.setattr(tr, "_progress", seen.append)
    monkeypatch.setattr(tr, "_urp_bridge_dead", False)
    tr._mark_urp_dead(RuntimeError("gone"), "t")
    tail = [s for s in seen if s.startswith("OFFICE LOG | ")]
    assert tail[0] == "OFFICE LOG | line 10"
    assert tail[-1] == "OFFICE LOG | line 49"
    assert len(tail) == 40


def test_clean_test_does_not_probe_pids(monkeypatch) -> None:
    calls = []

    def counting_pids() -> str:
        calls.append(1)
        return "1"

    monkeypatch.setattr(tr, "_soffice_pids", counting_pids)
    reset_office_death_signals(clear_proc=True)
    reset_lifecycle_breadcrumb()
    record_test_start("s.a")
    assert len(calls) == 1
    record_test_end("s.a", "OK")
    assert len(calls) == 1

    class FakeCtx:
        def getServiceManager(self):
            return object()

    assert collect_post_test_death(FakeCtx()) is None
    assert len(calls) == 1
    record_test_start("s.b")
    assert len(calls) == 1


def test_dead_office_still_probes_pids(monkeypatch) -> None:
    calls = []

    def counting_pids() -> str:
        calls.append(1)
        return "1"

    monkeypatch.setattr(tr, "_soffice_pids", counting_pids)

    class DeadProc:
        def poll(self):
            return 1

    monkeypatch.setattr(tr, "_soffice_proc", DeadProc())
    reason = collect_post_test_death(None)
    assert reason is not None
    assert len(calls) > 0


def test_soffice_pids_probe_passes_timeout(monkeypatch) -> None:
    recorded_calls: list[dict[str, Any]] = []

    def fake_check_output(*args: Any, **kwargs: Any) -> str:
        recorded_calls.append(kwargs)
        return ""

    monkeypatch.setattr(tr.subprocess, "check_output", fake_check_output)
    monkeypatch.setattr(tr.sys, "platform", "linux")
    assert tr._soffice_pids() == "-"
    assert len(recorded_calls) == 2
    assert all(kw.get("timeout") == 2 for kw in recorded_calls)

    recorded_calls.clear()
    monkeypatch.setattr(tr.sys, "platform", "win32")
    assert tr._soffice_pids() == "-"
    assert len(recorded_calls) == 1
    assert recorded_calls[0].get("timeout") == 2


def test_typo_filter_fails_the_run(monkeypatch, tmp_path) -> None:
    plugin_dir = tmp_path / "plugin"
    tests_dir = tmp_path / "tests"
    plugin_dir.mkdir()
    tests_dir.mkdir()
    (tests_dir / "test_ok_uno.py").write_text(
        "def test_unrelated(ctx):\n    pass\n",
        encoding="utf-8",
    )

    monkeypatch.setattr(tr, "use_user_profile", False)
    monkeypatch.setattr(tr, "_parse_cli_args", lambda _argv: ["no_such_test_zzz"])
    monkeypatch.setattr(tr, "_soak_repeat", 1)
    monkeypatch.setattr(tr, "_bootstrap_office", lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("unit test")))
    monkeypatch.setattr("plugin.framework.constants.get_plugin_dir", lambda: str(plugin_dir))
    monkeypatch.setattr("plugin.framework.uno_context.set_fallback_ctx", lambda _ctx: None)
    monkeypatch.setattr("plugin.framework.config.init_config", lambda _ctx=None: "")
    monkeypatch.setattr("plugin.main.bootstrap", lambda ctx=None: None)
    monkeypatch.setattr("plugin.framework.uno_context.get_desktop", lambda ctx=None: None)

    summary = json.loads(tr.run_all_tests(object()))
    assert summary.get("total_failed") == 1
    discovery_suites = [s for s in summary["suites"] if s["name"] == "discovery"]
    assert len(discovery_suites) == 1
    assert discovery_suites[0]["failed"] == 1
    assert any("No native test suites matched filters" in line for line in discovery_suites[0]["log"])


def test_missing_tests_tree_fails_the_run(monkeypatch, tmp_path) -> None:
    plugin_dir = tmp_path / "plugin"
    plugin_dir.mkdir()

    monkeypatch.setattr(tr, "use_user_profile", False)
    monkeypatch.setattr(tr, "_parse_cli_args", lambda _argv: [])
    monkeypatch.setattr(tr, "_bootstrap_office", lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("unit test")))
    monkeypatch.setattr("plugin.framework.constants.get_plugin_dir", lambda: str(plugin_dir))
    monkeypatch.setattr("plugin.framework.uno_context.set_fallback_ctx", lambda _ctx: None)
    monkeypatch.setattr("plugin.framework.config.init_config", lambda _ctx=None: "")
    monkeypatch.setattr("plugin.main.bootstrap", lambda ctx=None: None)
    monkeypatch.setattr("plugin.framework.uno_context.get_desktop", lambda ctx=None: None)

    summary = json.loads(tr.run_all_tests(object()))
    assert summary.get("total_failed") == 1
    discovery_suites = [s for s in summary["suites"] if s["name"] == "discovery"]
    assert len(discovery_suites) == 1
    assert discovery_suites[0]["failed"] == 1
    assert any("Tests directory not found" in line for line in discovery_suites[0]["log"])


def test_parse_cli_args_visible_and_filters(monkeypatch) -> None:
    monkeypatch.delenv("WRITERAGENT_UNO_USER_PROFILE", raising=False)
    monkeypatch.delenv("WRITERAGENT_UNO_SOAK", raising=False)
    monkeypatch.setattr(tr, "show_window", False)
    assert tr._parse_cli_args(["--visible", "tests/x/test_cells_uno.py", "E"]) == [
        "tests/x/test_cells_uno.py",
        "E",
    ]
    assert tr.show_window is True


def test_parse_cli_args_repeat_forms() -> None:
    assert tr._parse_cli_args(["--repeat", "3"]) == []
    assert tr._soak_repeat == 3
    assert tr._parse_cli_args(["--repeat=3"]) == []
    assert tr._soak_repeat == 3


def test_parse_cli_args_rejects_bad_repeat() -> None:
    with pytest.raises(SystemExit):
        tr._parse_cli_args(["--repeat", "abc"])


def test_parse_cli_args_pair_expands(monkeypatch) -> None:
    # _parse_cli_args sets this module global; restore it so later run_module_suite tests on the same xdist worker are not filtered.
    monkeypatch.setattr(tr, "_cli_exact_function_names", [])
    assert tr._parse_cli_args(["--pair", "tree-math"]) == [
        "test_get_draw_tree",
        "test_insert_math_draw",
    ]
    assert tr._cli_exact_function_names == [
        "test_get_draw_tree",
        "test_insert_math_draw",
    ]


def test_parse_cli_args_user_profile_sets_env(monkeypatch) -> None:
    monkeypatch.delenv("WRITERAGENT_UNO_USER_PROFILE", raising=False)
    monkeypatch.setattr(tr, "use_user_profile", False)
    tr._parse_cli_args(["--user-profile"])
    assert tr.use_user_profile is True
    assert os.environ["WRITERAGENT_UNO_USER_PROFILE"] == "1"
