# WriterAgent tests — crosshair_stream formatter
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

from io import StringIO
from pathlib import Path

from scripts.crosshair_stream import classify_line, discover_deal_plugin_files, print_error_summary, stream_lines


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


def test_classify_plugin_traceback_frame() -> None:
    line = 'File "/home/keithcu/Desktop/Python/writeragent/plugin/chatbot/memory.py", line 58, in upsert_memory_arguments_dict'
    got = classify_line(line, "check")
    assert got is not None
    assert got.tag == "CHECK ERROR"
    assert "plugin/chatbot/memory.py:58" in got.detail


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
