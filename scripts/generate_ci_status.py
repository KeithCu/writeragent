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
screenshot) so the repo README can embed it as an image.
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


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def write_site(out_dir: Path, html_text: str, svg_text: str) -> tuple[Path, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    index = out_dir / "index.html"
    svg = out_dir / "status.svg"
    index.write_text(html_text, encoding="utf-8")
    svg.write_text(svg_text, encoding="utf-8")
    return index, svg


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--out",
        default="_site",
        help="Directory to write index.html and status.svg into (default: _site)",
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
    generated_at = utc_now()
    page = render_html(rows, repo=repo, generated_at=generated_at)
    svg = render_svg(rows, repo=repo, generated_at=generated_at)
    if token and (token in page or token in svg):
        raise RuntimeError("refusing to write output that contains GITHUB_TOKEN")
    index, svg_path = write_site(Path(args.out), page, svg)
    print(f"Wrote {index} and {svg_path} ({len(rows)} rows)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
