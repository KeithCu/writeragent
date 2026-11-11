# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Score eval-1 TOC tasks (toc_list_entries and toc_renumber_insert).

Evaluates late binding and outline hyperlink preservation in Writer TOCs.
The list task scores from the tool-call trace; the edit task scores from the
exported .odt index-body rows and preserved outline bookmarks.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
import re
import sys
from typing import Any
import zipfile

_PO = Path(__file__).resolve().parent
if str(_PO) not in sys.path:
    sys.path.insert(0, str(_PO))

from toc_fixture import FIXTURE_ROWS, expected_rows_after_edit

TOC_LIST_TASK_ID = "toc_list_entries"
TOC_EDIT_TASK_ID = "toc_renumber_insert"
TOC_TASK_IDS = frozenset({TOC_LIST_TASK_ID, TOC_EDIT_TASK_ID})

_STRING_FAILURE = (
    "TOC benchmark tasks require the headless LO backend; "
    "the string simulator cannot run these tasks"
)


@dataclass
class TocScore:
    failures: list[str] = field(default_factory=list)
    soft: list[str] = field(default_factory=list)
    partial_score: float = 0.0
    summary: str = ""
    passed: bool = False


def links_in_answer(text: str) -> list[str]:
    """Find all #...|outline hyperlink tokens in text."""
    if not text:
        return []
    return re.findall(r"#[^\n\r`<>]+?\|outline", text)


def finish_answer_from_trace(trace: list[dict[str, Any]] | None) -> str:
    """Extract answer from the last specialized_workflow_finished call in trace."""
    if not trace:
        return ""
    for entry in reversed(trace):
        if entry.get("name") == "specialized_workflow_finished":
            raw_args = entry.get("arguments")
            if isinstance(raw_args, str):
                try:
                    args = json.loads(raw_args)
                except Exception:
                    args = {}
            elif isinstance(raw_args, dict):
                args = raw_args
            else:
                args = {}
            return str(args.get("answer") or "")
    return ""


def read_toc_rows_from_odt(path: Path | str) -> list[tuple[str, str]]:
    """Parse visible titles and hyperlink targets from content.xml index-body."""
    with zipfile.ZipFile(path) as zf:
        xml = zf.read("content.xml").decode("utf-8")
    m = re.search(r"<text:index-body>(.*?)</text:index-body>", xml, re.DOTALL)
    if not m:
        return []
    body = m.group(1)
    paras = re.findall(r"<text:p\b[^>]*>(.*?)</text:p>", body, re.DOTALL)
    rows: list[tuple[str, str]] = []
    for p in paras:
        if "<text:tab" not in p and "\t" not in p:
            continue
        p_tab = re.sub(r"<text:tab\b[^>]*/>", "\t", p)
        text_before_tab = p_tab.split("\t")[0]
        title = re.sub(r"<[^>]+>", "", text_before_tab).strip()
        link_m = re.search(r'xlink:href="([^"]*)"', p)
        href = link_m.group(1) if link_m else ""
        if title:
            rows.append((title, href))
    return rows


def _score_toc_list(trace: list[dict[str, Any]] | None) -> TocScore:
    failures: list[str] = []
    soft: list[str] = []

    has_delegate = False
    has_list_tool = False
    include_links_requested = False

    for entry in trace or ():
        name = entry.get("name") or ""
        raw_args = entry.get("arguments") or ""
        if isinstance(raw_args, str):
            try:
                args = json.loads(raw_args)
            except Exception:
                args = {}
        elif isinstance(raw_args, dict):
            args = raw_args
        else:
            args = {}

        if name.startswith("delegate_to_specialized_"):
            domain = str(args.get("domain") or "")
            if "indexes" in domain:
                has_delegate = True
        if name == "indexes_list_toc_entries":
            has_list_tool = True
            if bool(args.get("include_links")):
                include_links_requested = True

    if not has_delegate:
        failures.append("No delegate_to_specialized_* call with domain 'indexes'")
    if not has_list_tool:
        failures.append("No indexes_list_toc_entries call in trace")

    answer = finish_answer_from_trace(trace)
    fixture_titles = [row.title for row in FIXTURE_ROWS]
    titles_found = sum(1 for title in fixture_titles if title in answer)
    partial = titles_found / len(fixture_titles) if fixture_titles else 0.0

    if titles_found < 36:
        failures.append(f"Only {titles_found} of 40 fixture titles reported in answer (need >= 36)")

    valid_urls = {row.url for row in FIXTURE_ROWS}
    found_links = links_in_answer(answer)
    for link in found_links:
        if link not in valid_urls:
            failures.append(f"Corrupted outline hyperlink reported: {link}")

    if found_links:
        soft.append(f"Answer reported outline hyperlinks ({len(found_links)} found)")
    if include_links_requested:
        soft.append("indexes_list_toc_entries called with include_links=True")

    passed = (len(failures) == 0)
    summary = f"toc_list_entries passed={passed} partial={partial:.3f} titles={titles_found}/40"
    return TocScore(
        failures=failures,
        soft=soft,
        partial_score=partial,
        summary=summary,
        passed=passed,
    )


def _score_toc_edit(
    trace: list[dict[str, Any]] | None,
    odt_path: str | None = None,
) -> TocScore:
    failures: list[str] = []
    soft: list[str] = []

    exported: str | None = odt_path
    cleanup = False
    if not exported:
        try:
            import tools_lo

            exported = tools_lo.export_writer_odt()
            cleanup = True
        except Exception as exc:
            failures.append(f"Failed to export writer ODT: {exc}")
            exported = None

    try:
        if exported and Path(exported).is_file():
            with zipfile.ZipFile(exported) as zf:
                xml = zf.read("content.xml").decode("utf-8")

            bookmark_count = len(re.findall(r'text:bookmark-start\s+[^>]*text:name="(__RefHeading__[^"]*)"', xml))
            if bookmark_count != 40:
                failures.append(f"Expected 40 heading bookmarks (__RefHeading__), got {bookmark_count}")

            rows = read_toc_rows_from_odt(exported)
            expected_titles = expected_rows_after_edit()
            actual_titles = [r[0] for r in rows]

            if actual_titles != expected_titles:
                failures.append(
                    f"Row sequence does not match expected_rows_after_edit() (got {len(rows)} rows, expected {len(expected_titles)})"
                )
            else:
                # Untouched chapters 1..3 (rows 0..14)
                for i in range(15):
                    if rows[i][1] != FIXTURE_ROWS[i].url:
                        failures.append(
                            f"Untouched row {FIXTURE_ROWS[i].title} href changed to {rows[i][1]!r} (expected {FIXTURE_ROWS[i].url!r})"
                        )

                # Inserted row (row 15)
                if not rows[15][1].endswith("|outline"):
                    soft.append("Inserted row has no |outline href")

                # Old Chapter 4 renumbered to Chapter 5 (rows 16..20)
                for j in range(5):
                    orig_row = FIXTURE_ROWS[15 + j]
                    new_title = rows[16 + j][0]
                    expected_href = orig_row.url.replace(orig_row.title, new_title)
                    if rows[16 + j][1] != expected_href:
                        failures.append(
                            f"Renamed row {new_title} href {rows[16 + j][1]!r} differs from {expected_href!r}"
                        )

                # Old Chapter 5 renumbered to Chapter 6 (rows 21..25)
                for j in range(5):
                    orig_row = FIXTURE_ROWS[20 + j]
                    new_title = rows[21 + j][0]
                    expected_href = orig_row.url.replace(orig_row.title, new_title)
                    if rows[21 + j][1] != expected_href:
                        failures.append(
                            f"Renamed row {new_title} href {rows[21 + j][1]!r} differs from {expected_href!r}"
                        )

                # Untouched chapters 6..8 (rows 26..40)
                for j in range(15):
                    orig_row = FIXTURE_ROWS[25 + j]
                    if rows[26 + j][1] != orig_row.url:
                        failures.append(
                            f"Untouched row {orig_row.title} href changed to {rows[26 + j][1]!r} (expected {orig_row.url!r})"
                        )
        elif not failures:
            failures.append("Exported ODT file missing or unreadable")
    finally:
        if cleanup and exported:
            try:
                Path(exported).unlink(missing_ok=True)
            except OSError:
                pass

    for entry in trace or ():
        name = entry.get("name") or ""
        raw_args = entry.get("arguments") or ""
        if isinstance(raw_args, str):
            try:
                args = json.loads(raw_args)
            except Exception:
                args = {}
        elif isinstance(raw_args, dict):
            args = raw_args
        else:
            args = {}

        if name == "indexes_update_all":
            failures.append("Trace contains indexes_update_all (TOC rebuild wiped custom edits)")
        if name == "indexes_refresh_toc_entry" and args.get("hyperlink_url") is not None:
            soft.append("indexes_refresh_toc_entry call passed hyperlink_url")

    passed = (len(failures) == 0)
    partial = 1.0 if passed else 0.0
    summary = f"toc_renumber_insert passed={passed} partial={partial:.3f}"
    return TocScore(
        failures=failures,
        soft=soft,
        partial_score=partial,
        summary=summary,
        passed=passed,
    )


def score_toc_example(
    task_id: str,
    trace: list[dict[str, Any]] | None,
    *,
    backend: str,
    odt_path: str | None = None,
) -> TocScore:
    """Score a TOC example from the trace or exported ODT."""
    if backend != "lo":
        return TocScore(
            failures=[_STRING_FAILURE],
            summary=_STRING_FAILURE,
            passed=False,
        )

    if task_id == TOC_LIST_TASK_ID:
        return _score_toc_list(trace)
    if task_id == TOC_EDIT_TASK_ID:
        return _score_toc_edit(trace, odt_path=odt_path)

    raise ValueError(f"Unknown TOC task id: {task_id!r}")
