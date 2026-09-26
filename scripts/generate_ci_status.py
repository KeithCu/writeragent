#!/usr/bin/env python3
# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Build a static CI status table from the GitHub Actions API.

Used by ``.github/workflows/ci-status-pages.yml`` to publish
https://keithcu.github.io/writeragent/ (``index.html`` + ``status.svg``).
Auth is ``GITHUB_TOKEN`` only (optional for this public repo). The token
is never written into HTML or SVG. The SVG is a drawn table (not a
screenshot) so the repo README can embed it as an image. ``github_get``
retries transient GitHub API 429/502/503/504 and transport errors so a
one-off gateway flake does not fail Status Pages.
"""

from __future__ import annotations

import argparse
import html
import json
import os
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterator
from urllib.parse import quote

DEFAULT_REPO = "KeithCu/writeragent"
API_ROOT = "https://api.github.com"
USER_AGENT = "writeragent-ci-status"
API_VERSION = "2022-11-28"
RUNS_PER_PAGE = 100
MAX_RUN_PAGES = 50
DEFAULT_MAX_AGE_DAYS = 60
OS_LABELS = ("ubuntu-latest", "macos-latest", "windows-latest")

# Status Pages failed master on a single GitHub 502 ("Server Error") while
# listing run jobs. github_get used to raise on the first HTTPError, so a
# gateway flake reded the branch even though product CI was fine. Retry
# only these statuses (plus URLError/timeout); 401/403/404 stay fail-fast.
TRANSIENT_HTTP_STATUSES = frozenset({429, 502, 503, 504})
GITHUB_GET_ATTEMPTS = 4
GITHUB_GET_BACKOFF_SEC = 2.0
GITHUB_GET_MAX_SLEEP_SEC = 30.0

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


# Conclusions emitted when an Actions run/job has definitively completed.
# Non-terminal statuses (e.g. in_progress, queued, waiting) or "no run" must
# NEVER be cached as checkpoints, otherwise a job inspected while running will
# permanently lock in "in_progress" (yellow) instead of resolving to "success".
# Cancelled jobs are intentionally excluded: they are ignored during collection
# so prior completed (e.g. green) runs are surfaced, and caching a cancelled
# row would prematurely stop inspection of older runs.
TERMINAL_CONCLUSIONS: frozenset[str] = frozenset({
    "success",
    "failure",
    "timed_out",
    "skipped",
    "neutral",
    "action_required",
})


@dataclass(frozen=True)
class StatusRow:
    suite: str
    os: str
    conclusion: str
    sha: str
    when: str
    run_url: str
    run_id: int = 0
    run_attempt: int = 1


def suite_specs() -> tuple[SuiteSpec, ...]:
    return tuple(SuiteSpec(*item) for item in SUITE_SPECS)


def parse_iso_timestamp(ts: str) -> datetime | None:
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except (ValueError, TypeError):
        return None


def is_expired(
    ts_str: str,
    max_age_days: int = DEFAULT_MAX_AGE_DAYS,
    now: datetime | None = None,
) -> bool:
    """True when timestamp is older than max_age_days (2 months by default)."""
    if not ts_str:
        return True
    dt = parse_iso_timestamp(ts_str)
    if dt is None:
        return False
    if now is None:
        now = datetime.now(timezone.utc)
    return (now - dt) > timedelta(days=max_age_days)


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


def parse_retry_after_seconds(value: str | None) -> float | None:
    """Parse ``Retry-After`` as delta-seconds. Invalid or missing -> None.

    GitHub sends integer seconds on 429. HTTP-date values are ignored so
    this stays stdlib-simple; those fall back to exponential backoff.
    """
    if not isinstance(value, str) or not value:
        return None
    try:
        seconds = float(value.strip())
    except ValueError:
        return None
    if seconds < 0:
        return None
    return min(seconds, GITHUB_GET_MAX_SLEEP_SEC)


def github_retry_delay(
    status: int | None,
    headers: Any,
    attempt: int,
) -> float:
    """Sleep before the next ``github_get`` attempt (1-based failed attempt)."""
    if status == 429 and headers is not None:
        getter = getattr(headers, "get", None)
        raw_retry_after = getter("Retry-After") if callable(getter) else None
        retry_after = parse_retry_after_seconds(
            raw_retry_after if isinstance(raw_retry_after, str) else None
        )
        if retry_after is not None:
            return retry_after
    exponent = max(attempt, 1) - 1
    return min(GITHUB_GET_BACKOFF_SEC * (2 ** exponent), GITHUB_GET_MAX_SLEEP_SEC)


def github_get(
    url: str,
    token: str,
    *,
    sleep: Callable[[float], None] = time.sleep,
) -> JsonDict:
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": USER_AGENT,
        "X-GitHub-Api-Version": API_VERSION,
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, headers=headers)
    for attempt in range(1, GITHUB_GET_ATTEMPTS + 1):
        try:
            with urllib.request.urlopen(request, timeout=30) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            error = RuntimeError(f"GitHub API {exc.code} for {url}: {body[:300]}")
            if exc.code not in TRANSIENT_HTTP_STATUSES or attempt >= GITHUB_GET_ATTEMPTS:
                raise error from exc
            delay = github_retry_delay(exc.code, exc.headers, attempt)
            print(
                f"Retrying GitHub API {exc.code} for {url} in {delay:g}s "
                f"(attempt {attempt}/{GITHUB_GET_ATTEMPTS})",
                file=sys.stderr,
            )
            sleep(delay)
            continue
        except (urllib.error.URLError, TimeoutError) as exc:
            error = RuntimeError(f"GitHub API request failed for {url}: {exc}")
            if attempt >= GITHUB_GET_ATTEMPTS:
                raise error from exc
            delay = github_retry_delay(None, None, attempt)
            print(
                f"Retrying GitHub API request for {url} in {delay:g}s "
                f"(attempt {attempt}/{GITHUB_GET_ATTEMPTS}): {exc}",
                file=sys.stderr,
            )
            sleep(delay)
            continue
        parsed: Any = json.loads(raw.decode("utf-8"))
        if not isinstance(parsed, dict):
            raise RuntimeError(f"GitHub API returned a non-object for {url}")
        return parsed
    raise RuntimeError(f"GitHub API request failed for {url}")


def make_fetcher(token: str) -> Fetcher:
    def fetch(url: str) -> JsonDict:
        return github_get(url, token)

    return fetch


def iter_workflow_runs(
    fetch: Fetcher,
    repo: str,
    workflow: str,
    *,
    max_pages: int = MAX_RUN_PAGES,
    max_age_days: int = DEFAULT_MAX_AGE_DAYS,
    now: datetime | None = None,
) -> Iterator[JsonDict]:
    encoded = quote(workflow, safe="")
    for page in range(1, max_pages + 1):
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
                created = run.get("created_at")
                if isinstance(created, str) and is_expired(created, max_age_days, now):
                    return
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
        run_id=0,
        run_attempt=1,
    )


def parse_cached_rows(
    cached_data: JsonDict,
    specs: tuple[SuiteSpec, ...],
    max_age_days: int = DEFAULT_MAX_AGE_DAYS,
    now: datetime | None = None,
) -> dict[int, StatusRow]:
    raw_rows = cached_data.get("rows")
    if not isinstance(raw_rows, list):
        return {}
    cached_by_key: dict[tuple[str, str], StatusRow] = {}
    for item in raw_rows:
        if not isinstance(item, dict):
            continue
        suite = str(item.get("suite", ""))
        os_name = str(item.get("os", ""))
        conclusion = str(item.get("conclusion", ""))
        when = str(item.get("when", ""))
        run_id = int(item.get("run_id", 0) or 0)
        run_attempt = int(item.get("run_attempt", 1) or 1)
        # Only accept completed/terminal runs as cached hints; in-progress or
        # non-terminal rows must be re-evaluated against the live Actions API.
        # Expired cached runs revert to "no run" and are not accepted as hints.
        # Bugfix: Previously, cancelled jobs were treated as valid terminal conclusions,
        # caching a cancelled state that prevented inspecting older runs. We explicitly
        # ensure cancelled conclusions are never accepted as cached checkpoint hints.
        if (
            conclusion not in TERMINAL_CONCLUSIONS
            or conclusion == "cancelled"
            or is_expired(when, max_age_days, now)
        ):
            continue
        cached_by_key[(suite, os_name)] = StatusRow(
            suite=suite,
            os=os_name,
            conclusion=conclusion,
            sha=str(item.get("sha", "")),
            when=when,
            run_url=str(item.get("run_url", "")),
            run_id=run_id,
            run_attempt=run_attempt,
        )

    result: dict[int, StatusRow] = {}
    for index, spec in enumerate(specs):
        key = (spec.suite, spec.os)
        if key in cached_by_key:
            result[index] = cached_by_key[key]
    return result


def fetch_remote_cache(repo: str) -> JsonDict | None:
    """Try fetching the previous status.json published to GitHub Pages."""
    if not repo or "/" not in repo:
        return None
    owner, repo_name = repo.split("/", 1)
    url = f"https://{owner}.github.io/{repo_name}/status.json"
    headers = {"User-Agent": USER_AGENT}
    request = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=10) as resp:
            raw = resp.read()
            parsed = json.loads(raw.decode("utf-8"))
            if isinstance(parsed, dict) and "rows" in parsed:
                return parsed
    except Exception:
        return None
    return None


def load_cache(
    cache_path: str = "",
    repo: str = "",
) -> JsonDict | None:
    if cache_path:
        p = Path(cache_path)
        if p.is_file():
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
                if isinstance(data, dict) and "rows" in data:
                    return data
            except Exception:
                pass
    return fetch_remote_cache(repo)


def collect_status(
    fetch: Fetcher,
    repo: str,
    *,
    cached_data: JsonDict | None = None,
    max_age_days: int = DEFAULT_MAX_AGE_DAYS,
    now: datetime | None = None,
) -> list[StatusRow]:
    """Newest matching job per suite row within max_age_days.

    If cached_data is provided, cached rows within max_age_days serve as
    checkpoints: we only scan runs newer than the cached run ID / timestamp,
    avoiding re-fetching hundreds of historical runs.
    """
    specs = suite_specs()
    cached_rows = parse_cached_rows(cached_data, specs, max_age_days, now) if cached_data else {}
    found: dict[int, StatusRow] = dict(cached_rows)

    pending_by_workflow: dict[str, list[int]] = {}
    for index, spec in enumerate(specs):
        pending_by_workflow.setdefault(spec.workflow, []).append(index)

    for workflow, indexes in pending_by_workflow.items():
        pending = set(indexes)
        for run in iter_workflow_runs(fetch, repo, workflow, max_age_days=max_age_days, now=now):
            if not pending:
                break
            run_id = run.get("id")
            if not isinstance(run_id, int):
                continue
            run_attempt = int(run.get("run_attempt", 1) or 1)
            run_url = run.get("html_url") if isinstance(run.get("html_url"), str) else ""
            sha = short_sha(run.get("head_sha") if isinstance(run.get("head_sha"), str) else "")
            created_dt = parse_iso_timestamp(str(run.get("created_at", "")))

            # Checkpoints from cached hints:
            # If we reach or pass the run_id of a cached hint, no newer run exists
            # for that spec. We retain the cached hint and discard from pending.
            # If the run was re-run (higher attempt), we must inspect its jobs again.
            for index in list(pending):
                if index in cached_rows:
                    cached_row = cached_rows[index]
                    if cached_row.run_id and run_id <= cached_row.run_id:
                        if run_id == cached_row.run_id and run_attempt > cached_row.run_attempt:
                            pass
                        else:
                            pending.discard(index)
                    elif not cached_row.run_id and cached_row.when and created_dt:
                        cached_dt = parse_iso_timestamp(cached_row.when)
                        if cached_dt and created_dt <= cached_dt:
                            pending.discard(index)

            if not pending:
                break

            # Optimization for pr-ci.yml: pull_request events ONLY matrix ubuntu-latest
            # and test_mock_sidebar=false. If Test & Typecheck (ubuntu-latest) is not
            # pending, no job in this pull_request run can match any pending spec.
            if workflow == "pr-ci.yml" and run.get("event") == "pull_request":
                ubuntu_pending = any(
                    specs[i].suite == "Test & Typecheck" and specs[i].os == "ubuntu-latest"
                    for i in pending
                )
                if not ubuntu_pending:
                    continue

            jobs = list_run_jobs(fetch, repo, run_id)
            for job in jobs:
                name = job.get("name") if isinstance(job.get("name"), str) else ""
                when = job_when(job)
                if is_expired(when, max_age_days, now):
                    continue
                # Bugfix: Cancelled jobs previously matched pending specs and were recorded
                # as conclusion="cancelled", discarding the spec from pending. When a green run
                # was followed by a cancelled run, the status page showed "cancelled" instead
                # of the green run. Skipping cancelled jobs keeps the spec in pending so
                # older completed (e.g. green) runs are discovered instead.
                conclusion = job_conclusion(job)
                if conclusion == "cancelled":
                    continue
                still_pending = list(pending)
                for index in still_pending:
                    spec = specs[index]
                    if not job_matches(name, spec):
                        continue
                    found[index] = StatusRow(
                        suite=spec.suite,
                        os=spec.os or extract_os(name),
                        conclusion=conclusion,
                        sha=sha,
                        when=when,
                        run_url=run_url or "",
                        run_id=run_id,
                        run_attempt=run_attempt,
                    )
                    pending.discard(index)

    return [found.get(index, _empty_row(spec)) for index, spec in enumerate(specs)]


def _cell(value: str) -> str:
    return html.escape(value, quote=True)


# Drawn table for README ``<img>`` / Camo. No Run column: markdown images
# are not clickable, and the HTML page already has the links.
_SVG_COLS: tuple[tuple[str, int], ...] = (
    ("Suite", 168),
    ("OS", 118),
    ("Conclusion", 100),
    ("SHA", 72),
    ("When", 172),
)
_SVG_PAD_X = 12
_SVG_PAD_Y = 10
_SVG_TITLE_H = 20
_SVG_SUB_H = 16
_SVG_GAP = 8
_SVG_ROW_H = 22
# class, light fill, light text — CSS overrides fills in dark mode.
_CONCLUSION_STYLE: dict[str, tuple[str, str, str]] = {
    "success": ("ok", "#cfc", "#1a7f37"),
    "failure": ("fail", "#fcc", "#cf222e"),
    "cancelled": ("cancel", "#eee", "#6e7781"),
    "in_progress": ("prog", "#ffc", "#9a6700"),
    "in-progress": ("prog", "#ffc", "#9a6700"),
    "no run": ("norun", "#f6f8fa", "#6e7781"),
}
_DEFAULT_CONCLUSION_STYLE = ("unk", "#fff", "#111")


def _svg_table_size(row_count: int) -> tuple[int, int, int]:
    table_w = sum(width for _label, width in _SVG_COLS)
    table_top = _SVG_PAD_Y + _SVG_TITLE_H + _SVG_SUB_H + _SVG_GAP
    height = table_top + _SVG_ROW_H * (1 + row_count) + _SVG_PAD_Y
    return table_w, table_top, height


def _conclusion_style(conclusion: str) -> tuple[str, str, str]:
    return _CONCLUSION_STYLE.get(conclusion, _DEFAULT_CONCLUSION_STYLE)


def render_svg(
    rows: list[StatusRow],
    *,
    repo: str,
    generated_at: str,
) -> str:
    """Same suite/OS/conclusion/SHA/when table as HTML, as an SVG image."""
    table_w, table_top, height = _svg_table_size(len(rows))
    width = table_w + 2 * _SVG_PAD_X
    parts: list[str] = [
        '<?xml version="1.0" encoding="UTF-8"?>\n',
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" '
        f'height="{height}" viewBox="0 0 {width} {height}" role="img">\n',
        "<title>WriterAgent CI status</title>\n",
        "<style>\n",
        "text { font-family: sans-serif; font-size: 12px; }\n",
        "text.mono { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; }\n",
        "@media (prefers-color-scheme: dark) {\n",
        "  .bg { fill: #0d1117; }\n",
        "  .grid { stroke: #8b949e; }\n",
        "  .ink { fill: #e6edf3; }\n",
        "  .hdr { fill: #161b22; }\n",
        "  .ok { fill: #1a3d2a; } .okt { fill: #3fb950; }\n",
        "  .fail { fill: #3d1a1a; } .failt { fill: #f85149; }\n",
        "  .cancel { fill: #21262d; } .cancelt { fill: #8b949e; }\n",
        "  .prog { fill: #3d2f0a; } .progt { fill: #d29922; }\n",
        "  .norun { fill: #161b22; } .norunt { fill: #8b949e; }\n",
        "  .unk { fill: #0d1117; } .unkt { fill: #e6edf3; }\n",
        "}\n",
        "</style>\n",
        f'<rect class="bg" width="{width}" height="{height}" fill="#fff"/>\n',
        f'<text class="ink" x="{_SVG_PAD_X}" y="{_SVG_PAD_Y + 14}" '
        f'fill="#111" font-weight="bold">WriterAgent CI status</text>\n',
        f'<text class="ink" x="{_SVG_PAD_X}" y="{_SVG_PAD_Y + _SVG_TITLE_H + 12}" '
        f'fill="#333" font-size="11">{_cell(repo)} · {_cell(generated_at)}</text>\n',
    ]

    def _x_at(col: int) -> int:
        return _SVG_PAD_X + sum(width for _label, width in _SVG_COLS[:col])

    def _row_rect(y: int, fill: str, css: str) -> str:
        return (
            f'<rect class="{css}" x="{_SVG_PAD_X}" y="{y}" '
            f'width="{table_w}" height="{_SVG_ROW_H}" fill="{fill}"/>\n'
        )

    def _grid(y: int) -> str:
        lines = [
            f'<rect class="grid" x="{_SVG_PAD_X}" y="{y}" width="{table_w}" '
            f'height="{_SVG_ROW_H}" fill="none" stroke="#333"/>\n'
        ]
        for col in range(1, len(_SVG_COLS)):
            x = _x_at(col)
            lines.append(
                f'<line class="grid" x1="{x}" y1="{y}" x2="{x}" '
                f'y2="{y + _SVG_ROW_H}" stroke="#333"/>\n'
            )
        return "".join(lines)

    def _cell_text(col: int, y: int, value: str, *, css: str = "ink", fill: str = "#111",
                   mono: bool = False) -> str:
        text_y = y + _SVG_ROW_H - 6
        extra = ' class="mono ink"' if mono else f' class="{css}"'
        return (
            f"<text{extra} x=\"{_x_at(col) + 6}\" y=\"{text_y}\" "
            f'fill="{fill}">{_cell(value)}</text>\n'
        )

    header_y = table_top
    parts.append(_row_rect(header_y, "#f6f8fa", "hdr"))
    parts.append(_grid(header_y))
    for col, (label, _width) in enumerate(_SVG_COLS):
        parts.append(_cell_text(col, header_y, label, fill="#111"))

    for index, row in enumerate(rows):
        y = table_top + _SVG_ROW_H * (index + 1)
        css, bg, ink = _conclusion_style(row.conclusion)
        parts.append(_row_rect(y, "#fff", "bg"))
        # Conclusion tint only on that cell so the row stays readable.
        parts.append(
            f'<rect class="{css}" x="{_x_at(2)}" y="{y}" '
            f'width="{_SVG_COLS[2][1]}" height="{_SVG_ROW_H}" fill="{bg}"/>\n'
        )
        parts.append(_grid(y))
        parts.append(_cell_text(0, y, row.suite))
        parts.append(_cell_text(1, y, row.os))
        parts.append(_cell_text(2, y, row.conclusion, css=f"{css}t", fill=ink))
        parts.append(_cell_text(3, y, row.sha, mono=True))
        parts.append(_cell_text(4, y, row.when))

    parts.append("</svg>\n")
    return "".join(parts)


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


def render_json(
    rows: list[StatusRow],
    *,
    repo: str,
    generated_at: str,
    max_age_days: int = DEFAULT_MAX_AGE_DAYS,
) -> str:
    """JSON output containing all row fields and run_ids to serve as hints for future runs."""
    payload = {
        "repo": repo,
        "generated_at": generated_at,
        "max_age_days": max_age_days,
        "rows": [
            {
                "suite": r.suite,
                "os": r.os,
                "conclusion": r.conclusion,
                "sha": r.sha,
                "when": r.when,
                "run_url": r.run_url,
                "run_id": r.run_id,
                "run_attempt": r.run_attempt,
            }
            for r in rows
        ],
    }
    return json.dumps(payload, indent=2) + "\n"


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def write_site(
    out_dir: Path,
    html_text: str,
    svg_text: str,
    json_text: str = "",
    cache_file: Path | None = None,
) -> tuple[Path, Path, Path | None]:
    out_dir.mkdir(parents=True, exist_ok=True)
    index = out_dir / "index.html"
    svg = out_dir / "status.svg"
    status_json = out_dir / "status.json"
    index.write_text(html_text, encoding="utf-8")
    svg.write_text(svg_text, encoding="utf-8")
    json_path: Path | None = None
    if json_text:
        status_json.write_text(json_text, encoding="utf-8")
        json_path = status_json
    if cache_file is not None and json_text:
        cache_file.parent.mkdir(parents=True, exist_ok=True)
        cache_file.write_text(json_text, encoding="utf-8")
    return index, svg, json_path


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--out",
        default="_site",
        help="Directory to write index.html, status.svg, and status.json into (default: _site)",
    )
    parser.add_argument(
        "--repo",
        default="",
        help="owner/name (default: GITHUB_REPOSITORY or KeithCu/writeragent)",
    )
    parser.add_argument(
        "--cache",
        default="",
        help="Path to previous status.json cache file to load from and save to",
    )
    parser.add_argument(
        "--max-age-days",
        type=int,
        default=DEFAULT_MAX_AGE_DAYS,
        help=f"Maximum age in days before a test run expires (default: {DEFAULT_MAX_AGE_DAYS})",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    repo = (args.repo or os.environ.get("GITHUB_REPOSITORY") or DEFAULT_REPO).strip()
    token = os.environ.get("GITHUB_TOKEN", "")
    cached_data = load_cache(cache_path=args.cache, repo=repo)
    rows = collect_status(
        make_fetcher(token),
        repo,
        cached_data=cached_data,
        max_age_days=args.max_age_days,
    )
    generated_at = utc_now()
    page = render_html(rows, repo=repo, generated_at=generated_at)
    svg = render_svg(rows, repo=repo, generated_at=generated_at)
    json_text = render_json(
        rows,
        repo=repo,
        generated_at=generated_at,
        max_age_days=args.max_age_days,
    )
    if token and (token in page or token in svg or token in json_text):
        raise RuntimeError("refusing to write output that contains GITHUB_TOKEN")
    cache_file = Path(args.cache) if args.cache else None
    index, svg_path, json_path = write_site(
        Path(args.out), page, svg, json_text, cache_file=cache_file
    )
    status_msg = f"Wrote {index}, {svg_path}"
    if json_path:
        status_msg += f", and {json_path}"
    print(f"{status_msg} ({len(rows)} rows)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
