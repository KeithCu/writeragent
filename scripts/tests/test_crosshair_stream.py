# WriterAgent tests — crosshair_stream formatter
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

from io import StringIO
from pathlib import Path

from scripts.crosshair_stream import (
    StreamStats,
    classify_line,
    discover_deal_plugin_files,
    print_banner,
    print_error_summary,
    stream_lines,
)


def test_classify_check_confirmed() -> None:
    line = "/path/payload_codec.py:274: info: Confirmed over all paths."
    got = classify_line(line, "check")
    assert got is not None
    assert got.tag == "CHECK CONFIRMED"


def test_classify_check_error() -> None:
    line = "/home/keithcu/project/plugin/scripting/payload_codec.py:483: error: IndexError when calling host_unpack_split_grid(...)"
    got = classify_line(line, "check")
    assert got is not None
    assert got.tag == "CHECK ERROR"
    assert "plugin/scripting/payload_codec.py:483" in got.detail
    assert "IndexError" in got.detail


def test_classify_verbose_analyzing_function() -> None:
    line = "23222.229|    |analyze_function() Analyzing  host_pack_split_grid"
    got = classify_line(line, "check")
    assert got is not None
    assert got.tag == "CHECK PROGRESS"
    assert "host_pack_split_grid" in got.detail


def test_classify_verbose_choose_possible_suppressed() -> None:
    line = "23222.290|                  |choose_possible() SMT chose: Not(0 < grid_2_len_4)"
    assert classify_line(line, "check") is None


def test_classify_cover_example() -> None:
    got = classify_line("host_pack_split_grid([])", "cover")
    assert got is not None
    assert got.tag == "COVER EXAMPLE"


def test_stream_lines_check_summary() -> None:
    lines = [
        "plugin/scripting/payload_codec.py:274: info: Not confirmed.\n",
        "plugin/scripting/payload_codec.py:684: info: Confirmed over all paths.\n",
    ]
    buf = StringIO()
    stats = stream_lines(iter(lines), mode="check", out=buf, raw=False, quiet=False)
    assert stats.not_confirmed == 1
    assert stats.confirmed == 1
    out = buf.getvalue()
    assert "CHECK NOT_CONFIRMED" in out
    assert "CHECK CONFIRMED" in out
    assert "confirmed=1" in out


def test_stream_lines_verbose_milestone() -> None:
    lines = [
        "23222.229|    |analyze_function() Analyzing  host_pack_split_grid\n",
        "23222.251|    |analyze() Analyzing postcondition: \" isinstance(result, dict) \"\n",
    ]
    buf = StringIO()
    stats = stream_lines(iter(lines), mode="check", out=buf, raw=False, quiet=False)
    assert stats.progress == 2
    out = buf.getvalue()
    assert "CHECK PROGRESS" in out
    assert "choose_possible" not in out


def test_classify_crosshair_internal_as_error() -> None:
    line = "crosshair.util.CrossHairInternal: Numeric operation on symbolic while not tracing"
    got = classify_line(line, "check")
    assert got is not None
    assert got.tag == "CHECK ERROR"


def test_classify_traceback_as_error() -> None:
    got = classify_line("Traceback (most recent call last):", "check")
    assert got is not None
    assert got.tag == "CHECK ERROR"


def test_classify_plugin_file_frame_suppressed_in_check() -> None:
    """CrosshairUnsupported -v dumps File frames without a Traceback header; not check fatals."""
    line = (
        'File "/home/keithcu/Desktop/Python/writeragent/plugin/framework/tool.py", '
        "line 72, in _make_optional_scalar_nullable"
    )
    assert classify_line(line, "check") is None


def test_classify_check_still_fails_on_traceback_and_error() -> None:
    tb = classify_line("Traceback (most recent call last):", "check")
    assert tb is not None
    assert tb.tag == "CHECK ERROR"
    err = classify_line(
        "/home/keithcu/project/plugin/scripting/payload_codec.py:483: error: "
        "IndexError when calling host_unpack_split_grid(...)",
        "check",
    )
    assert err is not None
    assert err.tag == "CHECK ERROR"
    assert "payload_codec.py:483" in err.detail


def test_classify_cover_suppresses_exploration_stack_frames() -> None:
    """Cover -v dumps File/TypeError noise while exiting 0; must not fail the sweep."""
    file_line = (
        'File "/home/keithcu/Desktop/Python/writeragent/plugin/scripting/payload_codec.py", '
        "line 574, in wire_cell_count"
    )
    assert classify_line(file_line, "cover") is None
    assert classify_line("TypeError: __repr__ returned non-string (type LazyIntSymbolicStr)", "cover") is None


def test_classify_cover_crosshair_internal_still_fatal() -> None:
    line = "crosshair.util.CrossHairInternal: Numeric operation on symbolic while not tracing"
    got = classify_line(line, "cover")
    assert got is not None
    assert got.tag == "COVER FATAL"


def test_classify_cover_traceback_is_explore_not_fatal() -> None:
    """log.exception during cover path exploration must not fail the sweep."""
    got = classify_line("Traceback (most recent call last):", "cover")
    assert got is not None
    assert got.tag == "COVER EXPLORE"
    assert "exploration" in got.detail

    lines = [
        "payload_codec child_unpack split_grid failed for envelope dict(keys=[])\n",
        "Traceback (most recent call last):\n",
        '  File "plugin/scripting/payload_codec.py", line 1209, in child_unpack_split_grid\n',
        "ValueError: Missing payload binary buffer or b64 representation\n",
    ]
    buf = StringIO()
    stats = stream_lines(iter(lines), mode="cover", out=buf, raw=False, quiet=False)
    assert stats.cover_errors == 0
    assert stats.failure_count == 0
    assert stats.explore >= 2
    assert "COVER FATAL" not in buf.getvalue()


def test_error_summary_lists_unique_details() -> None:
    lines = [
        "plugin/scripting/payload_codec.py:500: error: TypeError when calling should_use_binary_envelope()\n",
        "plugin/scripting/payload_codec.py:500: error: TypeError when calling should_use_binary_envelope()\n",
        "crosshair.util.CrossHairInternal: boom\n",
    ]
    buf = StringIO()
    stats = stream_lines(iter(lines), mode="check", out=buf, raw=False, quiet=True)
    assert stats.check_errors >= 2
    summary = StringIO()
    print_error_summary(stats, summary)
    text = summary.getvalue()
    assert "=== ERRORS TO FIX ===" in text
    assert "payload_codec.py:500" in text
    assert "CrossHairInternal" in text


def test_print_banner_without_label_unchanged() -> None:
    stats = StreamStats(lines=10, suppressed=8, examples=2)
    out = StringIO()
    print_banner(stats, "cover", 0, out)
    text = out.getvalue()
    assert "=== CrossHair COVER DONE (exit 0) ===" in text
    assert "lines read: 10" in text
    assert "examples=2" in text
    # No module identity line between title and lines read
    title_idx = text.index("=== CrossHair COVER DONE")
    lines_idx = text.index("lines read:")
    between = text[title_idx:lines_idx]
    assert "[/" not in between


def test_print_banner_with_label_includes_module_line() -> None:
    stats = StreamStats(lines=100, suppressed=90, examples=5)
    out = StringIO()
    print_banner(stats, "cover", 0, out, label="[3/21] plugin/writer/word_diff_split.py")
    text = out.getvalue()
    assert "=== CrossHair COVER DONE (exit 0) ===" in text
    assert "  [3/21] plugin/writer/word_diff_split.py\n" in text
    assert text.index("[3/21] plugin/writer/word_diff_split.py") < text.index("lines read:")


def test_cover_module_section_markers_match_top_and_bottom() -> None:
    """Simulated cover-all block: opening and closing ######## lines match after renumber."""
    from scripts.crosshair_cover_all import (
        PROGRESS_SENTINEL,
        CoverModuleResult,
        emit_cover_module_result,
    )

    section_raw = f"######## {PROGRESS_SENTINEL} plugin/writer/word_diff_split.py ########"
    formatted = (
        f"\n{section_raw}\n"
        "[COVER EXAMPLE         ] is_surgical(SplitResult(0, 0, 0))\n"
        "=== CrossHair COVER DONE (exit 0) ===\n"
        f"  {PROGRESS_SENTINEL} plugin/writer/word_diff_split.py\n"
        "  lines read: 10 (suppressed 8)\n"
        "  examples=1 explore=0 errors=0\n"
        f"{section_raw}\n"
    )
    out = StringIO()
    emit_cover_module_result(
        out,
        CoverModuleResult(
            rel="plugin/writer/word_diff_split.py",
            index=8,  # discovery index must not appear in output
            total=21,
            exit_code=0,
            examples=1,
            explore=0,
            error_details=(),
            formatted=formatted,
            duration_sec=1.5,
        ),
        completed=3,
    )
    text = out.getvalue()
    section = "######## [3/21] plugin/writer/word_diff_split.py ########"
    first = text.index(section)
    last = text.rindex(section)
    assert first < last
    assert text.count(section) == 2
    assert "  [3/21] plugin/writer/word_diff_split.py\n" in text
    assert PROGRESS_SENTINEL not in text
    assert "[8/21]" not in text


def test_emit_renumbers_by_completion_not_discovery_index() -> None:
    """First finished module is [1/N] even if discovery index was 8."""
    from scripts.crosshair_cover_all import (
        PROGRESS_SENTINEL,
        CoverModuleResult,
        emit_cover_module_result,
    )

    out = StringIO()
    late = CoverModuleResult(
        rel="plugin/late.py",
        index=8,
        total=21,
        exit_code=0,
        examples=0,
        explore=0,
        error_details=(),
        formatted=f"######## {PROGRESS_SENTINEL} plugin/late.py ########\nDONE\n",
        duration_sec=10.0,
    )
    early = CoverModuleResult(
        rel="plugin/early.py",
        index=4,
        total=21,
        exit_code=0,
        examples=0,
        explore=0,
        error_details=(),
        formatted=f"######## {PROGRESS_SENTINEL} plugin/early.py ########\nDONE\n",
        duration_sec=1.0,
    )
    emit_cover_module_result(out, late, completed=1)
    emit_cover_module_result(out, early, completed=2)
    text = out.getvalue()
    assert "[1/21] plugin/late.py" in text
    assert "[2/21] plugin/early.py" in text
    assert "[8/21]" not in text
    assert "[4/21]" not in text
    assert text.index("[1/21]") < text.index("[2/21]")


def test_discover_deal_plugin_files_includes_payload_codec(tmp_path) -> None:
    plugin = tmp_path / "plugin"
    (plugin / "scripting").mkdir(parents=True)
    target = plugin / "scripting" / "payload_codec.py"
    target.write_text("import deal\n@deal.post(lambda result, *_, **__: True)\ndef f():\n    return 1\n", encoding="utf-8")
    (plugin / "scripting" / "no_deal.py").write_text("def g():\n    return 2\n", encoding="utf-8")
    found = discover_deal_plugin_files(plugin)
    assert found == [target]


def test_filter_check_all_targets_skip_list() -> None:
    from scripts.crosshair_check_all import CROSSHAIR_CHECK_ALL_SKIP, filter_check_all_targets

    paths = [Path(p) for p in sorted(CROSSHAIR_CHECK_ALL_SKIP)] + [Path("plugin/scripting/payload_codec.py")]
    to_run, skipped = filter_check_all_targets(paths, apply_skip=True)
    assert [p.as_posix() for p in to_run] == ["plugin/scripting/payload_codec.py"]
    assert set(skipped) == set(CROSSHAIR_CHECK_ALL_SKIP)
    all_run, none_skipped = filter_check_all_targets(paths, apply_skip=False)
    assert all_run == paths
    assert none_skipped == []


def test_cover_all_list_discovers_deal_without_spawning(tmp_path, capsys) -> None:
    """cover-all --list finds @deal. modules and exits 0 without spawning CrossHair."""
    from scripts.crosshair_cover_all import main as cover_all_main

    plugin = tmp_path / "plugin"
    (plugin / "scripting").mkdir(parents=True)
    target = plugin / "scripting" / "payload_codec.py"
    target.write_text(
        "import deal\n@deal.post(lambda result, *_, **__: True)\ndef f():\n    return 1\n",
        encoding="utf-8",
    )
    (plugin / "scripting" / "no_deal.py").write_text("def g():\n    return 2\n", encoding="utf-8")

    code = cover_all_main(["--list", "--plugin-root", str(plugin)])
    assert code == 0
    out = capsys.readouterr().out
    assert "CrossHair cover-all [regular]: 1 module(s)" in out
    assert "worker(s)" in out
    assert "process pool" in out
    assert "payload_codec.py" in out
    assert "no_deal.py" not in out


def test_default_cover_jobs_leaves_two_cores(monkeypatch) -> None:
    from scripts.crosshair_cover_all import default_cover_jobs

    monkeypatch.setattr("scripts.crosshair_cover_all.os.cpu_count", lambda: 8)
    assert default_cover_jobs() == 6
    monkeypatch.setattr("scripts.crosshair_cover_all.os.cpu_count", lambda: 3)
    assert default_cover_jobs() == 2
    monkeypatch.setattr("scripts.crosshair_cover_all.os.cpu_count", lambda: 1)
    assert default_cover_jobs() == 2
    monkeypatch.setattr("scripts.crosshair_cover_all.os.cpu_count", lambda: None)
    assert default_cover_jobs() == 2


def test_emit_cover_module_result_writes_full_blocks_without_interleave() -> None:
    """Parent emits whole module buffers; second block cannot start mid-first."""
    from scripts.crosshair_cover_all import (
        PROGRESS_SENTINEL,
        CoverModuleResult,
        emit_cover_module_result,
    )

    out = StringIO()
    first = CoverModuleResult(
        rel="a.py",
        index=1,
        total=2,
        exit_code=0,
        examples=1,
        explore=0,
        error_details=(),
        formatted=f"######## {PROGRESS_SENTINEL} a.py ########\n[COVER EXAMPLE] foo()\nDONE_A\n",
        duration_sec=2.0,
    )
    second = CoverModuleResult(
        rel="b.py",
        index=2,
        total=2,
        exit_code=0,
        examples=0,
        explore=1,
        error_details=(),
        formatted=f"######## {PROGRESS_SENTINEL} b.py ########\n[COVER EXPLORE] bar\nDONE_B\n",
        duration_sec=3.0,
    )
    emit_cover_module_result(out, first, completed=1)
    emit_cover_module_result(out, second, completed=2)
    text = out.getvalue()
    assert text.index("DONE_A") < text.index("######## [2/2] b.py")
    assert text.index("DONE_B") > text.index("######## [2/2] b.py")
    assert "DONE_A\n######## [2/2] b.py" in text
    assert "[1/2] a.py" in text


def test_cover_all_reuses_check_all_skip_list() -> None:
    from scripts.crosshair_check_all import CROSSHAIR_CHECK_ALL_SKIP
    from scripts.crosshair_cover_all import CROSSHAIR_CHECK_ALL_SKIP as cover_imported_check_skip

    assert cover_imported_check_skip is CROSSHAIR_CHECK_ALL_SKIP


def test_filter_cover_all_targets_unions_skips() -> None:
    from scripts.crosshair_check_all import CROSSHAIR_CHECK_ALL_SKIP
    from scripts.crosshair_cover_all import CROSSHAIR_COVER_ALL_SKIP, filter_cover_all_targets

    check_skip = Path(sorted(CROSSHAIR_CHECK_ALL_SKIP)[0])
    cover_skip = Path(sorted(CROSSHAIR_COVER_ALL_SKIP)[0])
    keep = Path("plugin/scripting/payload_codec.py")
    to_run, skipped = filter_cover_all_targets([check_skip, cover_skip, keep], apply_skip=True)
    assert to_run == [keep]
    assert set(skipped) == {check_skip.as_posix(), cover_skip.as_posix()}


def test_cover_all_skips_check_green_crash_frame_hosts() -> None:
    """Modules that pass check but crash-frame under cover stay in COVER_ALL_SKIP."""
    from scripts.crosshair_cover_all import CROSSHAIR_COVER_ALL_SKIP, filter_cover_all_targets

    crash_hosts = (
        "plugin/framework/client/auth.py",
        "plugin/framework/config.py",
        "plugin/framework/config_service.py",
        "plugin/framework/default_models.py",
        "plugin/framework/event_bus.py",
        "plugin/framework/i18n.py",
        "plugin/framework/tool.py",
        "plugin/framework/url_utils.py",
        "plugin/mcp/cors.py",
        "plugin/scripting/calc_range.py",
        "plugin/scripting/duckdb_sql.py",
        "plugin/scripting/editor_ipc.py",
        "plugin/scripting/helper_domain.py",
        "plugin/scripting/sandbox.py",
    )
    assert set(crash_hosts) <= CROSSHAIR_COVER_ALL_SKIP
    paths = [Path(p) for p in crash_hosts] + [Path("plugin/scripting/payload_codec.py")]
    to_run, skipped = filter_cover_all_targets(paths, apply_skip=True)
    assert to_run == [Path("plugin/scripting/payload_codec.py")]
    assert set(crash_hosts) <= set(skipped)


def test_resolve_cover_budget_regular_and_deep() -> None:
    from scripts.crosshair_cover_all import (
        DEEP_MAX_UNINTERESTING,
        REGULAR_MAX_UNINTERESTING,
        REGULAR_PER_CONDITION_TIMEOUT_SEC,
        resolve_cover_budget,
    )

    regular = resolve_cover_budget(deep=False)
    assert regular.mode == "regular"
    assert regular.max_uninteresting == REGULAR_MAX_UNINTERESTING == 50
    assert regular.per_condition_timeout == REGULAR_PER_CONDITION_TIMEOUT_SEC == 30

    deep = resolve_cover_budget(deep=True)
    assert deep.mode == "deep"
    assert deep.max_uninteresting == DEEP_MAX_UNINTERESTING == 200
    assert deep.per_condition_timeout is None


def test_build_timings_payload_sorts_longest_first() -> None:
    from scripts.crosshair_cover_all import CoverModuleResult, build_timings_payload

    slow = CoverModuleResult(
        rel="plugin/slow.py",
        index=1,
        total=2,
        exit_code=0,
        examples=1,
        explore=0,
        error_details=(),
        formatted="",
        duration_sec=100.0,
    )
    fast = CoverModuleResult(
        rel="plugin/fast.py",
        index=2,
        total=2,
        exit_code=0,
        examples=0,
        explore=0,
        error_details=(),
        formatted="",
        duration_sec=1.0,
    )
    payload = build_timings_payload(
        mode="regular",
        jobs=4,
        wall_sec=101.5,
        max_uninteresting=50,
        per_condition_timeout=30,
        results=[fast, slow],
    )
    assert payload["mode"] == "regular"
    modules = payload["modules"]
    assert isinstance(modules, list)
    assert modules[0]["rel"] == "plugin/slow.py"
    assert modules[1]["rel"] == "plugin/fast.py"
    assert modules[0]["duration_sec"] == 100.0


def test_order_cover_targets_longest_first() -> None:
    from scripts.crosshair_cover_all import order_cover_targets

    short = Path("plugin/chatbot/research_cache_fluff.py")
    mid = Path("plugin/mcp/mcp_state.py")
    long = Path("plugin/scripting/payload_codec.py")
    unknown = Path("plugin/zzz/new_deal_module.py")
    ordered = order_cover_targets([short, unknown, mid, long])
    assert [p.as_posix() for p in ordered] == [
        "plugin/scripting/payload_codec.py",
        "plugin/mcp/mcp_state.py",
        "plugin/chatbot/research_cache_fluff.py",
        "plugin/zzz/new_deal_module.py",
    ]


def test_cover_all_list_uses_schedule_order(tmp_path, capsys) -> None:
    """--list prints longest-first even when discovery is alphabetical."""
    from scripts.crosshair_cover_all import main as cover_all_main

    deal_src = "import deal\n@deal.post(lambda result, *_, **__: True)\ndef f():\n    return 1\n"
    plugin = tmp_path / "plugin"
    for rel in (
        "chatbot/research_cache_fluff.py",
        "mcp/mcp_state.py",
        "scripting/payload_codec.py",
    ):
        path = plugin / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(deal_src, encoding="utf-8")

    code = cover_all_main(["--list", "--plugin-root", str(plugin)])
    assert code == 0
    out = capsys.readouterr().out
    assert out.index("payload_codec.py") < out.index("mcp_state.py")
    assert out.index("mcp_state.py") < out.index("research_cache_fluff.py")


def test_module_cover_bounds_tightens_payload_codec_regular_only() -> None:
    from scripts.crosshair_cover_all import (
        PAYLOAD_CODEC_REL,
        module_cover_bounds,
        resolve_cover_budget,
    )

    regular = resolve_cover_budget(deep=False)
    deep = resolve_cover_budget(deep=True)
    assert module_cover_bounds(regular, PAYLOAD_CODEC_REL) == (5, 5)
    assert module_cover_bounds(regular, "plugin/mcp/mcp_state.py") == (50, 30)
    assert module_cover_bounds(deep, PAYLOAD_CODEC_REL) == (200, None)
