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
    format_check_bracket,
    format_prev_mmss,
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


def test_cover_all_list_discovers_deal_without_spawning(tmp_path, capsys, monkeypatch) -> None:
    """cover-all --list finds @deal. modules and exits 0 without spawning CrossHair."""
    import os

    from plugin.framework.deal_shim import CROSSHAIR_ENV
    from scripts.crosshair_cover_all import main as cover_all_main

    monkeypatch.delenv(CROSSHAIR_ENV, raising=False)

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
    assert os.environ.get(CROSSHAIR_ENV) == "1"
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


def test_filter_cover_all_targets_empty_skips_pass_through() -> None:
    """Module skip lists stay empty; hostility is per-callable ``# crosshair: off``."""
    from scripts.crosshair_check_all import CROSSHAIR_CHECK_ALL_SKIP
    from scripts.crosshair_cover_all import CROSSHAIR_COVER_ALL_SKIP, filter_cover_all_targets

    assert CROSSHAIR_CHECK_ALL_SKIP == frozenset()
    assert CROSSHAIR_COVER_ALL_SKIP == frozenset()
    keep = Path("plugin/scripting/payload_codec.py")
    to_run, skipped = filter_cover_all_targets([keep], apply_skip=True)
    assert to_run == [keep]
    assert skipped == []


def test_cover_fqns_skip_crosshair_off_callables(tmp_path: Path) -> None:
    from scripts.crosshair_stream import cover_fqns_for_module, plugin_path_to_module_fqn

    mod = tmp_path / "plugin" / "demo_off.py"
    mod.parent.mkdir(parents=True)
    mod.write_text(
        "def keep_me(x):\n"
        "    return x\n"
        "\n"
        "def hostile(x):\n"
        "    # crosshair: off\n"
        "    return x\n"
        "\n"
        "class Host:\n"
        "    def run(self):\n"
        "        # crosshair: off\n"
        "        return 1\n"
        "\n"
        "    def ok(self):\n"
        "        return 2\n",
        encoding="utf-8",
    )
    # plugin_path_to_module_fqn needs a plugin/ prefix in the path string.
    # tmp_path/plugin/demo_off.py → plugin.demo_off when we use as_posix with /plugin/.
    fqns = cover_fqns_for_module(mod)
    assert plugin_path_to_module_fqn(mod).endswith("demo_off")
    assert any(f.endswith(".keep_me") for f in fqns)
    assert any(f.endswith(".Host.ok") for f in fqns)
    assert not any(f.endswith(".hostile") for f in fqns)
    assert not any(f.endswith(".Host.run") for f in fqns)



def test_cover_fqns_skips_deal_predicate_helpers(tmp_path: Path) -> None:
    from scripts.crosshair_stream import cover_fqns_for_module

    mod = tmp_path / "plugin" / "deal_helpers.py"
    mod.parent.mkdir(parents=True)
    mod.write_text(
        "def _deal_foo_ok_pytest(x):\n"
        "    return True\n"
        "\n"
        "def _deal_foo_ok_crosshair(x):\n"
        "    return True\n"
        "\n"
        "def real_product(x):\n"
        "    return x\n",
        encoding="utf-8",
    )
    fqns = cover_fqns_for_module(mod)
    assert any(f.endswith(".real_product") for f in fqns)
    assert not any("._deal_" in f for f in fqns)


def test_cover_fqns_require_deal_drops_bare_helpers(tmp_path: Path) -> None:
    from scripts.crosshair_stream import cover_fqns_for_module

    mod = tmp_path / "plugin" / "mixed_deal.py"
    mod.parent.mkdir(parents=True)
    mod.write_text(
        "import deal\n"
        "def bare(x):\n"
        "    return x\n"
        "\n"
        "@deal.post(lambda result: True)\n"
        "def contracted(x):\n"
        "    return x\n",
        encoding="utf-8",
    )
    all_fqns = cover_fqns_for_module(mod)
    deal_fqns = cover_fqns_for_module(mod, require_deal=True)
    assert any(f.endswith(".bare") for f in all_fqns)
    assert any(f.endswith(".contracted") for f in all_fqns)
    assert not any(f.endswith(".bare") for f in deal_fqns)
    assert any(f.endswith(".contracted") for f in deal_fqns)


def test_cover_fqns_all_off_returns_empty(tmp_path: Path) -> None:
    from scripts.crosshair_stream import cover_fqns_for_module

    mod = tmp_path / "plugin" / "all_off.py"
    mod.parent.mkdir(parents=True)
    mod.write_text(
        "def only():\n"
        "    # crosshair: off\n"
        "    return 0\n",
        encoding="utf-8",
    )
    assert cover_fqns_for_module(mod) == []


def test_cover_fqns_module_level_off_returns_empty(tmp_path: Path) -> None:
    from scripts.crosshair_stream import cover_fqns_for_module, module_has_crosshair_off

    mod = tmp_path / "plugin" / "mod_off.py"
    mod.parent.mkdir(parents=True)
    mod.write_text(
        '"""Host module."""\n'
        "# crosshair: off\n"
        "\n"
        "def keep_me(x):\n"
        "    return x\n"
        "\n"
        "class Host:\n"
        "    def ok(self):\n"
        "        return 1\n",
        encoding="utf-8",
    )
    assert module_has_crosshair_off(mod.read_text(encoding="utf-8"))
    assert cover_fqns_for_module(mod) == []


def test_resolve_cover_budget_regular_and_deep() -> None:
    from scripts.crosshair_cover_all import (
        DEEP_MAX_UNINTERESTING,
        REGULAR_MAX_UNINTERESTING,
        REGULAR_PER_CONDITION_TIMEOUT_SEC,
        resolve_cover_budget,
    )

    regular = resolve_cover_budget(deep=False)
    assert regular.mode == "regular"
    assert regular.max_uninteresting == REGULAR_MAX_UNINTERESTING == 25
    assert regular.per_condition_timeout == REGULAR_PER_CONDITION_TIMEOUT_SEC == 5

    deep = resolve_cover_budget(deep=True)
    assert deep.mode == "deep"
    assert deep.max_uninteresting == DEEP_MAX_UNINTERESTING == 200
    assert deep.per_condition_timeout is None


def test_resolve_check_budget_regular_and_deep() -> None:
    from scripts.crosshair_check_all import (
        DEEP_MAX_UNINTERESTING,
        REGULAR_MAX_UNINTERESTING,
        REGULAR_PER_CONDITION_TIMEOUT_SEC,
        resolve_check_budget,
    )

    regular = resolve_check_budget(deep=False)
    assert regular.mode == "regular"
    assert regular.max_uninteresting == REGULAR_MAX_UNINTERESTING == 25
    assert regular.per_condition_timeout == REGULAR_PER_CONDITION_TIMEOUT_SEC == 5

    deep = resolve_check_budget(deep=True)
    assert deep.mode == "deep"
    assert deep.max_uninteresting == DEEP_MAX_UNINTERESTING == 200
    assert deep.per_condition_timeout is None


def test_module_check_bounds_tightens_payload_codec_regular_only() -> None:
    from scripts.crosshair_check_all import (
        PAYLOAD_CODEC_REL,
        module_check_bounds,
        resolve_check_budget,
    )

    regular = resolve_check_budget(deep=False)
    deep = resolve_check_budget(deep=True)
    assert module_check_bounds(regular, PAYLOAD_CODEC_REL) == (5, 5)
    assert module_check_bounds(regular, "plugin/mcp/mcp_state.py") == (25, 5)
    assert module_check_bounds(deep, PAYLOAD_CODEC_REL) == (200, None)


def test_check_all_list_discovers_deal_without_spawning(tmp_path, capsys, monkeypatch) -> None:
    """check-all --list finds @deal. modules and exits 0 without spawning CrossHair."""
    import os

    from plugin.framework.deal_shim import CROSSHAIR_ENV
    from scripts.crosshair_check_all import main as check_all_main

    monkeypatch.delenv(CROSSHAIR_ENV, raising=False)

    plugin = tmp_path / "plugin"
    (plugin / "scripting").mkdir(parents=True)
    target = plugin / "scripting" / "payload_codec.py"
    target.write_text(
        "import deal\n@deal.post(lambda result, *_, **__: True)\ndef f():\n    return 1\n",
        encoding="utf-8",
    )
    (plugin / "scripting" / "no_deal.py").write_text("def g():\n    return 2\n", encoding="utf-8")

    code = check_all_main(["--list", "--plugin-root", str(plugin)])
    assert code == 0
    assert os.environ.get(CROSSHAIR_ENV) == "1"
    out = capsys.readouterr().out
    assert "CrossHair check-all [regular]: 1 module(s)" in out
    assert "one CrossHair process per FQN" in out
    assert "max_uninteresting=25" in out
    assert "module_wall=120s" in out
    assert "payload_codec.py" in out
    assert "no_deal.py" not in out


def test_check_all_list_deep_banner(tmp_path, capsys, monkeypatch) -> None:
    import os

    from plugin.framework.deal_shim import CROSSHAIR_ENV
    from scripts.crosshair_check_all import main as check_all_main

    monkeypatch.delenv(CROSSHAIR_ENV, raising=False)

    plugin = tmp_path / "plugin"
    (plugin / "scripting").mkdir(parents=True)
    (plugin / "scripting" / "payload_codec.py").write_text(
        "import deal\n@deal.post(lambda result, *_, **__: True)\ndef f():\n    return 1\n",
        encoding="utf-8",
    )

    code = check_all_main(["--deep", "--list", "--plugin-root", str(plugin)])
    assert code == 0
    assert os.environ.get(CROSSHAIR_ENV) == "1"
    out = capsys.readouterr().out
    assert "CrossHair check-all [deep]: 1 module(s)" in out
    assert "max_uninteresting=200" in out
    assert "per_condition_timeout=none" in out
    assert "module_wall=none" in out


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


def test_order_cover_targets_unknowns_first_then_longest_known() -> None:
    from scripts.crosshair_cover_all import order_cover_targets

    short = Path("plugin/chatbot/research_cache_fluff.py")
    mid = Path("plugin/scripting/payload_codec.py")
    long = Path("plugin/mcp/mcp_state.py")
    unknown = Path("plugin/zzz/new_deal_module.py")
    ordered = order_cover_targets([short, unknown, mid, long])
    assert [p.as_posix() for p in ordered] == [
        "plugin/zzz/new_deal_module.py",
        "plugin/mcp/mcp_state.py",
        "plugin/scripting/payload_codec.py",
        "plugin/chatbot/research_cache_fluff.py",
    ]


def test_order_cover_targets_unknowns_alpha_among_themselves() -> None:
    from scripts.crosshair_cover_all import order_cover_targets

    known = Path("plugin/scripting/payload_codec.py")
    a = Path("plugin/aaa/new_a.py")
    z = Path("plugin/zzz/new_z.py")
    ordered = order_cover_targets([known, z, a])
    assert [p.as_posix() for p in ordered] == [
        "plugin/aaa/new_a.py",
        "plugin/zzz/new_z.py",
        "plugin/scripting/payload_codec.py",
    ]


def test_cover_all_list_uses_schedule_order(tmp_path, capsys, monkeypatch) -> None:
    """--list prints longest-first even when discovery is alphabetical."""
    import os

    from plugin.framework.deal_shim import CROSSHAIR_ENV
    from scripts.crosshair_cover_all import main as cover_all_main

    monkeypatch.delenv(CROSSHAIR_ENV, raising=False)

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
    assert os.environ.get(CROSSHAIR_ENV) == "1"
    out = capsys.readouterr().out
    # Schedule ranks mcp_state before payload_codec (longer measured run).
    assert out.index("mcp_state.py") < out.index("payload_codec.py")
    assert out.index("payload_codec.py") < out.index("research_cache_fluff.py")


def test_cover_all_schedule_order_starts_with_slowest_measured() -> None:
    from scripts.crosshair_cover_all import COVER_ALL_SCHEDULE_ORDER

    assert COVER_ALL_SCHEDULE_ORDER[0] == "plugin/calc/python/formula_edit.py"
    assert COVER_ALL_SCHEDULE_ORDER[1] == "plugin/mcp/mcp_state.py"
    assert COVER_ALL_SCHEDULE_ORDER[2] == "plugin/framework/client/stream_normalizer.py"
    assert "plugin/scripting/payload_codec.py" in COVER_ALL_SCHEDULE_ORDER
    # Longest-first: earlier index must be listed before later index in the tuple.
    assert COVER_ALL_SCHEDULE_ORDER.index("plugin/mcp/mcp_state.py") < COVER_ALL_SCHEDULE_ORDER.index(
        "plugin/scripting/payload_codec.py"
    )


def test_module_cover_bounds_tightens_payload_codec_regular_only() -> None:
    from scripts.crosshair_cover_all import (
        PAYLOAD_CODEC_REL,
        module_cover_bounds,
        resolve_cover_budget,
    )

    regular = resolve_cover_budget(deep=False)
    deep = resolve_cover_budget(deep=True)
    assert module_cover_bounds(regular, PAYLOAD_CODEC_REL) == (5, 5)
    assert module_cover_bounds(regular, "plugin/mcp/mcp_state.py") == (25, 5)
    assert module_cover_bounds(deep, PAYLOAD_CODEC_REL) == (200, None)


def test_regular_module_wall_timeout_constant() -> None:
    from scripts.crosshair_check_all import (
        REGULAR_MODULE_WALL_TIMEOUT_SEC as CHECK_WALL,
    )
    from scripts.crosshair_cover_all import (
        REGULAR_MODULE_WALL_TIMEOUT_SEC as COVER_WALL,
    )

    assert COVER_WALL == CHECK_WALL == 120


def test_run_crosshair_timeout_kills_and_exits_zero(monkeypatch) -> None:
    """Wall timeout kills the child and returns exit 0 (budget exhaustion, not failure)."""
    import sys
    import time

    import scripts.crosshair_stream as stream_mod

    monkeypatch.setattr(stream_mod, "find_crosshair", lambda: sys.executable)
    out = StringIO()
    started = time.perf_counter()
    # argv becomes: python -c "import time; time.sleep(30)"
    code, stats = stream_mod.run_crosshair(
        "-c",
        ["import time; time.sleep(30)"],
        "cover",
        False,
        False,
        out=out,
        label="plugin/fake_slow.py",
        timeout_sec=0.4,
    )
    elapsed = time.perf_counter() - started
    assert code == 0
    assert elapsed < 5.0
    text = out.getvalue()
    assert "[COVER START" in text
    start_at = text.index("[COVER START")
    timeout_at = text.index("[COVER TIMEOUT")
    assert start_at < timeout_at
    assert "wall 0.4s exceeded for plugin/fake_slow.py" in text
    assert "=== CrossHair COVER DONE (exit 0) ===" in text
    assert stats.failure_count == 0


def test_run_crosshair_timeout_check_mode_tag(monkeypatch) -> None:
    """Check mode wall timeout uses [CHECK TIMEOUT], not COVER."""
    import sys
    import time

    import scripts.crosshair_stream as stream_mod

    monkeypatch.setattr(stream_mod, "find_crosshair", lambda: sys.executable)
    out = StringIO()
    started = time.perf_counter()
    code, stats = stream_mod.run_crosshair(
        "-c",
        ["import time; time.sleep(30)"],
        "check",
        False,
        False,
        out=out,
        label="plugin/fake_slow.py",
        timeout_sec=0.4,
    )
    elapsed = time.perf_counter() - started
    assert code == 0
    assert elapsed < 5.0
    text = out.getvalue()
    assert "[CHECK START" in text
    assert text.index("[CHECK START") < text.index("[CHECK TIMEOUT")
    assert "[CHECK TIMEOUT" in text
    assert "[COVER TIMEOUT" not in text
    assert "=== CrossHair CHECK DONE (exit 0) ===" in text
    assert stats.failure_count == 0


def test_run_crosshair_child_env_sets_short_deal_table(monkeypatch) -> None:
    """make crosshair-check/cover spawn CrossHair with WRITERAGENT_CROSSHAIR=1."""
    from plugin.framework.deal_shim import CROSSHAIR_ENV
    import scripts.crosshair_stream as stream_mod

    captured: dict[str, str] = {}

    class FakeProc:
        stdout = iter(())

        def wait(self) -> int:
            return 0

    def fake_popen(*_args, **kwargs):
        captured.update(kwargs["env"])
        return FakeProc()

    monkeypatch.setattr(stream_mod, "find_crosshair", lambda: "crosshair")
    monkeypatch.setattr(stream_mod.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(stream_mod, "stream_lines", lambda *_a, **_k: stream_mod.StreamStats())
    out = StringIO()
    stream_mod.run_crosshair("cover", ["plugin.foo.bar"], "cover", False, False, out=out)
    assert captured.get(CROSSHAIR_ENV) == captured.get(stream_mod.CROSSHAIR_DEAL_ENV) == "1"
    assert captured.get("PYTHONUNBUFFERED") == "1"


def test_format_prev_mmss() -> None:
    assert format_prev_mmss(7) == "0:07"
    assert format_prev_mmss(18 * 60 + 4) == "18:04"
    assert format_prev_mmss(75 * 60 + 2) == "75:02"


def test_format_check_bracket_no_prev_pads_to_22() -> None:
    assert format_check_bracket("CHECK START", None) == "[CHECK START           ]"
    assert format_check_bracket("CHECK PROGRESS", None) == "[CHECK PROGRESS        ]"


def test_format_check_bracket_prev_progress() -> None:
    assert format_check_bracket("CHECK PROGRESS", 82) == "[CHECK PROGRESS | Prev 1:22]"
    assert format_check_bracket("CHECK START", 18 * 60 + 4) == "[CHECK START | Prev 18:04]"


def test_stream_lines_stamps_prev_between_emitted_check_lines(monkeypatch) -> None:
    """Prev is wall time since the previous emitted tagged line, not FQN start."""
    from scripts.crosshair_stream import PrevLineClock

    times = iter([1000.0, 1082.0])
    monkeypatch.setattr("scripts.crosshair_stream.time.perf_counter", lambda: next(times))
    buf = StringIO()
    stream_lines(
        iter(
            [
                "23222.229|    |analyze_function() Analyzing  host_pack_split_grid\n",
                '23222.251|    |analyze() Analyzing postcondition: " len(result) == len(args[0]) "\n',
            ]
        ),
        mode="check",
        out=buf,
        raw=False,
        quiet=False,
        prev_clock=PrevLineClock(),
    )
    text = buf.getvalue()
    progress_lines = [ln for ln in text.splitlines() if ln.startswith("[CHECK PROGRESS")]
    assert len(progress_lines) == 2
    assert progress_lines[0] == "[CHECK PROGRESS        ] analyzing host_pack_split_grid"
    assert progress_lines[1].startswith("[CHECK PROGRESS | Prev 1:22] post:")
    assert "len(result) == len(args[0])" in progress_lines[1]


def test_stream_lines_cover_does_not_stamp_prev(monkeypatch) -> None:
    from scripts.crosshair_stream import PrevLineClock

    monkeypatch.setattr("scripts.crosshair_stream.time.perf_counter", lambda: 1.0)
    buf = StringIO()
    stream_lines(
        iter(["host_pack_split_grid([])\n", "host_pack_split_grid([1])\n"]),
        mode="cover",
        out=buf,
        raw=False,
        quiet=False,
        prev_clock=PrevLineClock(),
    )
    assert "| Prev" not in buf.getvalue()


def test_stream_lines_quiet_stamps_prev_on_errors(monkeypatch) -> None:
    """Quiet still emits ERROR lines, so those still get | Prev."""
    from scripts.crosshair_stream import PrevLineClock

    times = iter([10.0, 17.0])
    monkeypatch.setattr("scripts.crosshair_stream.time.perf_counter", lambda: next(times))
    buf = StringIO()
    stream_lines(
        iter(
            [
                "23222.229|    |analyze_function() Analyzing  foo\n",
                "plugin/scripting/payload_codec.py:500: error: TypeError: boom\n",
                "plugin/scripting/payload_codec.py:501: error: ValueError: nope\n",
            ]
        ),
        mode="check",
        out=buf,
        raw=False,
        quiet=True,
        prev_clock=PrevLineClock(),
    )
    text = buf.getvalue()
    assert "analyzing foo" not in text
    assert "[CHECK ERROR           ]" in text
    assert "[CHECK ERROR | Prev 0:07]" in text
