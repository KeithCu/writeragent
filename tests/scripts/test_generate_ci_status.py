# WriterAgent tests — GitHub Pages CI status generator
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

from typing import Any

import pytest

from scripts.generate_ci_status import (
    RowWanted,
    StatusRow,
    WANTED_ROWS,
    classify_conclusion,
    collect_rows,
    format_utc,
    github_get_json,
    job_matches,
    os_from_job_name,
    render_html,
    short_sha,
    write_site,
)


def test_wanted_rows_cover_the_suites_keith_asks_for() -> None:
    suites = [(row.suite, row.os_name, row.job_contains, row.workflow_file) for row in WANTED_ROWS]
    assert suites == [
        ("CrossHair check-all", None, "check-all", "crosshair-deep.yml"),
        ("CrossHair cover-all", None, "cover-all", "crosshair-deep.yml"),
        ("Test & Typecheck (UNO+pytest+packaging)", "ubuntu-latest", "Test & Typecheck", "pr-ci.yml"),
        ("Test & Typecheck (UNO+pytest+packaging)", "macos-latest", "Test & Typecheck", "pr-ci.yml"),
        ("Test & Typecheck (UNO+pytest+packaging)", "windows-latest", "Test & Typecheck", "pr-ci.yml"),
        ("Mock LLM Sidebar", "ubuntu-latest", "Mock LLM Sidebar", "pr-ci.yml"),
        ("Mock LLM Sidebar", "macos-latest", "Mock LLM Sidebar", "pr-ci.yml"),
        ("Mock LLM Sidebar", "windows-latest", "Mock LLM Sidebar", "pr-ci.yml"),
    ]


@pytest.mark.parametrize(
    ("job_name", "contains", "os_name", "match_both", "expected"),
    [
        ("CrossHair (check-all, ubuntu-latest)", "check-all", None, True, True),
        ("CrossHair (cover-all, ubuntu-latest)", "cover-all", None, True, True),
        ("CrossHair (check-all, ubuntu-latest)", "cover-all", None, True, False),
        ("CrossHair (both, ubuntu-latest)", "check-all", None, True, True),
        ("CrossHair (both, ubuntu-latest)", "cover-all", None, True, True),
        ("CrossHair (both, ubuntu-latest)", "check-all", None, False, False),
        ("Test & Typecheck (ubuntu-latest)", "Test & Typecheck", "ubuntu-latest", False, True),
        ("Test & Typecheck (macos-latest)", "Test & Typecheck", "ubuntu-latest", False, False),
        ("Mock LLM Sidebar (windows-latest)", "Mock LLM Sidebar", "windows-latest", False, True),
        ("Test & Typecheck (windows-latest)", "Mock LLM Sidebar", "windows-latest", False, False),
    ],
)
def test_job_matches(job_name: str, contains: str, os_name: str | None, match_both: bool, expected: bool) -> None:
    wanted = RowWanted("suite", "wf.yml", contains, os_name, match_both)
    assert job_matches(job_name, wanted) is expected


def test_os_from_job_name() -> None:
    assert os_from_job_name("Test & Typecheck (macos-latest)") == "macos-latest"
    assert os_from_job_name("CrossHair (check-all, ubuntu-latest)") == "ubuntu-latest"
    assert os_from_job_name("no parens") == ""


def test_classify_and_sha_and_time() -> None:
    assert classify_conclusion("in_progress", None) == "in-progress"
    assert classify_conclusion("completed", "success") == "success"
    assert classify_conclusion("completed", "failure") == "failure"
    assert classify_conclusion("completed", "timed_out") == "failure"
    assert classify_conclusion("completed", "cancelled") == "cancelled"
    assert short_sha("abcdef1234567890") == "abcdef1"
    assert format_utc("2026-09-03T15:41:00Z") == "2026-09-03 15:41 UTC"


def _run(run_id: int, sha: str, created: str, url: str | None = None) -> dict[str, Any]:
    return {
        "id": run_id,
        "head_sha": sha,
        "created_at": created,
        "updated_at": created,
        "html_url": url or f"https://github.com/KeithCu/writeragent/actions/runs/{run_id}",
        "status": "completed",
    }


def _job(name: str, conclusion: str, completed: str, status: str = "completed") -> dict[str, Any]:
    return {"name": name, "conclusion": conclusion, "status": status, "completed_at": completed, "started_at": completed}


def test_collect_rows_picks_newest_real_jobs_and_ignores_skipped() -> None:
    runs = {
        "crosshair-deep.yml": [
            _run(2, "bbbbbbbcccccccc", "2026-09-02T20:00:00Z"),
            _run(1, "aaaaaaacccccccc", "2026-09-03T04:00:00Z"),
            _run(3, "bothbothbothxxx", "2026-08-01T00:00:00Z"),
        ],
        "pr-ci.yml": [
            _run(10, "1111111aaaaaaaa", "2026-09-03T18:00:00Z"),
            _run(11, "2222222aaaaaaaa", "2026-09-03T17:00:00Z"),
            _run(12, "3333333aaaaaaaa", "2026-09-03T16:00:00Z"),
            _run(13, "4444444aaaaaaaa", "2026-09-03T15:00:00Z"),
        ],
    }
    jobs = {
        1: [_job("CrossHair (cover-all, ubuntu-latest)", "cancelled", "2026-09-03T04:19:00Z")],
        2: [_job("CrossHair (check-all, ubuntu-latest)", "failure", "2026-09-02T21:21:00Z")],
        3: [_job("CrossHair (both, ubuntu-latest)", "success", "2026-08-01T01:00:00Z")],
        10: [
            _job("Test & Typecheck (ubuntu-latest)", None, "2026-09-03T18:21:00Z", status="in_progress"),
            _job("Test & Typecheck (skipped-os)", "skipped", "2026-09-03T18:21:00Z"),
        ],
        11: [_job("Test & Typecheck (windows-latest)", "success", "2026-09-03T18:15:00Z")],
        12: [_job("Test & Typecheck (macos-latest)", "success", "2026-09-03T16:54:00Z")],
        13: [
            _job("Mock LLM Sidebar (ubuntu-latest)", "success", "2026-09-03T15:32:00Z"),
            _job("Mock LLM Sidebar (macos-latest)", "success", "2026-09-03T15:41:00Z"),
            _job("Mock LLM Sidebar (windows-latest)", "success", "2026-09-03T15:25:00Z"),
        ],
    }

    rows = collect_rows(lambda wf: runs[wf], lambda rid: jobs[rid])
    by_key = {(row.suite, row.os_name): row for row in rows}

    check = by_key[("CrossHair check-all", "ubuntu-latest")]
    assert check.conclusion == "failure"
    assert check.sha == "bbbbbbb"
    assert check.run_id == "2"

    cover = by_key[("CrossHair cover-all", "ubuntu-latest")]
    assert cover.conclusion == "cancelled"
    assert cover.sha == "aaaaaaa"
    assert cover.run_id == "1"

    assert by_key[("Test & Typecheck (UNO+pytest+packaging)", "ubuntu-latest")].conclusion == "in-progress"
    assert by_key[("Test & Typecheck (UNO+pytest+packaging)", "ubuntu-latest")].sha == "1111111"
    macos = by_key[("Test & Typecheck (UNO+pytest+packaging)", "macos-latest")]
    assert macos.conclusion == "success"
    assert macos.sha == "3333333"
    windows = by_key[("Test & Typecheck (UNO+pytest+packaging)", "windows-latest")]
    assert windows.conclusion == "success"
    assert windows.sha == "2222222"
    assert by_key[("Mock LLM Sidebar", "macos-latest")].run_id == "13"


def test_collect_rows_both_fills_both_crosshair_suites_when_newest() -> None:
    runs = {
        "crosshair-deep.yml": [_run(9, "deadbeefdeadbee", "2026-09-03T12:00:00Z")],
        "pr-ci.yml": [],
    }
    jobs = {9: [_job("CrossHair (both, windows-latest)", "success", "2026-09-03T12:30:00Z")]}
    rows = collect_rows(lambda wf: runs[wf], lambda rid: jobs.get(rid, []))
    cross = [row for row in rows if row.suite.startswith("CrossHair")]
    assert {row.suite for row in cross} == {"CrossHair check-all", "CrossHair cover-all"}
    assert all(row.conclusion == "success" and row.os_name == "windows-latest" for row in cross)
    missing = [row for row in rows if row.conclusion == "no run found"]
    assert len(missing) == 6


def test_render_html_escapes_and_colors(tmp_path: Any) -> None:
    rows = [
        StatusRow("Suite <x>", "os&", "success", "abc1234", "2026-09-03 15:41 UTC", "https://example.test/1", "99"),
        StatusRow("Fail", "ubuntu-latest", "failure", "def5678", "2026-09-01 00:00 UTC", "https://example.test/2", "88"),
        StatusRow("Gone", "", "no run found", "", "", "", ""),
    ]
    page = render_html(rows, repo="KeithCu/writeragent", generated_at="2026-09-03 18:00 UTC")
    assert "<script>" not in page
    assert "Suite &lt;x&gt;" in page
    assert "os&amp;" in page
    assert 'class="ok">success</class>' not in page
    assert 'class="ok">success</td>' in page
    assert 'class="bad">failure</td>' in page
    assert "https://example.test/1" in page
    assert "keithcu.com" not in page.lower() or "not keithcu.com" in page
    assert "When (UTC)" in page
    write_site(str(tmp_path), page)
    assert (tmp_path / "index.html").is_file()
    assert (tmp_path / ".nojekyll").is_file()


def test_github_get_json_rejects_non_api_hosts() -> None:
    with pytest.raises(ValueError, match="refusing non-GitHub API URL"):
        github_get_json("https://keithcu.com/status", token=None)
