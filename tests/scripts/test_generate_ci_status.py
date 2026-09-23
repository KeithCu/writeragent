# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""CI status page is generated from Actions API data, not placeholders."""

from __future__ import annotations

from io import BytesIO
from pathlib import Path
from datetime import datetime, timezone
from typing import Any
from urllib.error import HTTPError, URLError

import pytest

from scripts.generate_ci_status import (
    GITHUB_GET_ATTEMPTS,
    StatusRow,
    SuiteSpec,
    collect_status,
    github_get,
    github_retry_delay,
    is_expired,
    job_matches,
    main,
    parse_cached_rows,
    parse_retry_after_seconds,
    render_html,
    render_json,
    render_svg,
    short_sha,
    suite_specs,
)

_REPO_ROOT = Path(__file__).resolve().parents[2]
_WORKFLOW = _REPO_ROOT / ".github" / "workflows" / "ci-status-pages.yml"
_GENERATOR = _REPO_ROOT / "scripts" / "generate_ci_status.py"


def _spec(*needles: str, suite: str = "x", os: str = "", workflow: str = "pr-ci.yml") -> SuiteSpec:
    return SuiteSpec(suite=suite, os=os, workflow=workflow, name_contains=needles)


def test_job_matches_crosshair_needles() -> None:
    check = _spec("check-all", workflow="crosshair-deep.yml")
    cover = _spec("cover-all", workflow="crosshair-deep.yml")
    assert job_matches("CrossHair (check-all, ubuntu-latest)", check)
    assert not job_matches("CrossHair (cover-all, ubuntu-latest)", check)
    assert job_matches("CrossHair (cover-all, ubuntu-latest)", cover)
    assert not job_matches("CrossHair (check-all, ubuntu-latest)", cover)
    assert not job_matches("CrossHair (both, ubuntu-latest)", check)
    assert not job_matches("CrossHair (both, ubuntu-latest)", cover)


def test_job_matches_pr_ci_suite_and_os() -> None:
    ubuntu_typecheck = _spec("Test & Typecheck", "ubuntu-latest")
    mac_sidebar = _spec("Mock LLM Sidebar", "macos-latest")
    assert job_matches("Test & Typecheck (ubuntu-latest)", ubuntu_typecheck)
    assert not job_matches("Test & Typecheck (windows-latest)", ubuntu_typecheck)
    assert not job_matches("Mock LLM Sidebar (ubuntu-latest)", ubuntu_typecheck)
    assert job_matches("Mock LLM Sidebar (macos-latest)", mac_sidebar)
    assert not job_matches("Test & Typecheck (macos-latest)", mac_sidebar)


def test_short_sha_is_seven_chars_from_real_oid() -> None:
    assert short_sha("96912c4abf1c2d3e4f567890abcdef1234567890") == "96912c4"
    assert short_sha("") == ""
    assert short_sha("abc") == "abc"


def _run(run_id: int, sha: str, html_url: str) -> dict[str, Any]:
    return {"id": run_id, "head_sha": sha, "html_url": html_url}


def _job(name: str, conclusion: str, when: str) -> dict[str, Any]:
    return {"name": name, "conclusion": conclusion, "completed_at": when, "status": "completed"}


def test_collect_status_picks_newest_matching_jobs() -> None:
    """Fixtures use real KeithCu/writeragent run ids / SHAs / URLs."""
    runs = {
        "crosshair-deep.yml": [
            _run(
                33689813185,
                "6045c1b0aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
                "https://github.com/KeithCu/writeragent/actions/runs/33689813185",
            ),
            _run(
                33677574062,
                "ce0da960bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
                "https://github.com/KeithCu/writeragent/actions/runs/33677574062",
            ),
        ],
        "pr-ci.yml": [
            _run(
                33789965142,
                "a5957240cccccccccccccccccccccccccccccccc",
                "https://github.com/KeithCu/writeragent/actions/runs/33789965142",
            ),
            _run(
                33788378302,
                "96912c40dddddddddddddddddddddddddddddddd",
                "https://github.com/KeithCu/writeragent/actions/runs/33788378302",
            ),
            _run(
                33780513241,
                "589df340eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee",
                "https://github.com/KeithCu/writeragent/actions/runs/33780513241",
            ),
            _run(
                33772067046,
                "85f7a76fffffffffffffffffffffffffffffffff",
                "https://github.com/KeithCu/writeragent/actions/runs/33772067046",
            ),
        ],
    }
    jobs = {
        33689813185: [_job("CrossHair (cover-all, ubuntu-latest)", "cancelled", "2026-09-03T04:19:58Z")],
        33677574062: [_job("CrossHair (check-all, ubuntu-latest)", "failure", "2026-09-02T21:21:27Z")],
        33789965142: [
            {"name": "Test & Typecheck (ubuntu-latest)", "conclusion": None, "status": "in_progress", "started_at": "2026-09-03T18:21:10Z"}
        ],
        33788378302: [_job("Test & Typecheck (windows-latest)", "success", "2026-09-03T18:15:33Z")],
        33780513241: [_job("Test & Typecheck (macos-latest)", "success", "2026-09-03T16:54:58Z")],
        33772067046: [
            _job("Mock LLM Sidebar (ubuntu-latest)", "success", "2026-09-03T15:28:00Z"),
            _job("Mock LLM Sidebar (macos-latest)", "success", "2026-09-03T15:29:00Z"),
            _job("Mock LLM Sidebar (windows-latest)", "success", "2026-09-03T15:30:00Z"),
        ],
    }

    def fetch(url: str) -> dict[str, Any]:
        if "/workflows/crosshair-deep.yml/runs" in url:
            return {"workflow_runs": runs["crosshair-deep.yml"]}
        if "/workflows/pr-ci.yml/runs" in url:
            return {"workflow_runs": runs["pr-ci.yml"]}
        for run_id, job_list in jobs.items():
            if f"/runs/{run_id}/jobs" in url:
                return {"jobs": job_list}
        raise AssertionError(f"unexpected URL {url}")

    rows = collect_status(fetch, "KeithCu/writeragent")
    by_key = {(row.suite, row.os): row for row in rows}
    assert by_key[("CrossHair check-all", "ubuntu-latest")].conclusion == "failure"
    assert by_key[("CrossHair check-all", "ubuntu-latest")].sha == "ce0da96"
    assert by_key[("CrossHair cover-all", "ubuntu-latest")].conclusion == "cancelled"
    assert by_key[("CrossHair cover-all", "ubuntu-latest")].sha == "6045c1b"
    assert by_key[("Test & Typecheck", "ubuntu-latest")].conclusion == "in_progress"
    assert by_key[("Test & Typecheck", "ubuntu-latest")].sha == "a595724"
    assert by_key[("Test & Typecheck", "windows-latest")].sha == "96912c4"
    assert by_key[("Test & Typecheck", "macos-latest")].sha == "589df34"
    assert by_key[("Mock LLM Sidebar", "ubuntu-latest")].sha == "85f7a76"
    assert by_key[("Mock LLM Sidebar", "windows-latest")].run_url.endswith("/33772067046")
    assert all(row.sha != "deadbee" and row.sha != "0000000" for row in rows)


def test_collect_status_empty_is_no_run_not_fake_sha() -> None:
    def fetch(url: str) -> dict[str, Any]:
        if "/runs?" in url or "/runs?per_page" in url:
            return {"workflow_runs": []}
        if url.endswith("/runs"):
            return {"workflow_runs": []}
        if "/workflows/" in url:
            return {"workflow_runs": []}
        raise AssertionError(f"unexpected URL {url}")

    rows = collect_status(fetch, "KeithCu/writeragent")
    assert len(rows) == len(suite_specs())
    assert all(row.conclusion == "no run" for row in rows)
    assert all(row.sha == "" for row in rows)


def _sample_rows() -> list[StatusRow]:
    return [
        StatusRow("CrossHair check-all", "ubuntu-latest", "failure", "ce0da96", "2026-09-02T21:21:27Z", ""),
        StatusRow("CrossHair cover-all", "ubuntu-latest", "cancelled", "6045c1b", "2026-09-03T04:19:58Z", ""),
        StatusRow("Test & Typecheck", "ubuntu-latest", "success", "12a7676", "2026-09-03T19:01:28Z", ""),
        StatusRow("Test & Typecheck", "macos-latest", "success", "589df34", "2026-09-03T16:54:58Z", ""),
        StatusRow("Test & Typecheck", "windows-latest", "success", "96912c4", "2026-09-03T18:15:33Z", ""),
        StatusRow("Mock LLM Sidebar", "ubuntu-latest", "success", "85f7a76", "2026-09-03T15:32:33Z", ""),
        StatusRow("Mock LLM Sidebar", "macos-latest", "success", "85f7a76", "2026-09-03T15:41:00Z", ""),
        StatusRow("Mock LLM Sidebar", "windows-latest", "success", "85f7a76", "2026-09-03T15:25:02Z", ""),
    ]


def test_render_svg_contains_suite_names() -> None:
    svg = render_svg(_sample_rows(), repo="KeithCu/writeragent", generated_at="2026-09-03T19:01:48Z")
    assert svg.startswith("<?xml")
    assert "<svg" in svg
    assert "CrossHair check-all" in svg
    assert "CrossHair cover-all" in svg
    assert "Test &amp; Typecheck" in svg
    assert "Mock LLM Sidebar" in svg
    assert "ubuntu-latest" in svg
    assert "macos-latest" in svg
    assert "windows-latest" in svg
    assert "success" in svg
    assert "failure" in svg
    assert "cancelled" in svg
    assert "ce0da96" in svg
    assert "2026-09-03T19:01:48Z" in svg
    assert "ghs_" not in svg


def test_render_svg_empty_runs_does_not_crash() -> None:
    empty_rows = [
        StatusRow(spec.suite, spec.os, "no run", "", "", "") for spec in suite_specs()
    ]
    svg = render_svg(empty_rows, repo="KeithCu/writeragent", generated_at="2026-09-03T00:00:00Z")
    assert "<svg" in svg
    assert "CrossHair check-all" in svg
    assert "Test &amp; Typecheck" in svg
    assert "Mock LLM Sidebar" in svg
    assert "no run" in svg
    header_only = render_svg([], repo="KeithCu/writeragent", generated_at="2026-09-03T00:00:00Z")
    assert "<svg" in header_only
    assert "</svg>" in header_only


def test_render_svg_escapes_xml() -> None:
    rows = [
        StatusRow(
            suite="<script>alert(1)</script>",
            os="ubuntu-latest",
            conclusion="success",
            sha="96912c4",
            when="2026-09-03T18:15:33Z",
            run_url="",
        )
    ]
    svg = render_svg(rows, repo="KeithCu/writeragent", generated_at="2026-09-03T18:30:00Z")
    assert "<script>alert(1)</script>" not in svg
    assert "&lt;script&gt;" in svg


def test_readme_embeds_pages_svg() -> None:
    text = (_REPO_ROOT / "README.md").read_text(encoding="utf-8")
    assert "![CI status](https://keithcu.github.io/writeragent/status.svg)" in text
    assert "[CI status page](https://keithcu.github.io/writeragent/)" in text


def test_render_html_escapes_and_omits_token() -> None:
    rows = [
        StatusRow(
            suite="<script>alert(1)</script>",
            os="ubuntu-latest",
            conclusion="success",
            sha="96912c4",
            when="2026-09-03T18:15:33Z",
            run_url="https://github.com/KeithCu/writeragent/actions/runs/33788378302",
        )
    ]
    page = render_html(rows, repo="KeithCu/writeragent", generated_at="2026-09-03T18:30:00Z")
    assert "<script>alert(1)</script>" not in page
    assert "&lt;script&gt;" in page
    assert "96912c4" in page
    assert "33788378302" in page
    assert "ghs_" not in page
    assert "GITHUB_TOKEN" not in page


def test_main_writes_index_and_never_embeds_token(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GITHUB_TOKEN", "ghs_this_must_not_appear_in_html")
    monkeypatch.setenv("GITHUB_REPOSITORY", "KeithCu/writeragent")

    def fetch(url: str) -> dict[str, Any]:
        if "crosshair-deep.yml" in url:
            return {
                "workflow_runs": [
                    {
                        "id": 33677574062,
                        "head_sha": "ce0da960bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
                        "html_url": "https://github.com/KeithCu/writeragent/actions/runs/33677574062",
                    }
                ]
            }
        if "pr-ci.yml" in url and "/jobs" not in url:
            return {"workflow_runs": []}
        if "/33677574062/jobs" in url:
            return {
                "jobs": [
                    _job("CrossHair (check-all, ubuntu-latest)", "failure", "2026-09-02T21:21:27Z"),
                ]
            }
        return {"workflow_runs": []}

    monkeypatch.setattr("scripts.generate_ci_status.make_fetcher", lambda token: fetch)
    out = tmp_path / "site"
    assert main(["--out", str(out)]) == 0
    text = (out / "index.html").read_text(encoding="utf-8")
    svg = (out / "status.svg").read_text(encoding="utf-8")
    assert "ghs_this_must_not_appear_in_html" not in text
    assert "ghs_this_must_not_appear_in_html" not in svg
    assert "ce0da96" in text
    assert "ce0da96" in svg
    assert "CrossHair check-all" in text
    assert "CrossHair check-all" in svg
    assert "no run" in text
    assert "no run" in svg


def test_workflow_deploys_pages_from_actions_api() -> None:
    text = _WORKFLOW.read_text(encoding="utf-8")
    assert "workflow_dispatch:" in text
    assert "workflow_run:" in text
    assert "PR CI" in text
    assert "CrossHair Verification (Deep / On-Demand)" in text
    assert "pages: write" in text
    assert "id-token: write" in text
    assert "actions: read" in text
    assert "name: github-pages" in text
    assert "actions/upload-pages-artifact" in text
    assert "actions/deploy-pages" in text
    assert "secrets.GITHUB_TOKEN" in text
    assert "scripts/generate_ci_status.py" in text
    assert "status.svg" in text
    assert "docs/" not in text


def test_generator_and_workflow_live_outside_docs() -> None:
    from scripts.generate_ci_status import SUITE_SPECS

    assert "docs" not in _GENERATOR.parts
    assert "docs" not in _WORKFLOW.parts
    suites = [item[0] for item in SUITE_SPECS]
    assert suites.count("CrossHair check-all") == 1
    assert suites.count("CrossHair cover-all") == 1
    assert suites.count("Test & Typecheck") == 3
    assert suites.count("Mock LLM Sidebar") == 3


def test_is_expired_within_and_outside_window() -> None:
    now = datetime(2026, 9, 9, 12, 0, 0, tzinfo=timezone.utc)
    # 6 days old: within 60 days
    assert not is_expired("2026-09-03T18:15:33Z", max_age_days=60, now=now)
    # 59 days old: within 60 days
    assert not is_expired("2026-07-12T00:00:00Z", max_age_days=60, now=now)
    # 61 days old: expired
    assert is_expired("2026-07-09T00:00:00Z", max_age_days=60, now=now)
    # 100 days old: expired
    assert is_expired("2026-06-01T00:00:00Z", max_age_days=60, now=now)
    # Empty string is expired
    assert is_expired("", max_age_days=60, now=now)
    # Unparseable string does not crash and is safe fallback
    assert not is_expired("invalid-date", max_age_days=60, now=now)


def test_parse_cached_rows_filters_expired_and_no_run() -> None:
    specs = suite_specs()
    now = datetime(2026, 9, 9, 12, 0, 0, tzinfo=timezone.utc)
    cached = {
        "rows": [
            {
                "suite": "Test & Typecheck",
                "os": "macos-latest",
                "conclusion": "success",
                "sha": "589df34",
                "when": "2026-09-03T16:54:58Z",
                "run_url": "https://github.com/KeithCu/writeragent/actions/runs/33780513241",
                "run_id": 33780513241,
            },
            {
                "suite": "Test & Typecheck",
                "os": "windows-latest",
                "conclusion": "success",
                "sha": "96912c4",
                "when": "2026-06-01T00:00:00Z",  # Expired (> 60 days)
                "run_url": "https://github.com/KeithCu/writeragent/actions/runs/100",
                "run_id": 100,
            },
            {
                "suite": "Mock LLM Sidebar",
                "os": "ubuntu-latest",
                "conclusion": "no run",  # Empty
                "sha": "",
                "when": "",
                "run_url": "",
                "run_id": 0,
            },
        ]
    }
    rows = parse_cached_rows(cached, specs, max_age_days=60, now=now)
    # macos-latest is retained
    assert any(r.suite == "Test & Typecheck" and r.os == "macos-latest" and r.sha == "589df34" for r in rows.values())
    # windows-latest was expired, so not retained
    assert not any(r.suite == "Test & Typecheck" and r.os == "windows-latest" for r in rows.values())
    # no run was not retained
    assert not any(r.suite == "Mock LLM Sidebar" and r.os == "ubuntu-latest" for r in rows.values())


def test_parse_cached_rows_filters_non_terminal_conclusions() -> None:
    specs = suite_specs()
    now = datetime(2026, 9, 9, 12, 0, 0, tzinfo=timezone.utc)
    cached = {
        "rows": [
            {
                "suite": "Test & Typecheck",
                "os": "macos-latest",
                "conclusion": "in_progress",
                "sha": "3dc07b3",
                "when": "2026-09-08T12:00:00Z",
                "run_url": "url1",
                "run_id": 101,
            },
            {
                "suite": "Test & Typecheck",
                "os": "windows-latest",
                "conclusion": "queued",
                "sha": "cdf42af",
                "when": "2026-09-08T12:00:00Z",
                "run_url": "url2",
                "run_id": 102,
            },
            {
                "suite": "Test & Typecheck",
                "os": "ubuntu-latest",
                "conclusion": "success",
                "sha": "9ebaad2",
                "when": "2026-09-08T12:00:00Z",
                "run_url": "url3",
                "run_id": 103,
                "run_attempt": 2,
            },
            {
                "suite": "Mock LLM Sidebar",
                "os": "ubuntu-latest",
                "conclusion": "unknown",
                "sha": "1234567",
                "when": "2026-09-08T12:00:00Z",
                "run_url": "url4",
                "run_id": 104,
            },
        ]
    }
    rows = parse_cached_rows(cached, specs, max_age_days=60, now=now)
    # in_progress, queued, and unknown are non-terminal and MUST NOT be accepted as cached hints
    assert not any(r.suite == "Test & Typecheck" and r.os == "macos-latest" for r in rows.values())
    assert not any(r.suite == "Test & Typecheck" and r.os == "windows-latest" for r in rows.values())
    assert not any(r.suite == "Mock LLM Sidebar" and r.os == "ubuntu-latest" for r in rows.values())
    # success is terminal and is retained with its run_attempt
    ubuntu_row = [r for r in rows.values() if r.suite == "Test & Typecheck" and r.os == "ubuntu-latest"][0]
    assert ubuntu_row.conclusion == "success"
    assert ubuntu_row.run_attempt == 2


def test_collect_status_uses_cached_hints_and_stops_early() -> None:
    now = datetime(2026, 9, 9, 12, 0, 0, tzinfo=timezone.utc)
    cached_data = {
        "rows": [
            {
                "suite": "Test & Typecheck",
                "os": "macos-latest",
                "conclusion": "success",
                "sha": "589df34",
                "when": "2026-09-03T16:54:58Z",
                "run_url": "https://github.com/KeithCu/writeragent/actions/runs/33780513241",
                "run_id": 33780513241,
            },
            {
                "suite": "Test & Typecheck",
                "os": "windows-latest",
                "conclusion": "success",
                "sha": "96912c4",
                "when": "2026-09-03T18:15:33Z",
                "run_url": "https://github.com/KeithCu/writeragent/actions/runs/33788378302",
                "run_id": 33788378302,
            },
            {
                "suite": "Mock LLM Sidebar",
                "os": "ubuntu-latest",
                "conclusion": "success",
                "sha": "85f7a76",
                "when": "2026-09-03T15:28:00Z",
                "run_url": "url",
                "run_id": 33788378302,
            },
            {
                "suite": "Mock LLM Sidebar",
                "os": "macos-latest",
                "conclusion": "success",
                "sha": "85f7a76",
                "when": "2026-09-03T15:29:00Z",
                "run_url": "url",
                "run_id": 33788378302,
            },
            {
                "suite": "Mock LLM Sidebar",
                "os": "windows-latest",
                "conclusion": "success",
                "sha": "85f7a76",
                "when": "2026-09-03T15:30:00Z",
                "run_url": "url",
                "run_id": 33788378302,
            },
        ]
    }
    # Run 4000 is a newer run that has ubuntu-latest
    # Run 33780513241 is at or before the checkpoint for all remaining specs
    runs = {
        "crosshair-deep.yml": [],
        "pr-ci.yml": [
            {"id": 40000000000, "head_sha": "new_sha123", "html_url": "https://github.com/KeithCu/writeragent/actions/runs/40000000000", "created_at": "2026-09-09T10:00:00Z", "event": "pull_request"},
            {"id": 33780513241, "head_sha": "589df340000", "html_url": "https://github.com/KeithCu/writeragent/actions/runs/33780513241", "created_at": "2026-09-03T16:50:00Z", "event": "workflow_dispatch"},
        ],
    }
    jobs_called: list[int] = []

    def fetch(url: str) -> dict[str, Any]:
        if "crosshair-deep.yml" in url:
            return {"workflow_runs": []}
        if "pr-ci.yml" in url and "/jobs" not in url:
            return {"workflow_runs": runs["pr-ci.yml"]}
        if "/40000000000/jobs" in url:
            jobs_called.append(40000000000)
            return {"jobs": [_job("Test & Typecheck (ubuntu-latest)", "success", "2026-09-09T10:05:00Z")]}
        if "/33780513241/jobs" in url:
            jobs_called.append(33780513241)
            return {"jobs": [_job("Test & Typecheck (macos-latest)", "success", "2026-09-03T16:54:58Z")]}
        raise AssertionError(f"unexpected URL: {url}")

    rows = collect_status(fetch, "KeithCu/writeragent", cached_data=cached_data, max_age_days=60, now=now)
    by_key = {(r.suite, r.os): r for r in rows}
    # Ubuntu got the new run
    assert by_key[("Test & Typecheck", "ubuntu-latest")].sha == "new_sha"
    assert by_key[("Test & Typecheck", "ubuntu-latest")].conclusion == "success"
    # MacOS retained the cached run
    assert by_key[("Test & Typecheck", "macos-latest")].sha == "589df34"
    assert by_key[("Test & Typecheck", "macos-latest")].run_id == 33780513241
    # Windows retained the cached run
    assert by_key[("Test & Typecheck", "windows-latest")].sha == "96912c4"
    # Because all checkpoints were reached, jobs were NOT requested for 33780513241
    assert 33780513241 not in jobs_called


def test_collect_status_supersedes_cached_hint_when_newer_run_exists() -> None:
    now = datetime(2026, 9, 9, 12, 0, 0, tzinfo=timezone.utc)
    cached_data = {
        "rows": [
            {
                "suite": "Test & Typecheck",
                "os": "macos-latest",
                "conclusion": "failure",
                "sha": "old_sha",
                "when": "2026-09-01T00:00:00Z",
                "run_url": "https://github.com/KeithCu/writeragent/actions/runs/100",
                "run_id": 100,
            }
        ]
    }
    runs = {
        "crosshair-deep.yml": [],
        "pr-ci.yml": [
            {"id": 200, "head_sha": "new_mac_sha", "html_url": "https://github.com/KeithCu/writeragent/actions/runs/200", "created_at": "2026-09-08T00:00:00Z", "event": "workflow_dispatch"},
        ],
    }

    def fetch(url: str) -> dict[str, Any]:
        if "crosshair-deep.yml" in url:
            return {"workflow_runs": []}
        if "pr-ci.yml" in url and "/jobs" not in url:
            return {"workflow_runs": runs["pr-ci.yml"]}
        if "/200/jobs" in url:
            return {"jobs": [_job("Test & Typecheck (macos-latest)", "success", "2026-09-08T00:10:00Z")]}
        raise AssertionError(f"unexpected URL: {url}")

    rows = collect_status(fetch, "KeithCu/writeragent", cached_data=cached_data, max_age_days=60, now=now)
    by_key = {(r.suite, r.os): r for r in rows}
    assert by_key[("Test & Typecheck", "macos-latest")].conclusion == "success"
    assert by_key[("Test & Typecheck", "macos-latest")].sha == "new_mac"
    assert by_key[("Test & Typecheck", "macos-latest")].run_id == 200


def test_collect_status_skips_pr_runs_when_ubuntu_typecheck_resolved() -> None:
    now = datetime(2026, 9, 9, 12, 0, 0, tzinfo=timezone.utc)
    # Run 500: PR run (gives ubuntu-latest)
    # Runs 499, 498, 497: PR runs that should be skipped without calling jobs
    runs = {
        "crosshair-deep.yml": [],
        "pr-ci.yml": [
            {"id": 500, "head_sha": "sha500", "html_url": "url500", "created_at": "2026-09-09T10:00:00Z", "event": "pull_request"},
            {"id": 499, "head_sha": "sha499", "html_url": "url499", "created_at": "2026-09-09T09:00:00Z", "event": "pull_request"},
            {"id": 498, "head_sha": "sha498", "html_url": "url498", "created_at": "2026-09-09T08:00:00Z", "event": "pull_request"},
        ],
    }
    job_calls = []

    def fetch(url: str) -> dict[str, Any]:
        if "crosshair-deep.yml" in url:
            return {"workflow_runs": []}
        if "pr-ci.yml" in url and "/jobs" not in url:
            return {"workflow_runs": runs["pr-ci.yml"]}
        if "/500/jobs" in url:
            job_calls.append(500)
            return {"jobs": [_job("Test & Typecheck (ubuntu-latest)", "success", "2026-09-09T10:05:00Z")]}
        if "/499/jobs" in url or "/498/jobs" in url:
            job_calls.append(int(url.split("/runs/")[1].split("/")[0]))
            return {"jobs": []}
        raise AssertionError(f"unexpected URL: {url}")

    rows = collect_status(fetch, "KeithCu/writeragent", max_age_days=60, now=now)
    assert job_calls == [500]  # Only run 500 called jobs; 499 and 498 were skipped!
    by_key = {(r.suite, r.os): r for r in rows}
    assert by_key[("Test & Typecheck", "ubuntu-latest")].conclusion == "success"


def test_render_json_includes_run_ids() -> None:
    rows = [
        StatusRow("Test & Typecheck", "macos-latest", "success", "589df34", "2026-09-03T16:54:58Z", "url", run_id=33780513241, run_attempt=2),
    ]
    raw = render_json(rows, repo="KeithCu/writeragent", generated_at="2026-09-09T12:00:00Z", max_age_days=60)
    import json
    data = json.loads(raw)
    assert data["max_age_days"] == 60
    assert data["rows"][0]["suite"] == "Test & Typecheck"
    assert data["rows"][0]["run_id"] == 33780513241
    assert data["rows"][0]["run_attempt"] == 2


def test_collect_status_re_evaluates_in_progress_cached_hint() -> None:
    """When a previous cache contains an in_progress run, it must not be treated as a checkpoint.

    Instead, collect_status must re-evaluate the run and pick up its final success conclusion.
    """
    now = datetime(2026, 9, 20, 12, 0, 0, tzinfo=timezone.utc)
    cached_data = {
        "rows": [
            {
                "suite": "Test & Typecheck",
                "os": "macos-latest",
                "conclusion": "in_progress",
                "sha": "3dc07b3",
                "when": "2026-09-20T00:24:31Z",
                "run_url": "https://github.com/KeithCu/writeragent/actions/runs/35478652597",
                "run_id": 35478652597,
            }
        ]
    }
    runs = {
        "crosshair-deep.yml": [],
        "pr-ci.yml": [
            {
                "id": 35478652597,
                "head_sha": "3dc07b30000",
                "html_url": "https://github.com/KeithCu/writeragent/actions/runs/35478652597",
                "created_at": "2026-09-20T00:23:38Z",
                "event": "workflow_dispatch",
                "run_attempt": 1,
            },
        ],
    }

    def fetch(url: str) -> dict[str, Any]:
        if "crosshair-deep.yml" in url:
            return {"workflow_runs": []}
        if "pr-ci.yml" in url and "/jobs" not in url:
            return {"workflow_runs": runs["pr-ci.yml"]}
        if "/35478652597/jobs" in url:
            return {
                "jobs": [
                    _job("Test & Typecheck (macos-latest)", "success", "2026-09-20T00:37:50Z")
                ]
            }
        raise AssertionError(f"unexpected URL: {url}")

    rows = collect_status(fetch, "KeithCu/writeragent", cached_data=cached_data, max_age_days=60, now=now)
    by_key = {(r.suite, r.os): r for r in rows}
    # macOS must resolve to success, not remain in_progress
    assert by_key[("Test & Typecheck", "macos-latest")].conclusion == "success"
    assert by_key[("Test & Typecheck", "macos-latest")].sha == "3dc07b3"
    assert by_key[("Test & Typecheck", "macos-latest")].run_id == 35478652597


def test_collect_status_re_evaluates_on_higher_run_attempt() -> None:
    """When a workflow run was re-run (run_attempt incremented), it must not be discarded."""
    now = datetime(2026, 9, 20, 12, 0, 0, tzinfo=timezone.utc)
    cached_data = {
        "rows": [
            {
                "suite": "Test & Typecheck",
                "os": "macos-latest",
                "conclusion": "failure",
                "sha": "3dc07b3",
                "when": "2026-09-20T00:24:31Z",
                "run_url": "https://github.com/KeithCu/writeragent/actions/runs/35478652597",
                "run_id": 35478652597,
                "run_attempt": 1,
            }
        ]
    }
    runs = {
        "crosshair-deep.yml": [],
        "pr-ci.yml": [
            {
                "id": 35478652597,
                "head_sha": "3dc07b30000",
                "html_url": "https://github.com/KeithCu/writeragent/actions/runs/35478652597",
                "created_at": "2026-09-20T00:23:38Z",
                "event": "workflow_dispatch",
                "run_attempt": 2,
            },
        ],
    }

    def fetch(url: str) -> dict[str, Any]:
        if "crosshair-deep.yml" in url:
            return {"workflow_runs": []}
        if "pr-ci.yml" in url and "/jobs" not in url:
            return {"workflow_runs": runs["pr-ci.yml"]}
        if "/35478652597/jobs" in url:
            return {
                "jobs": [
                    _job("Test & Typecheck (macos-latest)", "success", "2026-09-20T01:00:00Z")
                ]
            }
        raise AssertionError(f"unexpected URL: {url}")

    rows = collect_status(fetch, "KeithCu/writeragent", cached_data=cached_data, max_age_days=60, now=now)
    by_key = {(r.suite, r.os): r for r in rows}
    assert by_key[("Test & Typecheck", "macos-latest")].conclusion == "success"
    assert by_key[("Test & Typecheck", "macos-latest")].run_attempt == 2


def test_workflow_restores_and_saves_cache() -> None:
    text = _WORKFLOW.read_text(encoding="utf-8")
    assert "actions/cache/restore" in text
    assert "actions/cache/save" in text
    assert "--cache .cache/ci-status/status.json" in text


class _FakeHTTPResponse:
    def __init__(self, payload: bytes) -> None:
        self._payload = payload

    def read(self) -> bytes:
        return self._payload

    def __enter__(self) -> _FakeHTTPResponse:
        return self

    def __exit__(self, *args: object) -> None:
        return None


def _http_error(
    url: str,
    code: int,
    body: bytes = b'{"message": "Server Error"}',
    headers: dict[str, str] | None = None,
) -> HTTPError:
    return HTTPError(url, code, "Server Error", headers or {}, BytesIO(body))


def test_parse_retry_after_seconds() -> None:
    assert parse_retry_after_seconds(None) is None
    assert parse_retry_after_seconds("") is None
    assert parse_retry_after_seconds("Wed, 21 Oct 2015 07:28:00 GMT") is None
    assert parse_retry_after_seconds("-1") is None
    assert parse_retry_after_seconds("7") == 7.0
    assert parse_retry_after_seconds("120") == 30.0


def test_github_retry_delay_exponential_and_retry_after() -> None:
    assert github_retry_delay(502, {}, 1) == 2.0
    assert github_retry_delay(503, {}, 2) == 4.0
    assert github_retry_delay(504, {}, 3) == 8.0
    assert github_retry_delay(429, {"Retry-After": "7"}, 1) == 7.0
    assert github_retry_delay(429, {"Retry-After": "not-a-number"}, 1) == 2.0
    assert github_retry_delay(429, {}, 1) == 2.0


def test_github_get_retries_transient_502_then_succeeds(monkeypatch: pytest.MonkeyPatch) -> None:
    url = "https://api.github.com/repos/KeithCu/writeragent/actions/runs/35406653422/jobs?per_page=100"
    calls = {"n": 0}
    sleeps: list[float] = []

    def fake_urlopen(request: object, timeout: int = 30) -> _FakeHTTPResponse:
        calls["n"] += 1
        if calls["n"] == 1:
            raise _http_error(url, 502)
        return _FakeHTTPResponse(b'{"jobs": []}')

    monkeypatch.setattr("scripts.generate_ci_status.urllib.request.urlopen", fake_urlopen)
    result = github_get(url, "", sleep=sleeps.append)
    assert result == {"jobs": []}
    assert calls["n"] == 2
    assert sleeps == [2.0]


def test_github_get_fails_fast_on_404(monkeypatch: pytest.MonkeyPatch) -> None:
    url = "https://api.github.com/repos/KeithCu/writeragent/actions/runs/1/jobs"
    calls = {"n": 0}
    sleeps: list[float] = []

    def fake_urlopen(request: object, timeout: int = 30) -> _FakeHTTPResponse:
        calls["n"] += 1
        raise _http_error(url, 404, b'{"message": "Not Found"}')

    monkeypatch.setattr("scripts.generate_ci_status.urllib.request.urlopen", fake_urlopen)
    with pytest.raises(RuntimeError, match="GitHub API 404"):
        github_get(url, "", sleep=sleeps.append)
    assert calls["n"] == 1
    assert sleeps == []


def test_github_get_respects_retry_after_on_429(monkeypatch: pytest.MonkeyPatch) -> None:
    url = "https://api.github.com/repos/KeithCu/writeragent/actions/workflows/pr-ci.yml/runs"
    calls = {"n": 0}
    sleeps: list[float] = []

    def fake_urlopen(request: object, timeout: int = 30) -> _FakeHTTPResponse:
        calls["n"] += 1
        if calls["n"] == 1:
            raise _http_error(
                url,
                429,
                b'{"message": "API rate limit exceeded"}',
                {"Retry-After": "5"},
            )
        return _FakeHTTPResponse(b'{"workflow_runs": []}')

    monkeypatch.setattr("scripts.generate_ci_status.urllib.request.urlopen", fake_urlopen)
    result = github_get(url, "", sleep=sleeps.append)
    assert result == {"workflow_runs": []}
    assert calls["n"] == 2
    assert sleeps == [5.0]


def test_github_get_exhausts_retries_on_persistent_502(monkeypatch: pytest.MonkeyPatch) -> None:
    url = "https://api.github.com/repos/KeithCu/writeragent/actions/runs/35406653422/jobs"
    calls = {"n": 0}
    sleeps: list[float] = []

    def fake_urlopen(request: object, timeout: int = 30) -> _FakeHTTPResponse:
        calls["n"] += 1
        raise _http_error(url, 502)

    monkeypatch.setattr("scripts.generate_ci_status.urllib.request.urlopen", fake_urlopen)
    with pytest.raises(RuntimeError, match="GitHub API 502"):
        github_get(url, "", sleep=sleeps.append)
    assert calls["n"] == GITHUB_GET_ATTEMPTS
    assert sleeps == [2.0, 4.0, 8.0]


def test_github_get_retries_urlerror_then_succeeds(monkeypatch: pytest.MonkeyPatch) -> None:
    url = "https://api.github.com/repos/KeithCu/writeragent/actions/runs/1/jobs"
    calls = {"n": 0}
    sleeps: list[float] = []

    def fake_urlopen(request: object, timeout: int = 30) -> _FakeHTTPResponse:
        calls["n"] += 1
        if calls["n"] == 1:
            raise URLError("temporarily unavailable")
        return _FakeHTTPResponse(b'{"jobs": []}')

    monkeypatch.setattr("scripts.generate_ci_status.urllib.request.urlopen", fake_urlopen)
    result = github_get(url, "", sleep=sleeps.append)
    assert result == {"jobs": []}
    assert calls["n"] == 2
    assert sleeps == [2.0]
