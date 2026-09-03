#!/usr/bin/env python3
# WriterAgent — GitHub Pages CI status table (Actions API, no scraping)
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Build a one-table HTML dashboard of the Actions runs Keith actually looks up.

Reads the GitHub Actions REST API (``GITHUB_TOKEN`` in CI; public unauthenticated
reads work for this repo) and writes ``index.html``. Deployed by
``.github/workflows/ci-status.yml`` — not the product manual under ``docs/``.

CrossHair jobs are named ``CrossHair (<target>, <os>)``. A ``both`` target runs
check-all and cover-all in one job, so it fills both rows when it is newer than
the dedicated sweeps.
"""

from __future__ import annotations

import argparse
import html
import json
import os
import re
import sys
import urllib.error
import urllib.request
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse

API_ROOT = "https://api.github.com"
DEFAULT_REPO = "KeithCu/writeragent"
USER_AGENT = "writeragent-ci-status"
MAX_RUN_PAGES = 3
RUNS_PER_PAGE = 100

# Job names from .github/workflows/crosshair-deep.yml and pr-ci.yml.
_CROSSHAIR_TARGET = re.compile(r"crosshair\s*\(\s*([^,)]+)", re.IGNORECASE)
_JOB_OS = re.compile(r"\(([^)]+)\)\s*$")

GetJson = Callable[[str], Any]


@dataclass(frozen=True)
class RowWanted:
    """One dashboard row Keith wants, matched against Actions job names."""

    suite: str
    workflow_file: str
    # Substring the job name must contain (e.g. "check-all", "Test & Typecheck").
    job_contains: str
    os_name: str | None = None
    # CrossHair workflow_dispatch target "both" runs both sweeps in one job.
    match_both: bool = False


@dataclass(frozen=True)
class StatusRow:
    suite: str
    os_name: str
    conclusion: str
    sha: str
    when_utc: str
    run_url: str
    run_id: str


WANTED_ROWS: tuple[RowWanted, ...] = (
    RowWanted("CrossHair check-all", "crosshair-deep.yml", "check-all", match_both=True),
    RowWanted("CrossHair cover-all", "crosshair-deep.yml", "cover-all", match_both=True),
    RowWanted("Test & Typecheck (UNO+pytest+packaging)", "pr-ci.yml", "Test & Typecheck", "ubuntu-latest"),
    RowWanted("Test & Typecheck (UNO+pytest+packaging)", "pr-ci.yml", "Test & Typecheck", "macos-latest"),
    RowWanted("Test & Typecheck (UNO+pytest+packaging)", "pr-ci.yml", "Test & Typecheck", "windows-latest"),
    RowWanted("Mock LLM Sidebar", "pr-ci.yml", "Mock LLM Sidebar", "ubuntu-latest"),
    RowWanted("Mock LLM Sidebar", "pr-ci.yml", "Mock LLM Sidebar", "macos-latest"),
    RowWanted("Mock LLM Sidebar", "pr-ci.yml", "Mock LLM Sidebar", "windows-latest"),
)


def short_sha(sha: str) -> str:
    sha = (sha or "").strip()
    if len(sha) <= 7:
        return sha
    return sha[:7]


def format_utc(stamp: str | None) -> str:
    """GitHub timestamps are ISO-8601 Zulu; label the column as UTC."""
    if not stamp:
        return ""
    text = stamp.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return stamp
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    dt = dt.astimezone(timezone.utc)
    return dt.strftime("%Y-%m-%d %H:%M UTC")


def classify_conclusion(status: str | None, conclusion: str | None) -> str:
    """Map Actions status/conclusion onto the four colors Keith asked for."""
    st = (status or "").lower()
    if st in {"in_progress", "queued", "waiting", "pending", "requested"}:
        return "in-progress"
    con = (conclusion or "").lower()
    if con == "success":
        return "success"
    if con in {"failure", "timed_out", "startup_failure"}:
        return "failure"
    if con == "cancelled":
        return "cancelled"
    if con == "skipped":
        return "skipped"
    if st and st != "completed":
        return "in-progress"
    return con or "unknown"


def os_from_job_name(job_name: str) -> str:
    """``Test & Typecheck (macos-latest)`` / ``CrossHair (check-all, ubuntu-latest)``."""
    match = _JOB_OS.search(job_name or "")
    if match is None:
        return ""
    inner = match.group(1)
    if "," in inner:
        return inner.split(",")[-1].strip()
    return inner.strip()


def job_matches(job_name: str, wanted: RowWanted) -> bool:
    name = job_name or ""
    if wanted.os_name and wanted.os_name.lower() not in name.lower():
        return False
    needle = wanted.job_contains.lower()
    if needle in name.lower():
        return True
    if not wanted.match_both:
        return False
    target = _CROSSHAIR_TARGET.search(name)
    return bool(target and target.group(1).strip().lower() == "both")


def _run_sort_key(run: dict[str, Any]) -> str:
    return str(run.get("created_at") or "")


def collect_rows(
    iter_runs: Callable[[str], Iterable[dict[str, Any]]],
    list_jobs: Callable[[int], list[dict[str, Any]]],
    wanted: tuple[RowWanted, ...] = WANTED_ROWS,
) -> list[StatusRow]:
    """Newest matching job per row. Skipped jobs are not a result (they never ran)."""
    filled: list[StatusRow | None] = [None] * len(wanted)
    workflows = {row.workflow_file for row in wanted}
    for workflow_file in workflows:
        indexes = [i for i, row in enumerate(wanted) if row.workflow_file == workflow_file]
        if all(filled[i] is not None for i in indexes):
            continue
        runs = sorted(iter_runs(workflow_file), key=_run_sort_key, reverse=True)
        for run in runs:
            if all(filled[i] is not None for i in indexes):
                break
            run_id = run.get("id")
            if not isinstance(run_id, int):
                continue
            for job in list_jobs(run_id):
                if (job.get("conclusion") or "").lower() == "skipped":
                    continue
                job_name = str(job.get("name") or "")
                for i in indexes:
                    if filled[i] is not None:
                        continue
                    spec = wanted[i]
                    if not job_matches(job_name, spec):
                        continue
                    when = job.get("completed_at") or job.get("started_at") or run.get("updated_at")
                    filled[i] = StatusRow(
                        suite=spec.suite,
                        os_name=spec.os_name or os_from_job_name(job_name),
                        conclusion=classify_conclusion(
                            job.get("status") or run.get("status"),
                            job.get("conclusion"),
                        ),
                        sha=short_sha(str(run.get("head_sha") or "")),
                        when_utc=format_utc(when if isinstance(when, str) else None),
                        run_url=str(run.get("html_url") or ""),
                        run_id=str(run_id),
                    )
    rows: list[StatusRow] = []
    for spec, found in zip(wanted, filled, strict=True):
        if found is not None:
            rows.append(found)
            continue
        rows.append(
            StatusRow(
                suite=spec.suite,
                os_name=spec.os_name or "",
                conclusion="no run found",
                sha="",
                when_utc="",
                run_url="",
                run_id="",
            )
        )
    return rows


def _assert_github_api_url(url: str) -> None:
    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.netloc != "api.github.com":
        raise ValueError(f"refusing non-GitHub API URL: {url}")


def github_get_json(url: str, token: str | None) -> Any:
    _assert_github_api_url(url)
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": USER_AGENT,
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=45) as resp:  # noqa: S310 — host pinned to api.github.com
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"GitHub API {exc.code} for {url}: {body[:400]}") from exc


def iter_workflow_runs(repo: str, workflow_file: str, get_json: GetJson) -> Iterable[dict[str, Any]]:
    for page in range(1, MAX_RUN_PAGES + 1):
        url = (
            f"{API_ROOT}/repos/{repo}/actions/workflows/{workflow_file}/runs"
            f"?per_page={RUNS_PER_PAGE}&page={page}"
        )
        payload = get_json(url)
        runs = payload.get("workflow_runs") if isinstance(payload, dict) else None
        if not isinstance(runs, list) or not runs:
            return
        yield from runs
        if len(runs) < RUNS_PER_PAGE:
            return


def list_run_jobs(repo: str, run_id: int, get_json: GetJson) -> list[dict[str, Any]]:
    jobs: list[dict[str, Any]] = []
    page = 1
    while True:
        url = f"{API_ROOT}/repos/{repo}/actions/runs/{run_id}/jobs?per_page=100&page={page}"
        payload = get_json(url)
        batch = payload.get("jobs") if isinstance(payload, dict) else None
        if not isinstance(batch, list) or not batch:
            break
        jobs.extend(batch)
        if len(batch) < 100:
            break
        page += 1
    return jobs


def _conclusion_class(conclusion: str) -> str:
    if conclusion == "success":
        return "ok"
    if conclusion in {"failure", "timed_out", "startup_failure"}:
        return "bad"
    if conclusion == "cancelled":
        return "cancelled"
    if conclusion == "in-progress":
        return "progress"
    return "other"


def render_html(rows: list[StatusRow], *, repo: str, generated_at: str) -> str:
    """Ugly-simple table. No JS, no keys, no third-party assets."""
    cells: list[str] = []
    for row in rows:
        cls = _conclusion_class(row.conclusion)
        if row.run_url:
            link = f'<a href="{html.escape(row.run_url, quote=True)}">{html.escape(row.run_id or "run")}</a>'
        else:
            link = ""
        cells.append(
            "<tr>"
            f"<td>{html.escape(row.suite)}</td>"
            f"<td>{html.escape(row.os_name)}</td>"
            f'<td class="{cls}">{html.escape(row.conclusion)}</td>'
            f"<td><code>{html.escape(row.sha)}</code></td>"
            f"<td>{html.escape(row.when_utc)}</td>"
            f"<td>{link}</td>"
            "</tr>"
        )
    body = "\n".join(cells)
    title = f"{repo} CI status"
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(title)}</title>
<style>
body {{ font-family: sans-serif; margin: 1.2rem; }}
table {{ border-collapse: collapse; }}
th, td {{ border: 1px solid #444; padding: 0.35rem 0.55rem; text-align: left; }}
th {{ background: #eee; }}
.ok {{ background: #c6f3c6; }}
.bad {{ background: #f5c2c2; }}
.cancelled {{ background: #ddd; }}
.progress {{ background: #ffe08a; }}
.other {{ background: #f3f3f3; }}
code {{ font-size: 0.95em; }}
p {{ max-width: 52rem; }}
</style>
</head>
<body>
<h1>{html.escape(title)}</h1>
<p>Latest Actions jobs Keith cares about. Generated {html.escape(generated_at)} from the GitHub Actions API (not keithcu.com). Times are UTC.</p>
<table>
<thead>
<tr><th>Suite</th><th>OS</th><th>Conclusion</th><th>SHA</th><th>When (UTC)</th><th>Run</th></tr>
</thead>
<tbody>
{body}
</tbody>
</table>
</body>
</html>
"""


def write_site(out_dir: str, page: str) -> None:
    os.makedirs(out_dir, exist_ok=True)
    index = os.path.join(out_dir, "index.html")
    with open(index, "w", encoding="utf-8") as handle:
        handle.write(page)
    # Pages will try Jekyll unless this exists; we ship raw HTML.
    with open(os.path.join(out_dir, ".nojekyll"), "w", encoding="utf-8") as handle:
        handle.write("")


def build_page(repo: str, token: str | None) -> str:
    def get_json(url: str) -> Any:
        return github_get_json(url, token)

    rows = collect_rows(
        lambda workflow: iter_workflow_runs(repo, workflow, get_json),
        lambda run_id: list_run_jobs(repo, run_id, get_json),
    )
    generated = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    return render_html(rows, repo=repo, generated_at=generated)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Generate the Actions CI status HTML table.")
    parser.add_argument("--out", default="_site", help="Directory for index.html (default: _site)")
    parser.add_argument(
        "--repo",
        default=os.environ.get("GITHUB_REPOSITORY") or DEFAULT_REPO,
        help="owner/repo (default: GITHUB_REPOSITORY or KeithCu/writeragent)",
    )
    args = parser.parse_args(argv)
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN") or None
    page = build_page(args.repo, token)
    write_site(args.out, page)
    print(f"wrote {os.path.join(args.out, 'index.html')} for {args.repo} (token={'yes' if token else 'no'})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
