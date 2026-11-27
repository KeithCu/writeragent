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
    _exit_after_summary,
    _function_name_matches,
    _is_case_id,
    _module_matches_filters,
    _native_suite_save,
    _native_suite_sort_key,
    _test_function_filters,
    collect_post_test_death,
    current_office,
    expand_soak_pair,
    probe_uno_bridge,
    record_test_end,
    record_test_start,
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
    assert _module_matches_filters(full, ["test_cells_uno"]) is True
    assert _module_matches_filters(full, ["test_read_range_format_info_performance"]) is True
    assert _module_matches_filters(full, ["test_unrelated_other"]) is False


def test_module_matches_filters_by_packet_letter(tmp_path: Path) -> None:
    path = tmp_path / "test_mock_llm_sidebar_uno.py"
    path.write_text(
        "def test_f1_crash(ctx):\n    return\n\ndef test_e7_outline(ctx):\n    return\n",
        encoding="utf-8",
    )
    full = str(path)
    assert _module_matches_filters(full, ["E"]) is True
    assert _module_matches_filters(full, ["B"]) is False
    assert _module_matches_filters(full, ["f1"]) is True
    assert _module_matches_filters(full, ["f10"]) is False


def test_cli_filters_default_empty() -> None:
    assert isinstance(current_office().options.filters, list)
    assert current_office().options.filters == []


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


def test_function_name_matches_draw_tree_prefix_bleed() -> None:
    """FILTER=test_get_draw_tree also selects the blank/label test; --pair must not."""
    assert _function_name_matches(
        "test_get_draw_tree_marks_blank_and_label_hint",
        ["test_get_draw_tree"],
    ) is True
    assert "test_get_draw_tree_marks_blank_and_label_hint" not in expand_soak_pair("tree-math")


def test_note_stderr_line_sets_salabort_flag() -> None:
    office = current_office()
    office.reset_death_signals(clear_proc=True)
    assert office.consume_application_error() is False
    office.note_stderr_line("warn: something else")
    assert office.consume_application_error() is False
    office.note_stderr_line("  %s  " % _APPLICATION_ERROR_MARKER)
    assert office.consume_application_error() is True
    assert office.consume_application_error() is False
    office.reset_death_signals(clear_proc=True)


def test_collect_post_test_death_application_error() -> None:
    office = current_office()
    office.reset_lifecycle_breadcrumb()
    office.reset_death_signals(clear_proc=True)
    office.note_stderr_line(_APPLICATION_ERROR_MARKER)
    reason = collect_post_test_death(None)
    assert reason is not None
    office.reset_death_signals(clear_proc=True)


def test_collect_post_test_death_soffice_exit() -> None:
    class _DeadProc:
        def poll(self) -> int:
            return 1

    office = current_office()
    office.reset_lifecycle_breadcrumb()
    office.reset_death_signals(clear_proc=True)
    tr._office.soffice_proc = _DeadProc()
    assert office.soffice_exit_code() == 1
    reason = collect_post_test_death(None)
    assert reason is not None
    office.reset_death_signals(clear_proc=True)


@pytest.mark.parametrize(
    ("paths", "expected_before_pairs"),
    [
        # GHA 34551644954: other suites must run before leftover peer docs.
        pytest.param(
            [
                "/tmp/tests/chatbot/test_peer_message_uno.py",
                "/tmp/tests/doc/test_text_helpers_uno.py",
                "/tmp/tests/chatbot/test_slash_popup_uno.py",
            ],
            [
                ("test_slash_popup_uno.py", "test_text_helpers_uno.py"),
                ("test_text_helpers_uno.py", "test_peer_message_uno.py"),
            ],
            id="puts_peer_last",
        ),
        # GHA 34616287301: leftover Math Draw hung notebook import-filter load.
        pytest.param(
            [
                "/tmp/tests/chatbot/test_peer_message_uno.py",
                "/tmp/tests/draw/test_draw_uno.py",
                "/tmp/tests/notebook/test_import_filter_uno.py",
                "/tmp/tests/draw/test_draw_forms_uno.py",
                "/tmp/tests/impress/test_impress_uno.py",
            ],
            [
                ("test_import_filter_uno.py", "test_draw_uno.py"),
                ("test_draw_forms_uno.py", "test_draw_uno.py"),
                ("test_impress_uno.py", "test_draw_uno.py"),
                ("test_draw_uno.py", "test_peer_message_uno.py"),
            ],
            id="puts_draw_uno_before_peer",
        ),
        # GHA 34648929578: leftover paste Writers poisoned Hidden Budget_read.
        pytest.param(
            [
                "/tmp/tests/calc/test_formulas_uno.py",
                "/tmp/tests/calc/test_rich_html_uno.py",
                "/tmp/tests/chatbot/test_slash_popup_uno.py",
                "/tmp/tests/doc/test_document_research_uno.py",
                "/tmp/tests/notebook/test_import_filter_uno.py",
                "/tmp/tests/draw/test_draw_uno.py",
                "/tmp/tests/chatbot/test_peer_message_uno.py",
            ],
            [
                ("test_slash_popup_uno.py", "test_formulas_uno.py"),
                ("test_document_research_uno.py", "test_formulas_uno.py"),
                ("test_import_filter_uno.py", "test_rich_html_uno.py"),
                ("test_formulas_uno.py", "test_draw_uno.py"),
                ("test_rich_html_uno.py", "test_draw_uno.py"),
                ("test_draw_uno.py", "test_peer_message_uno.py"),
            ],
            id="defers_html_paste_after_doc_research",
        ),
        # GHA 37406913485: designs must not inherit the bridge suite's recycle.
        pytest.param(
            [
                "/tmp/tests/draw/test_draw_forms_uno.py",
                "/tmp/tests/draw/test_bridge_uno.py",
                "/tmp/tests/doc/test_visual_helpers_uno.py",
                "/tmp/tests/draw/test_designs_uno.py",
                "/tmp/tests/calc/test_cells_uno.py",
                "/tmp/tests/draw/test_draw_uno.py",
            ],
            [
                ("test_cells_uno.py", "test_visual_helpers_uno.py"),
                ("test_visual_helpers_uno.py", "test_designs_uno.py"),
                ("test_designs_uno.py", "test_bridge_uno.py"),
                ("test_bridge_uno.py", "test_draw_forms_uno.py"),
                ("test_draw_forms_uno.py", "test_draw_uno.py"),
            ],
            id="runs_designs_before_bridge",
        ),
    ],
)
def test_native_suite_sort_key_windows_ordering(
    monkeypatch,
    paths: list[str],
    expected_before_pairs: list[tuple[str, str]],
) -> None:
    """Windows native suite ordering isolates leftover docs and recycle boundaries."""

    monkeypatch.setattr(tr.sys, "platform", "win32")
    names = [p.rsplit("/", 1)[-1] for p in sorted(paths, key=_native_suite_sort_key)]
    for earlier, later in expected_before_pairs:
        assert names.index(earlier) < names.index(later)


def test_native_suite_sort_key_posix_keeps_path_order(monkeypatch) -> None:
    """Linux PR CI must not defer draw_uno; POSIX still closes Math OLE."""

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
    office = current_office()
    office.recycle_after_suite = False
    assert office.consume_recycle_request() is False
    office.request_recycle()
    assert office.consume_recycle_request() is True
    assert office.consume_recycle_request() is False


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
    def boom(code: int) -> None:
        raise AssertionError(f"os._exit must not hide failures, got {code}")

    monkeypatch.setattr(tr.os, "_exit", boom)
    try:
        _exit_after_summary(1)
    except SystemExit as exc:
        assert exc.code == 1
    else:
        raise AssertionError("expected SystemExit(1) on failure")


def test_native_suite_save_forwards_text_model_event_key(monkeypatch) -> None:
    """Sidebar setup calls set_text_model, which always passes event_key."""
    from plugin.framework.client.model_fetcher import set_text_model

    captured: list[tuple[dict[str, object], str | None]] = []

    def original(patch: dict[str, object], *, event_key: str | None = None) -> None:
        captured.append((patch, event_key))

    override: dict[str, object] = {}

    def wrapper(patch: dict[str, object], *, event_key: str | None = None) -> None:
        _native_suite_save(
            patch,
            event_key=event_key,
            ctx=object(),
            review_mode_override=override,
            original_save=original,
        )

    monkeypatch.setattr("plugin.framework.client.model_fetcher.get_config", lambda key: "")
    monkeypatch.setattr("plugin.framework.client.model_fetcher.save", wrapper)

    set_text_model("mock-model", update_lru=False)
    assert captured == [({"text_model": "mock-model"}, None)]

    set_text_model("mapped-model", update_lru=False, event_key="ai.text_model")
    assert captured[-1] == ({"text_model": "mapped-model"}, "ai.text_model")


def test_native_suite_save_review_mode_keeps_mapped_event_key(monkeypatch) -> None:
    emitted: list[tuple[str, dict[str, object]]] = []

    def emit(event: str, **data: object) -> None:
        emitted.append((event, dict(data)))

    monkeypatch.setattr(get_event_bus(), "emit", emit)
    override: dict[str, object] = {}
    ctx = object()

    def original(patch: dict[str, object], *, event_key: str | None = None) -> None:
        raise AssertionError("review mode stays in memory")

    _native_suite_save(
        {"doc.agent_edit_review_mode": "record"},
        event_key=None,
        ctx=ctx,
        review_mode_override=override,
        original_save=original,
    )
    assert override["doc.agent_edit_review_mode"] == "record"
    assert emitted == [("config:changed", {"ctx": ctx})]

    _native_suite_save(
        {"doc.agent_edit_review_mode": "wait"},
        event_key="ai.review",
        ctx=ctx,
        review_mode_override=override,
        original_save=original,
    )
    assert override["doc.agent_edit_review_mode"] == "wait"
    assert emitted[-1] == ("config:changed", {"key": "ai.review", "value": "wait", "ctx": ctx})


def test_uno_module_import_error_fails_the_run(monkeypatch, tmp_path) -> None:
    """A *_uno.py that fails to import must set total_failed so main() exits 1."""
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

    monkeypatch.setattr(tr.current_office().options, "use_user_profile", False)
    monkeypatch.setattr(tr.current_office().options, "filters", [])
    monkeypatch.setattr("plugin.framework.constants.get_plugin_dir", lambda: str(plugin_dir))
    monkeypatch.setattr("plugin.framework.uno_context.set_fallback_ctx", lambda _ctx: None)
    monkeypatch.setattr("plugin.framework.config.init_config", lambda _ctx=None: "")
    monkeypatch.setattr("plugin.app.bootstrap", lambda ctx=None: None)
    monkeypatch.setattr("plugin.framework.uno_context.get_desktop", lambda ctx=None: None)

    saved = (config_mod.get_config, config_mod.save, config_mod.get_config_dict)
    leaked = (
        "plugin.tests.test_r48_import_fail_uno",
        "plugin.tests.test_r48_syntax_uno",
        "plugin.tests.test_r48_skip_native_uno",
    )
    try:
        ctx = types.SimpleNamespace(getServiceManager=lambda: object())
        summary = json.loads(tr.run_all_tests(ctx))
    finally:
        config_mod.get_config, config_mod.save, config_mod.get_config_dict = saved
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


def test_clean_test_does_not_probe_pids(monkeypatch) -> None:
    calls = []

    def counting_pids() -> str:
        calls.append(1)
        return "1"

    monkeypatch.setattr(tr, "_soffice_pids", counting_pids)
    current_office().reset_death_signals(clear_proc=True)
    current_office().reset_lifecycle_breadcrumb()
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

    monkeypatch.setattr(tr._office, "soffice_proc", DeadProc())
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

    monkeypatch.setattr(tr.current_office().options, "use_user_profile", False)
    monkeypatch.setattr(tr.current_office().options, "filters", ["no_such_test_zzz"])
    monkeypatch.setattr(tr.current_office().options, "soak_repeat", 1)
    monkeypatch.setattr("plugin.framework.constants.get_plugin_dir", lambda: str(plugin_dir))
    monkeypatch.setattr("plugin.framework.uno_context.set_fallback_ctx", lambda _ctx: None)
    monkeypatch.setattr("plugin.framework.config.init_config", lambda _ctx=None: "")
    monkeypatch.setattr("plugin.app.bootstrap", lambda ctx=None: None)
    monkeypatch.setattr("plugin.framework.uno_context.get_desktop", lambda ctx=None: None)

    summary = json.loads(tr.run_all_tests(object()))
    assert summary.get("total_failed") == 1
    discovery_suites = [s for s in summary["suites"] if s["name"] == "discovery"]
    assert len(discovery_suites) == 1
    assert discovery_suites[0]["failed"] == 1


def test_missing_tests_tree_fails_the_run(monkeypatch, tmp_path) -> None:
    plugin_dir = tmp_path / "plugin"
    plugin_dir.mkdir()

    monkeypatch.setattr(tr.current_office().options, "use_user_profile", False)
    monkeypatch.setattr("plugin.framework.constants.get_plugin_dir", lambda: str(plugin_dir))
    monkeypatch.setattr("plugin.framework.uno_context.set_fallback_ctx", lambda _ctx: None)
    monkeypatch.setattr("plugin.framework.config.init_config", lambda _ctx=None: "")
    monkeypatch.setattr("plugin.app.bootstrap", lambda ctx=None: None)
    monkeypatch.setattr("plugin.framework.uno_context.get_desktop", lambda ctx=None: None)

    summary = json.loads(tr.run_all_tests(object()))
    assert summary.get("total_failed") == 1
    discovery_suites = [s for s in summary["suites"] if s["name"] == "discovery"]
    assert len(discovery_suites) == 1
    assert discovery_suites[0]["failed"] == 1


def test_uno_shards_cover_every_suite_file() -> None:
    """Each discovered *_uno.py lands in exactly one shard, and no shard is empty."""
    repo_root = Path(__file__).resolve().parents[2]
    paths = sorted(p.relative_to(repo_root).as_posix() for p in (repo_root / "tests").rglob("test_*_uno.py"))
    assigned = tr._uno_shards_for(paths)
    assert set(assigned) == set(paths)
    assert set(assigned.values()) == set(range(1, tr._UNO_SHARD_COUNT + 1))


def test_uno_shard_durations_pack_nearly_even() -> None:
    """Recorded durations should pack into nearly equal shards; refresh the file when they drift."""
    durations = tr._load_uno_shard_durations()
    assigned = tr._uno_shards_for(list(durations))
    totals = [0.0] * tr._UNO_SHARD_COUNT
    for path, seconds in durations.items():
        totals[assigned[path] - 1] += seconds
    assert max(totals) - min(totals) <= 1.0


def test_new_suite_file_does_not_move_recorded_files() -> None:
    """A new suite path joins a shard without changing where any recorded file runs."""
    durations = tr._load_uno_shard_durations()
    new_paths = ["tests/writer/test_zz_new_suite_uno.py", "tests/calc/test_aa_new_suite_uno.py"]
    assert not set(new_paths) & set(durations)
    before = tr._uno_shards_for(list(durations))
    after = tr._uno_shards_for([*durations, *new_paths])
    assert {path: after[path] for path in durations} == before
    assert all(after[path] in range(1, tr._UNO_SHARD_COUNT + 1) for path in new_paths)


def test_deleted_suite_file_does_not_move_the_rest() -> None:
    durations = tr._load_uno_shard_durations()
    paths = list(durations)
    full = tr._uno_shards_for(paths)
    without_one = tr._uno_shards_for(paths[1:])
    assert {path: full[path] for path in paths[1:]} == without_one


def test_pr_ci_shard_matrix_runs_every_shard() -> None:
    """The CI matrix must list every runner shard, or the files in a missing shard never run."""
    yaml = pytest.importorskip("yaml")
    workflow = Path(__file__).resolve().parents[2] / ".github" / "workflows" / "pr-ci.yml"
    jobs = yaml.safe_load(workflow.read_text(encoding="utf-8"))["jobs"]
    assert jobs["test-uno-shard"]["strategy"]["matrix"]["shard"] == list(range(1, tr._UNO_SHARD_COUNT + 1))
    assert "test-uno" not in jobs


def test_shard_option_limits_discovery_to_its_files(monkeypatch, tmp_path) -> None:
    plugin_dir = tmp_path / "plugin"
    plugin_dir.mkdir()
    rels = ("tests/calc/test_shard_one_uno.py", "tests/writer/test_zz_shard_three_uno.py")
    for rel in rels:
        target = tmp_path / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("from os import no_such_writeragent_pkg_shard\n", encoding="utf-8")

    assigned = tr._uno_shards_for(list(rels))
    chosen = rels[-1]
    monkeypatch.setattr(tr.current_office().options, "use_user_profile", False)
    monkeypatch.setattr(tr.current_office().options, "filters", [])
    monkeypatch.setattr(tr.current_office().options, "soak_repeat", 1)
    monkeypatch.setattr(tr.current_office().options, "shard", assigned[chosen])
    monkeypatch.setattr("plugin.framework.constants.get_plugin_dir", lambda: str(plugin_dir))
    monkeypatch.setattr("plugin.framework.uno_context.set_fallback_ctx", lambda _ctx: None)
    monkeypatch.setattr("plugin.framework.config.init_config", lambda _ctx=None: "")
    monkeypatch.setattr("plugin.app.bootstrap", lambda ctx=None: None)
    monkeypatch.setattr("plugin.framework.uno_context.get_desktop", lambda ctx=None: None)

    saved = (config_mod.get_config, config_mod.save, config_mod.get_config_dict)
    try:
        ctx = types.SimpleNamespace(getServiceManager=lambda: object())
        summary = json.loads(tr.run_all_tests(ctx))
    finally:
        config_mod.get_config, config_mod.save, config_mod.get_config_dict = saved
        sys.modules.pop("plugin.tests.calc.test_shard_one_uno", None)
        sys.modules.pop("plugin.tests.writer.test_zz_shard_three_uno", None)

    assert {entry["name"] for entry in summary["suites"]} == {"writer.test_zz_shard_three_uno"}


def test_parse_cli_args_shard(monkeypatch) -> None:
    monkeypatch.setattr(tr.current_office().options, "shard", 0)
    assert tr._parse_cli_args(["--shard", "2", "test_cells_uno"]) == ["test_cells_uno"]
    assert tr.current_office().options.shard == 2
    with pytest.raises(SystemExit):
        tr._parse_cli_args(["--shard", str(tr._UNO_SHARD_COUNT + 1)])


def test_parse_cli_args_visible_and_filters(monkeypatch) -> None:
    monkeypatch.delenv("WRITERAGENT_UNO_USER_PROFILE", raising=False)
    monkeypatch.delenv("WRITERAGENT_UNO_SOAK", raising=False)
    monkeypatch.setattr(tr.current_office().options, "show_window", False)
    assert tr._parse_cli_args(["--visible", "tests/x/test_cells_uno.py", "E"]) == [
        "tests/x/test_cells_uno.py",
        "E",
    ]
    assert tr.current_office().options.show_window is True


def test_parse_cli_args_repeat_forms() -> None:
    assert tr._parse_cli_args(["--repeat", "3"]) == []
    assert tr.current_office().options.soak_repeat == 3
    assert tr._parse_cli_args(["--repeat=3"]) == []
    assert tr.current_office().options.soak_repeat == 3


def test_parse_cli_args_rejects_bad_repeat() -> None:
    with pytest.raises(SystemExit):
        tr._parse_cli_args(["--repeat", "abc"])


def test_parse_cli_args_pair_expands(monkeypatch) -> None:
    # _parse_cli_args sets office options; restore it so later run_module_suite tests on the same xdist worker are not filtered.
    monkeypatch.setattr(tr.current_office().options, "exact_function_names", [])
    assert tr._parse_cli_args(["--pair", "tree-math"]) == [
        "test_get_draw_tree",
        "test_insert_math_draw",
    ]
    assert tr.current_office().options.exact_function_names == [
        "test_get_draw_tree",
        "test_insert_math_draw",
    ]


def test_parse_cli_args_user_profile_sets_env(monkeypatch) -> None:
    monkeypatch.delenv("WRITERAGENT_UNO_USER_PROFILE", raising=False)
    monkeypatch.setattr(tr.current_office().options, "use_user_profile", False)
    tr._parse_cli_args(["--user-profile"])
    assert tr.current_office().options.use_user_profile is True
    assert os.environ["WRITERAGENT_UNO_USER_PROFILE"] == "1"


@pytest.mark.parametrize(
    ("suite_name", "expected_modules_attr"),
    [
        ("calc.tests", "CALC_MENU_UNIT_MODULES"),
        ("calc.integration_tests", "CALC_MENU_INTEGRATION_MODULES"),
    ],
)
def test_calc_menu_suite_names_dispatch_to_calc_fallbacks(monkeypatch, suite_name, expected_modules_attr) -> None:
    """Regression (#2584): main.RunCalcTests passes "calc.tests"; the generic
    rule derives "run_tests_tests", which would discover zero tests and report
    a false-green 0 passed / 0 failed."""
    import tests.calc.test_calc_uno as calc_menu

    seen: list[tuple[str, ...]] = []

    def fake_aggregated(_ctx: Any, module_names: tuple[str, ...]) -> tuple[int, int, list[str]]:
        seen.append(module_names)
        return len(module_names), 0, []

    monkeypatch.setattr(calc_menu, "_run_calc_menu_aggregated", fake_aggregated)
    passed, failed, _log = tr.run_module_suite(object(), calc_menu, suite_name)

    expected = getattr(calc_menu, expected_modules_attr)
    assert seen == [expected]
    assert passed == len(expected) > 0
    assert failed == 0


def test_rebootstrap_follows_the_new_office_profile(monkeypatch, tmp_path: Path) -> None:
    import plugin.app as app_mod
    import plugin.framework.queue_executor as qe_mod
    import plugin.framework.uno_context as uno_ctx_mod

    def _fake_ctx(user_config: Path) -> Any:
        user_config.mkdir(parents=True, exist_ok=True)
        path_settings = types.SimpleNamespace(UserConfig=str(user_config))
        sm = types.SimpleNamespace(createInstanceWithContext=lambda _name, _ctx: path_settings)
        return types.SimpleNamespace(getServiceManager=lambda: sm)

    ctx_a = _fake_ctx(tmp_path / "profile_a" / "user" / "config")
    ctx_b = _fake_ctx(tmp_path / "profile_b" / "user" / "config")

    monkeypatch.setattr(uno_ctx_mod, "set_fallback_ctx", lambda _ctx: None)
    monkeypatch.setattr(qe_mod.main, "set_context", lambda _ctx: None)
    monkeypatch.setattr(tr, "_ensure_writeragent_bind", lambda: None)
    monkeypatch.setattr(app_mod, "bootstrap", lambda ctx=None: None)
    monkeypatch.setattr(tr.current_office().options, "use_user_profile", False)
    config_mod.reset_config_for_tests()

    tr._rebootstrap_ctx(ctx_a)
    config_mod.save({"scripting.python_session_mode": "shared"})
    assert config_mod.get_config("scripting.python_session_mode") == "shared"

    tr._rebootstrap_ctx(ctx_b)
    assert config_mod.get_config("scripting.python_session_mode") == "isolated"

    config_mod.save({"scripting.python_session_mode": "shared"})
    cfg_b = tmp_path / "profile_b" / "user" / "config" / "writeragent.json"
    assert cfg_b.is_file()
    assert config_mod.parse_config_json_text(cfg_b.read_text(encoding="utf-8")) == {
        "scripting.python_session_mode": "shared"
    }


def _run_two_uno_suites(monkeypatch, tmp_path, suite_a_body: str, ctx: Any) -> dict[str, Any]:
    plugin_dir = tmp_path / "plugin"
    tests_dir = tmp_path / "tests"
    plugin_dir.mkdir()
    tests_dir.mkdir()
    (tests_dir / "test_a_uno.py").write_text(suite_a_body, encoding="utf-8")
    (tests_dir / "test_b_uno.py").write_text(
        "from plugin.testing_runner import native_test\n"
        "@native_test\n"
        "def test_never_runs(ctx):\n"
        "    pass\n",
        encoding="utf-8",
    )

    office = tr.current_office()
    office.reset_death_signals(clear_proc=True)
    office.consume_recycle_request()
    monkeypatch.setattr(office, "urp_bridge_dead", False)
    monkeypatch.setattr(office.options, "use_user_profile", False)
    monkeypatch.setattr(office.options, "filters", [])
    monkeypatch.setattr(office.options, "soak_repeat", 1)
    monkeypatch.setattr("plugin.framework.constants.get_plugin_dir", lambda: str(plugin_dir))
    monkeypatch.setattr(tr, "_rebootstrap_ctx", lambda _ctx: None)
    monkeypatch.setattr(tr, "_open_keeper", lambda _ctx: None)

    saved = (config_mod.get_config, config_mod.save, config_mod.get_config_dict)
    try:
        return json.loads(tr.run_all_tests(ctx))
    finally:
        config_mod.get_config, config_mod.save, config_mod.get_config_dict = saved
        office.reset_death_signals(clear_proc=True)
        office.consume_recycle_request()
        sys.modules.pop("plugin.tests.test_a_uno", None)
        sys.modules.pop("plugin.tests.test_b_uno", None)


@pytest.mark.parametrize(
    "exc",
    [
        RuntimeError("Binary URP bridge already disposed"),
        RuntimeError("null component context service manager"),
    ],
)
def test_dead_office_between_suites_fails_the_run(monkeypatch, tmp_path, exc: Exception) -> None:
    """An office that dies between suites stops the run and marks it failed."""
    bootstrap_calls: list[int] = []
    monkeypatch.setattr(tr, "_bootstrap_office", lambda *_a, **_k: bootstrap_calls.append(1))

    class _Ctx:
        dead = False

        def getServiceManager(self):
            if self.dead:
                raise exc
            return object()

    suite_a = (
        "from plugin.testing_runner import native_test, teardown\n"
        "@native_test\n"
        "def test_ok(ctx):\n"
        "    pass\n"
        "@teardown\n"
        "def _td(ctx):\n"
        "    ctx.dead = True\n"
    )
    summary = _run_two_uno_suites(monkeypatch, tmp_path, suite_a, _Ctx())

    assert summary.get("total_passed") == 1
    assert summary.get("total_failed") == 1
    names = [entry["name"] for entry in summary["suites"]]
    assert "test_a_uno" in names
    assert "test_b_uno" not in names
    assert "office" in names
    assert bootstrap_calls == []


def test_failed_recycle_fails_the_run(monkeypatch, tmp_path) -> None:
    """A failed office recycle between suites must fail the run."""
    monkeypatch.setattr(tr, "_terminate_bootstrap_soffice", lambda: None)
    monkeypatch.setattr(
        tr,
        "_bootstrap_office",
        lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("no office")),
    )

    suite_a = (
        "from plugin.testing_runner import current_office, native_test\n"
        "@native_test\n"
        "def test_ok(ctx):\n"
        "    current_office().request_recycle()\n"
    )
    ctx = types.SimpleNamespace(getServiceManager=lambda: object())
    summary = _run_two_uno_suites(monkeypatch, tmp_path, suite_a, ctx)

    assert summary.get("total_passed") == 1
    assert summary.get("total_failed") == 1
    names = [entry["name"] for entry in summary["suites"]]
    assert "test_a_uno" in names
    assert "test_b_uno" not in names
    assert "office" in names
