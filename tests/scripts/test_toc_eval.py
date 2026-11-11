# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Unit tests for TOC task scoring and oracles (no soffice)."""
from __future__ import annotations

import json
from pathlib import Path
import sys
import zipfile

_PO = Path(__file__).resolve().parents[2] / "scripts" / "prompt_optimization"
if str(_PO) not in sys.path:
    sys.path.insert(0, str(_PO))

from toc_fixture import EDIT_INSERT, FIXTURE_ROWS, expected_rows_after_edit  # noqa: E402
from toc_eval import (  # noqa: E402
    TOC_EDIT_TASK_ID,
    TOC_LIST_TASK_ID,
    score_toc_example,
)


def _good_rows() -> list[tuple[str, str]]:
    expected_titles = expected_rows_after_edit()
    rows: list[tuple[str, str]] = []
    # Chapters 1..3 untouched (0..14)
    for i in range(15):
        rows.append((FIXTURE_ROWS[i].title, FIXTURE_ROWS[i].url))
    # Inserted row (15)
    rows.append((EDIT_INSERT[0], ""))
    # Old Chapter 4 renumbered to Chapter 5 (16..20)
    for j in range(5):
        orig = FIXTURE_ROWS[15 + j]
        new_title = expected_titles[16 + j]
        rows.append((new_title, orig.url.replace(orig.title, new_title)))
    # Old Chapter 5 renumbered to Chapter 6 (21..25)
    for j in range(5):
        orig = FIXTURE_ROWS[20 + j]
        new_title = expected_titles[21 + j]
        rows.append((new_title, orig.url.replace(orig.title, new_title)))
    # Chapters 6..8 untouched (26..40)
    for j in range(15):
        orig = FIXTURE_ROWS[25 + j]
        rows.append((orig.title, orig.url))
    return rows


def _odt(path: Path, *, rows: list[tuple[str, str]] | None = None, bookmark_count: int = 40) -> Path:
    if rows is None:
        rows = _good_rows()
    bookmarks = "".join(
        f'<text:bookmark-start text:name="__RefHeading___Toc{idx}_1234"/>'
        f'<text:bookmark-end text:name="__RefHeading___Toc{idx}_1234"/>'
        for idx in range(bookmark_count)
    )
    paras = ['<text:p text:style-name="Contents_20_Heading">Contents</text:p>']
    for title, href in rows:
        link = f'<text:a xlink:type="simple" xlink:href="{href}">{title}<text:tab/>1</text:a>' if href else f"{title}<text:tab/>1"
        paras.append(f'<text:p text:style-name="Contents_20_1">{link}</text:p>')
    body = "".join(paras)
    content = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<office:document-content xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0" '
        'xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0" '
        'xmlns:xlink="http://www.w3.org/1999/xlink" office:version="1.2">\n'
        f"<office:body><office:text>{bookmarks}<text:table-of-content>"
        f"<text:index-body>{body}</text:index-body>"
        "</text:table-of-content></office:text></office:body>\n"
        "</office:document-content>\n"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr(
            "mimetype",
            "application/vnd.oasis.opendocument.text",
            compress_type=zipfile.ZIP_STORED,
        )
        zf.writestr("content.xml", content)
    return path


def _good_edit_trace() -> list[dict[str, str]]:
    return [
        {
            "name": "delegate_to_specialized_writer_toolset",
            "arguments": json.dumps({"domain": "indexes", "task": "renumber"}),
        },
        {
            "name": "indexes_refresh_toc_entry",
            "arguments": json.dumps({"old_content": "Chapter 5 Title", "content": "Chapter 6 Title"}),
        },
        {
            "name": "indexes_insert_toc_entry",
            "arguments": json.dumps({
                "content": "Chapter 4 Interim Review",
                "position": "after",
                "old_content": "Section 3.4 Topic",
            }),
        },
        {
            "name": "specialized_workflow_finished",
            "arguments": json.dumps({"answer": "renumbered and inserted"}),
        },
    ]


def test_string_backend_fails_closed() -> None:
    score = score_toc_example(TOC_LIST_TASK_ID, [], backend="string")
    assert score.passed is False
    assert score.partial_score == 0.0
    assert any("string simulator" in f for f in score.failures)


def test_good_edit_odt_and_clean_trace_passes(tmp_path: Path) -> None:
    odt = _odt(tmp_path / "toc.odt")
    scored = score_toc_example(TOC_EDIT_TASK_ID, _good_edit_trace(), backend="lo", odt_path=str(odt))
    assert scored.passed is True
    assert scored.failures == []
    assert scored.partial_score == 1.0


def test_renamed_row_href_stale_fails(tmp_path: Path) -> None:
    rows = _good_rows()
    # Row 16 was renamed to Chapter 5 Title, but href still says Chapter 4 Title
    rows[16] = (rows[16][0], "#4.Chapter 4 Title|outline")
    odt = _odt(tmp_path / "toc_stale.odt", rows=rows)
    scored = score_toc_example(TOC_EDIT_TASK_ID, _good_edit_trace(), backend="lo", odt_path=str(odt))
    assert scored.passed is False
    assert any("differs from" in f for f in scored.failures)


def test_href_with_inserted_quote_fails(tmp_path: Path) -> None:
    rows = _good_rows()
    rows[16] = (rows[16][0], '#4.Chapter "5" Title|outline')
    odt = _odt(tmp_path / "toc_quote.odt", rows=rows)
    scored = score_toc_example(TOC_EDIT_TASK_ID, _good_edit_trace(), backend="lo", odt_path=str(odt))
    assert scored.passed is False
    assert any("differs from" in f for f in scored.failures)


def test_indexes_update_all_in_trace_fails(tmp_path: Path) -> None:
    odt = _odt(tmp_path / "toc.odt")
    trace = _good_edit_trace() + [{"name": "indexes_update_all", "arguments": "{}"}]
    scored = score_toc_example(TOC_EDIT_TASK_ID, trace, backend="lo", odt_path=str(odt))
    assert scored.passed is False
    assert any("indexes_update_all" in f for f in scored.failures)


def test_missing_heading_bookmarks_fails(tmp_path: Path) -> None:
    odt = _odt(tmp_path / "toc_bm.odt", bookmark_count=39)
    scored = score_toc_example(TOC_EDIT_TASK_ID, _good_edit_trace(), backend="lo", odt_path=str(odt))
    assert scored.passed is False
    assert any("heading bookmarks" in f for f in scored.failures)


def test_read_task_all_titles_passes() -> None:
    answer = "\n".join(f"{r.title} (level {r.level})" for r in FIXTURE_ROWS)
    trace = [
        {
            "name": "delegate_to_specialized_writer_toolset",
            "arguments": json.dumps({"domain": "indexes", "task": "list"}),
        },
        {
            "name": "indexes_list_toc_entries",
            "arguments": "{}",
        },
        {
            "name": "specialized_workflow_finished",
            "arguments": json.dumps({"answer": answer}),
        },
    ]
    scored = score_toc_example(TOC_LIST_TASK_ID, trace, backend="lo")
    assert scored.passed is True
    assert scored.failures == []
    assert scored.partial_score == 1.0
    assert scored.soft == []


def test_read_task_corrupted_link_fails() -> None:
    answer = "\n".join(f"{r.title} (level {r.level})" for r in FIXTURE_ROWS)
    corrupted = f'{answer}\nLink: #4.Chapter "4" Title|outline'
    trace = [
        {
            "name": "delegate_to_specialized_writer_toolset",
            "arguments": json.dumps({"domain": "indexes", "task": "list"}),
        },
        {
            "name": "indexes_list_toc_entries",
            "arguments": "{}",
        },
        {
            "name": "specialized_workflow_finished",
            "arguments": json.dumps({"answer": corrupted}),
        },
    ]
    scored = score_toc_example(TOC_LIST_TASK_ID, trace, backend="lo")
    assert scored.passed is False
    assert any("Corrupted" in f for f in scored.failures)


def test_read_task_exact_fixture_link_soft_note() -> None:
    answer = "\n".join(f"{r.title} (level {r.level})" for r in FIXTURE_ROWS)
    linked = f"{answer}\nLink: #1.Chapter 1 Title|outline"
    trace = [
        {
            "name": "delegate_to_specialized_writer_toolset",
            "arguments": json.dumps({"domain": "indexes", "task": "list"}),
        },
        {
            "name": "indexes_list_toc_entries",
            "arguments": "{}",
        },
        {
            "name": "specialized_workflow_finished",
            "arguments": json.dumps({"answer": linked}),
        },
    ]
    scored = score_toc_example(TOC_LIST_TASK_ID, trace, backend="lo")
    assert scored.passed is True
    assert scored.failures == []
    assert any("reported outline hyperlinks" in s for s in scored.soft)


def test_read_task_missing_list_tool_fails() -> None:
    answer = "\n".join(f"{r.title} (level {r.level})" for r in FIXTURE_ROWS)
    trace = [
        {
            "name": "delegate_to_specialized_writer_toolset",
            "arguments": json.dumps({"domain": "indexes", "task": "list"}),
        },
        {
            "name": "specialized_workflow_finished",
            "arguments": json.dumps({"answer": answer}),
        },
    ]
    scored = score_toc_example(TOC_LIST_TASK_ID, trace, backend="lo")
    assert scored.passed is False
    assert any("indexes_list_toc_entries" in f for f in scored.failures)
