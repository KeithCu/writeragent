#!/usr/bin/env python3
# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Build a static CI status table from the GitHub Actions API.

Used by ``.github/workflows/ci-status-pages.yml`` to publish
https://keithcu.github.io/writeragent/ . Auth is ``GITHUB_TOKEN`` only
(optional for this public repo). The token is never written into HTML.
"""

from __future__ import annotations

import argparse
import html
import json
import os
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator
from urllib.parse import quote

DEFAULT_REPO = "KeithCu/writeragent"
API_ROOT = "https://api.github.com"
USER_AGENT = "writeragent-ci-status"
API_VERSION = "2022-11-28"
RUNS_PER_PAGE = 50
MAX_RUN_PAGES = 5
OS_LABELS = ("ubuntu-latest", "macos-latest", "windows-latest")

# Each row is the newest job whose name contains every needle. CrossHair
# jobs are ``CrossHair (check-all, ubuntu-latest)`` / ``cover-all`` (see
# crosshair-deep.yml). PR CI jobs are ``Test & Typecheck (os)`` or
# ``Mock LLM Sidebar (os)`` (see pr-ci.yml). ``both`` CrossHair jobs do
# not contain those needles and are ignored.
SUITE_SPECS: tuple[tuple[str, str, str, tuple[str, ...]], ...] = (
    ("CrossHair check-all", "", "crosshair-deep.yml", ("check-all",)),
    ("CrossHair cover-all", "", "crosshair-deep.yml", ("cover-all",)),
    ("Test & Typecheck", "ubuntu-latest", "pr-ci.yml", ("Test & Typecheck", "ubuntu-latest")),
    ("Test & Typecheck", "macos-latest", "pr-ci.yml", ("Test & Typecheck", "macos-latest")),
    ("Test & Typecheck", "windows-latest", "pr-ci.yml", ("Test & Typecheck", "windows-latest")),
    ("Mock LLM Sidebar", "ubuntu-latest", "pr-ci.yml", ("Mock LLM Sidebar", "ubuntu-latest")),
    ("Mock LLM Sidebar", "macos-latest", "pr-ci.yml", ("Mock LLM Sidebar", "macos-latest")),
    ("Mock LLM Sidebar", "windows-latest", "pr-ci.yml", ("Mock LLM Sidebar", "windows-latest")),
)


JsonDict = dict[str, Any]
Fetcher = Callable[[str], JsonDict]


@dataclass(frozen=True)
class SuiteSpec:
    suite: str
    os: str
    workflow: str
    name_contains: tuple[str, ...]


@dataclass(frozen=True)
class StatusRow:
    suite: str
    os: str
    conclusion: str
    sha: str
    when: str
    run_url: str


def suite_specs() -> tuple[SuiteSpec, ...]:
    return tuple(SuiteSpec(*item) for item in SUITE_SPECS)


def job_matches(job_name: str, spec: SuiteSpec) -> bool:
    """True when the Actions job name is this dashboard row.

    Needles are AND-matched so ``Test & Typecheck (ubuntu-latest)`` does
    not fill the macos row or a Mock LLM Sidebar row.
    """
    return all(needle in job_name for needle in spec.name_contains)


def extract_os(job_name: str, fallback: str = "") -> str:
    for label in OS_LABELS:
        if label in job_name:
            return label
    return fallback


def short_sha(sha: str) -> str:
    sha = sha.strip()
    if len(sha) < 7:
        return sha
    return sha[:7]


def job_when(job: JsonDict) -> str:
    for key in ("completed_at", "started_at"):
        value = job.get(key)
        if isinstance(value, str) and value:
            return value
    return ""


def job_conclusion(job: JsonDict) -> str:
    conclusion = job.get("conclusion")
    if isinstance(conclusion, str) and conclusion:
        return conclusion
    status = job.get("status")
    if isinstance(status, str) and status:
        return status
    return "unknown"


def github_get(url: str, token: str) -> JsonDict:
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": USER_AGENT,
        "X-GitHub-Api-Version": API_VERSION,
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=30) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"GitHub API {exc.code} for {url}: {body[:300]}") from exc
    parsed: Any = json.loads(raw.decode("utf-8"))
    if not isinstance(parsed, dict):
        raise RuntimeError(f"GitHub API returned a non-object for {url}")
    return parsed


def make_fetcher(token: str) -> Fetcher:
    def fetch(url: str) -> JsonDict:
        return github_get(url, token)

    return fetch


def iter_workflow_runs(fetch: Fetcher, repo: str, workflow: str) -> Iterator[JsonDict]:
    encoded = quote(workflow, safe="")
    for page in range(1, MAX_RUN_PAGES + 1):
        url = (
            f"{API_ROOT}/repos/{repo}/actions/workflows/{encoded}/runs"
            f"?per_page={RUNS_PER_PAGE}&page={page}"
        )
        payload = fetch(url)
        runs = payload.get("workflow_runs")
        if not isinstance(runs, list) or not runs:
            return
        for run in runs:
            if isinstance(run, dict):
                yield run
        if len(runs) < RUNS_PER_PAGE:
            return


def list_run_jobs(fetch: Fetcher, repo: str, run_id: int) -> list[JsonDict]:
    url = f"{API_ROOT}/repos/{repo}/actions/runs/{run_id}/jobs?per_page=100"
    payload = fetch(url)
    jobs = payload.get("jobs")
    if not isinstance(jobs, list):
        return []
    return [job for job in jobs if isinstance(job, dict)]


def _empty_row(spec: SuiteSpec) -> StatusRow:
    return StatusRow(
        suite=spec.suite,
        os=spec.os,
        conclusion="no run",
        sha="",
        when="",
        run_url="",
    )


def collect_status(fetch: Fetcher, repo: str) -> list[StatusRow]:
    """Newest matching job per suite row (workflow runs are newest-first)."""
    specs = suite_specs()
    found: dict[int, StatusRow] = {}
    pending_by_workflow: dict[str, list[int]] = {}
    for index, spec in enumerate(specs):
        pending_by_workflow.setdefault(spec.workflow, []).append(index)

    for workflow, indexes in pending_by_workflow.items():
        pending = set(indexes)
        for run in iter_workflow_runs(fetch, repo, workflow):
            if not pending:
                break
            run_id = run.get("id")
            if not isinstance(run_id, int):
                continue
            run_url = run.get("html_url") if isinstance(run.get("html_url"), str) else ""
            sha = short_sha(run.get("head_sha") if isinstance(run.get("head_sha"), str) else "")
            jobs = list_run_jobs(fetch, repo, run_id)
            for job in jobs:
                name = job.get("name") if isinstance(job.get("name"), str) else ""
                still_pending = list(pending)
                for index in still_pending:
                    spec = specs[index]
                    if not job_matches(name, spec):
                        continue
                    found[index] = StatusRow(
                        suite=spec.suite,
                        os=spec.os or extract_os(name),
                        conclusion=job_conclusion(job),
                        sha=sha,
                        when=job_when(job),
                        run_url=run_url or "",
                    )
                    pending.discard(index)

    return [found.get(index, _empty_row(spec)) for index, spec in enumerate(specs)]


def _cell(value: str) -> str:
    return html.escape(value, quote=True)


def render_html(
    rows: list[StatusRow],
    *,
    repo: str,
    generated_at: str,
) -> str:
    """Ugly-simple table. No JS, no secrets, no placeholder SHAs."""
    body_rows: list[str] = []
    for row in rows:
        link = ""
        if row.run_url:
            href = _cell(row.run_url)
            link = f'<a href="{href}">{href}</a>'
        css = row.conclusion.replace(" ", "-")
        body_rows.append(
            "<tr>"
            f"<td>{_cell(row.suite)}</td>"
            f"<td>{_cell(row.os)}</td>"
            f'<td class="{_cell(css)}">{_cell(row.conclusion)}</td>'
            f"<td><code>{_cell(row.sha)}</code></td>"
            f"<td>{_cell(row.when)}</td>"
            f"<td>{link}</td>"
            "</tr>"
        )
    table_body = "\n".join(body_rows)
    return (
        "<!DOCTYPE html>\n"
        '<html lang="en">\n'
        "<head>\n"
        '<meta charset="utf-8">\n'
        "<title>WriterAgent CI status</title>\n"
        "<style>\n"
        "body { font-family: sans-serif; margin: 1em; }\n"
        "table { border-collapse: collapse; }\n"
        "th, td { border: 1px solid #333; padding: 4px 8px; text-align: left; }\n"
        "td.success { background: #cfc; }\n"
        "td.failure { background: #fcc; }\n"
        "td.cancelled { background: #eee; }\n"
        "td.in_progress { background: #ffc; }\n"
        "</style>\n"
        "</head>\n"
        "<body>\n"
        "<h1>WriterAgent CI status</h1>\n"
        f"<p>Latest Actions jobs for {_cell(repo)}. Generated { _cell(generated_at) }.</p>\n"
        "<table>\n"
        "<thead><tr>"
        "<th>Suite</th><th>OS</th><th>Conclusion</th><th>SHA</th><th>When</th><th>Run</th>"
        "</tr></thead>\n"
        f"<tbody>\n{table_body}\n</tbody>\n"
        "</table>\n"
        "</body>\n"
        "</html>\n"
    )


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def write_site(out_dir: Path, html_text: str) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    index = out_dir / "index.html"
    index.write_text(html_text, encoding="utf-8")
    return index


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--out",
        default="_site",
        help="Directory to write index.html into (default: _site)",
    )
    parser.add_argument(
        "--repo",
        default="",
        help="owner/name (default: GITHUB_REPOSITORY or KeithCu/writeragent)",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    repo = (args.repo or os.environ.get("GITHUB_REPOSITORY") or DEFAULT_REPO).strip()
    token = os.environ.get("GITHUB_TOKEN", "")
    rows = collect_status(make_fetcher(token), repo)
    page = render_html(rows, repo=repo, generated_at=utc_now())
    if token and token in page:
        raise RuntimeError("refusing to write HTML that contains GITHUB_TOKEN")
    path = write_site(Path(args.out), page)
    print(f"Wrote {path} ({len(rows)} rows)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
