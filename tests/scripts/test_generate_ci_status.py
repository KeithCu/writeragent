# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""CI status page is generated from Actions API data, not placeholders."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from scripts.generate_ci_status import (
    StatusRow,
    SuiteSpec,
    collect_status,
    job_matches,
    main,
    render_html,
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
