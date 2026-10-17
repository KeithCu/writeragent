# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
"""Unit tests for document_research grep (grep_nearby_files)."""

from __future__ import annotations

import os
from pathlib import Path
import tempfile
from unittest.mock import MagicMock, patch

from plugin.doc.document_research_grep import (
    _grep_text_in_calc,
    _grep_text_in_draw,
    _grep_text_in_writer,
    _search_opened_document,
    grep_nearby_files,
    resolve_grep_candidates,
)
from plugin.framework.url_utils import path_to_file_url


def test_resolve_grep_candidates_budget_filter():
    with tempfile.TemporaryDirectory() as tmp:
        budget = os.path.join(tmp, "Budget_2026.ods")
        notes = os.path.join(tmp, "Notes.odt")
        for path in (budget, notes):
            with open(path, "wb"):
                pass

        model = MagicMock()
        ctx = MagicMock()
        with patch("plugin.doc.document_research_grep.list_nearby_files") as mock_list:
            mock_list.return_value = {
                "status": "ok",
                "files": [
                    {"path": budget, "name": "Budget_2026.ods", "url": "file:///b", "modified": 1.0, "size_bytes": 0, "doc_type_guess": "calc", "is_open": False},
                    {"path": notes, "name": "Notes.odt", "url": "file:///n", "modified": 0.5, "size_bytes": 0, "doc_type_guess": "writer", "is_open": False},
                ],
                "truncated": False,
            }
            candidates, truncated, err = resolve_grep_candidates(ctx, model, file_subset="budget")

        assert err is None
        assert truncated is False
        assert len(candidates) == 2
        assert candidates[0]["name"] == "Budget_2026.ods"


def test_resolve_grep_candidates_single_absolute_path():
    with tempfile.NamedTemporaryFile(suffix=".ods", delete=False) as f:
        path = f.name
    try:
        candidates, truncated, err = resolve_grep_candidates(MagicMock(), MagicMock(), file_subset=path)
        assert err is None
        assert truncated is False
        assert len(candidates) == 1
        norm = os.path.normpath(os.path.abspath(path))
        assert candidates[0]["path"] == norm
        assert candidates[0]["url"] == path_to_file_url(norm)
    finally:
        os.unlink(path)


def test_resolve_grep_candidates_file_url_opens_that_file():
    with tempfile.NamedTemporaryFile(suffix=".ods", delete=False) as f:
        path = f.name
    try:
        file_url = Path(path).as_uri()
        with patch("plugin.doc.document_research_grep.list_nearby_files") as mock_list:
            candidates, truncated, err = resolve_grep_candidates(MagicMock(), MagicMock(), file_subset=file_url)
            mock_list.assert_not_called()
        assert err is None
        assert truncated is False
        assert len(candidates) == 1
        norm = os.path.normpath(os.path.abspath(path))
        assert candidates[0]["path"] == norm
        assert candidates[0]["url"] == path_to_file_url(norm)
    finally:
        os.unlink(path)


def test_resolve_grep_candidates_missing_file_url_is_an_error():
    missing_path = "/nonexistent/path/to/missing.ods"
    missing_url = Path(missing_path).as_uri()
    with patch("plugin.doc.document_research_grep.list_nearby_files") as mock_list:
        candidates, truncated, err = resolve_grep_candidates(MagicMock(), MagicMock(), file_subset=missing_url)
        mock_list.assert_not_called()
    assert candidates == []
    assert truncated is False
    assert err is not None
    assert f"No file matching {missing_url!r}" in err


def test_resolve_grep_candidates_unc_file_url_rejected():
    unc_url = "file://server/share/Budget.ods"
    with patch("os.path.isfile", return_value=True):
        with patch("plugin.doc.document_research_grep.list_nearby_files") as mock_list:
            candidates, truncated, err = resolve_grep_candidates(MagicMock(), MagicMock(), file_subset=unc_url)
            mock_list.assert_not_called()
    assert candidates == []
    assert truncated is False
    assert err is not None
    assert f"No file matching {unc_url!r}" in err


def test_resolve_grep_candidates_listing_truncated_flag():
    with tempfile.TemporaryDirectory() as tmp:
        files = []
        for i in range(3):
            p = os.path.join(tmp, f"Budget_{i}.ods")
            with open(p, "wb"):
                pass
            files.append(
                {
                    "path": p,
                    "name": os.path.basename(p),
                    "url": f"file:///{i}",
                    "modified": float(i),
                    "size_bytes": 0,
                    "doc_type_guess": "calc",
                    "is_open": False,
                }
            )

        with patch("plugin.doc.document_research_grep.list_nearby_files") as mock_list:
            mock_list.return_value = {"status": "ok", "files": files, "truncated": True}
            candidates, truncated, err = resolve_grep_candidates(MagicMock(), MagicMock(), file_subset="budget")

        assert err is None
        assert truncated is True
        assert len(candidates) == 3


def test_resolve_grep_candidates_open_files_first():
    closed = "/tmp/closed.ods"
    open_f = "/tmp/open.ods"
    with patch("plugin.doc.document_research_grep.list_nearby_files") as mock_list:
        mock_list.return_value = {
            "status": "ok",
            "files": [
                {"path": closed, "name": "closed.ods", "url": "", "modified": 100.0, "size_bytes": 0, "doc_type_guess": "calc", "is_open": False},
                {"path": open_f, "name": "open.ods", "url": "", "modified": 1.0, "size_bytes": 0, "doc_type_guess": "calc", "is_open": True},
            ],
            "truncated": False,
        }
        candidates, _, _ = resolve_grep_candidates(MagicMock(), MagicMock())

    assert candidates[0]["path"] == open_f


@patch("plugin.doc.document_research_grep.close_document_research_document")
@patch("plugin.doc.document_research_grep.open_document_for_read")
@patch("plugin.doc.document_research_grep._search_opened_document")
@patch("plugin.doc.document_research_grep.resolve_grep_candidates")
def test_grep_nearby_files_aggregates_hits(mock_resolve, mock_search, mock_open, mock_close):
    mock_resolve.return_value = (
        [{"path": "/tmp/Budget.ods", "name": "Budget.ods", "url": "file:///b", "doc_type_guess": "calc", "is_open": False}],
        False,
        None,
    )
    mock_open.return_value = (MagicMock(), "calc", None, True)
    mock_search.return_value = (
        [{"sheet": "Sheet1", "cell": "A1", "value": "Q4 revenue"}],
        1,
        False,
        None,
    )

    result = grep_nearby_files(MagicMock(), MagicMock(), MagicMock(), "Q4", file_subset="budget")

    assert result["status"] == "ok"
    assert result["files_scanned"] == 1
    assert result["files_with_hits"] == 1
    assert len(result["hits"]) == 1
    assert result["hits"][0]["matches"][0]["value"] == "Q4 revenue"
    mock_close.assert_called_once()


@patch("plugin.doc.document_research_grep.close_document_research_document")
@patch("plugin.doc.document_research_grep.open_document_for_read")
@patch("plugin.doc.document_research_grep._search_opened_document")
@patch("plugin.doc.document_research_grep.resolve_grep_candidates")
def test_grep_nearby_files_stopped_early_on_total_cap(mock_resolve, mock_search, mock_open, mock_close):
    mock_resolve.return_value = (
        [
            {"path": "/tmp/a.ods", "name": "a.ods", "url": "", "doc_type_guess": "calc", "is_open": False},
            {"path": "/tmp/b.ods", "name": "b.ods", "url": "", "doc_type_guess": "calc", "is_open": False},
        ],
        False,
        None,
    )
    mock_open.return_value = (MagicMock(), "calc", None, True)
    mock_search.return_value = (
        [{"sheet": "S", "cell": "A1", "value": "x"}],
        1,
        False,
        None,
    )

    with patch("plugin.doc.document_research_grep.DEFAULT_GREP_MAX_TOTAL_RESULTS", 1):
        result = grep_nearby_files(
            MagicMock(),
            MagicMock(),
            MagicMock(),
            "x",
        )

    assert result["stopped_early"] is True
    assert result["files_scanned"] == 1


@patch("plugin.doc.document_research_grep.close_document_research_document")
@patch("plugin.doc.document_research_grep.open_document_for_read")
@patch("plugin.doc.document_research_grep._search_opened_document")
@patch("plugin.doc.document_research_grep.resolve_grep_candidates")
def test_grep_nearby_files_stop_checker(mock_resolve, mock_search, mock_open, mock_close):
    mock_resolve.return_value = (
        [
            {"path": "/tmp/a.ods", "name": "a.ods", "url": "", "doc_type_guess": "calc", "is_open": False},
            {"path": "/tmp/b.ods", "name": "b.ods", "url": "", "doc_type_guess": "calc", "is_open": False},
        ],
        False,
        None,
    )
    mock_open.return_value = (MagicMock(), "calc", None, True)
    mock_search.return_value = ([{"sheet": "S", "cell": "A1", "value": "x"}], 1, False, None)

    stopped = {"n": 0}

    def stop_after_first():
        stopped["n"] += 1
        return stopped["n"] > 1

    result = grep_nearby_files(
        MagicMock(),
        MagicMock(),
        MagicMock(),
        "x",
        stop_checker=stop_after_first,
    )

    assert result["status"] == "error"
    assert result["code"] == "USER_STOPPED"


@patch("plugin.doc.document_research_grep.close_document_research_document")
@patch("plugin.doc.document_research_grep.open_document_for_read")
@patch("plugin.doc.document_research_grep.resolve_grep_candidates")
def test_grep_nearby_files_open_error_continues(mock_resolve, mock_open, mock_close):
    mock_resolve.return_value = (
        [{"path": "/tmp/bad.ods", "name": "bad.ods", "url": "", "doc_type_guess": "calc", "is_open": False}],
        False,
        None,
    )
    mock_open.return_value = (None, None, "Permission denied", False)

    result = grep_nearby_files(MagicMock(), MagicMock(), MagicMock(), "Q4")

    assert result["status"] == "ok"
    assert result["hits"] == []
    assert result["errors"][0]["message"] == "Permission denied"


def test_grep_nearby_files_requires_pattern():
    result = grep_nearby_files(MagicMock(), MagicMock(), MagicMock(), "  ")
    assert result["status"] == "error"


def _draw_text_shape(text: str) -> MagicMock:
    shape = MagicMock()
    shape.getString.return_value = text
    shape.getShapeType.return_value = "com.sun.star.drawing.TextShape"
    return shape


def _draw_page(shapes: list[MagicMock]) -> MagicMock:
    page = MagicMock()
    page.getCount.return_value = len(shapes)
    page.getByIndex.side_effect = lambda index: shapes[index]
    return page


def test_grep_text_in_draw_match_count_uses_remaining_page_budget():
    """A later page must not search with the original max_results."""
    page0 = _draw_page([_draw_text_shape("alpha one"), _draw_text_shape("alpha two"), _draw_text_shape("nope")])
    page1 = _draw_page(
        [_draw_text_shape("alpha three"), _draw_text_shape("alpha four"), _draw_text_shape("alpha five")]
    )
    pages = MagicMock()
    pages.getCount.return_value = 2
    pages.getByIndex.side_effect = lambda index: (page0, page1)[index]
    model = MagicMock()
    model.getDrawPages.return_value = pages

    matches, count, partial = _grep_text_in_draw(model, "alpha", max_results=3)

    assert [row["text"] for row in matches] == ["alpha one", "alpha two", "alpha three"]
    assert count == len(matches) == 3
    assert partial is True
    # Remaining budget was 2 (scan_limit = 4, page0 returned 2, remaining = 2), so page1 probes 2 shapes
    assert page1.getByIndex.call_count == 2


def test_grep_text_in_draw_count_matches_sliced_return_when_page_overshoots():
    def overshoot(*args: object, **kwargs: object) -> tuple[list[dict[str, str]], int, bool]:
        assert kwargs["max_results"] == 3
        return (
            [{"page_index": 0, "shape_index": str(i), "text": "x"} for i in range(4)],
            4,
            False,
        )

    pages = MagicMock()
    pages.getCount.return_value = 1
    pages.getByIndex.return_value = MagicMock()
    model = MagicMock()
    model.getDrawPages.return_value = pages
    with patch("plugin.doc.document_research_grep._grep_shapes_on_page", side_effect=overshoot):
        matches, count, partial = _grep_text_in_draw(model, "x", max_results=2)
    assert len(matches) == 2
    assert count == 2
    assert partial is True


def test_grep_text_in_draw_extra_shape_read_no_match_partial_false():
    page0 = _draw_page([_draw_text_shape("alpha"), _draw_text_shape("nope")])
    pages = MagicMock()
    pages.getCount.return_value = 1
    pages.getByIndex.side_effect = lambda index: [page0][index]
    model = MagicMock()
    model.getDrawPages.return_value = pages

    matches, count, partial = _grep_text_in_draw(model, "alpha", max_results=1)

    assert [row["text"] for row in matches] == ["alpha"]
    assert count == len(matches) == 1
    assert partial is False
    assert page0.getByIndex.call_count == 2

def test_grep_text_in_writer_includes_table_text():
    model = MagicMock()
    services = MagicMock()

    # Create paragraphs and a table
    para1 = MagicMock()
    para1.supportsService.side_effect = lambda s: s == "com.sun.star.text.Paragraph"
    para1.getString.return_value = "Normal text before table."
    para1.createEnumeration.side_effect = RuntimeError("no enum")

    table = MagicMock()
    table.supportsService.side_effect = lambda s: s == "com.sun.star.text.TextTable"
    table.getCellNames.return_value = ("A1", "B1")

    cellA1 = MagicMock()
    cellA1.getString.return_value = "Cell one text."
    cellA1.createEnumeration.side_effect = RuntimeError("no enum")
    cellB1 = MagicMock()
    cellB1.getString.return_value = "target keyword cell."
    cellB1.createEnumeration.side_effect = RuntimeError("no enum")

    def get_cell_by_name(name):
        if name == "A1": return cellA1
        elif name == "B1": return cellB1
        raise Exception()

    table.getCellByName.side_effect = get_cell_by_name

    services.document.get_paragraph_ranges.return_value = [para1, table]

    matches, count, partial = _grep_text_in_writer(
        model,
        services,
        pattern="keyword",
        max_results=5,
    )

    assert count == 1
    assert partial is False


def test_grep_text_in_writer_caps_match_count_and_sets_partial():
    model = MagicMock()
    services = MagicMock()

    para1 = MagicMock()
    para1.supportsService.side_effect = lambda s: s == "com.sun.star.text.Paragraph"
    para1.getString.return_value = "first keyword paragraph"
    para1.createEnumeration.side_effect = RuntimeError("no enum")

    para2 = MagicMock()
    para2.supportsService.side_effect = lambda s: s == "com.sun.star.text.Paragraph"
    para2.getString.return_value = "second keyword paragraph"
    para2.createEnumeration.side_effect = RuntimeError("no enum")

    services.document.get_paragraph_ranges.return_value = [para1, para2]

    matches, count, partial = _grep_text_in_writer(
        model,
        services,
        pattern="keyword",
        max_results=1,
    )

    assert len(matches) == 1
    assert count == 1
    assert partial is True


def test_grep_text_in_writer_stop_checker_skips_later_paragraphs():
    model = MagicMock()
    services = MagicMock()

    para1 = MagicMock()
    para1.supportsService.side_effect = lambda s: s == "com.sun.star.text.Paragraph"
    para1.getString.return_value = "first"
    para1.createEnumeration.side_effect = RuntimeError("no enum")

    para2 = MagicMock()
    para2.supportsService.side_effect = lambda s: s == "com.sun.star.text.Paragraph"
    para2.getString.return_value = "second"
    para2.createEnumeration.side_effect = RuntimeError("no enum")

    para3 = MagicMock()
    para3.supportsService.side_effect = lambda s: s == "com.sun.star.text.Paragraph"
    para3.getString.return_value = "third"
    para3.createEnumeration.side_effect = RuntimeError("no enum")

    services.document.get_paragraph_ranges.return_value = [para1, para2, para3]

    stop_checker = MagicMock(return_value=True)

    matches, count, partial = _grep_text_in_writer(
        model,
        services,
        pattern="first",
        max_results=5,
        stop_checker=stop_checker,
    )

    para2.getString.assert_not_called()
    para3.getString.assert_not_called()
    assert partial is True


def test_grep_text_in_writer_uses_visible_text():

    model = MagicMock()
    services = MagicMock()

    # Paragraph test
    para = MagicMock()
    para.supportsService.side_effect = lambda s: s == "com.sun.star.text.Paragraph"
    para.getString.return_value = "deleted keyword"
    services.document.get_paragraph_ranges.return_value = [para]

    with patch("plugin.doc.document_research_grep.get_string_without_tracked_deletions") as mock_helper:
        mock_helper.return_value = "visible keyword"

        matches, count, partial = _grep_text_in_writer(
            model,
            services,
            pattern="visible",
            max_results=5,
        )
        assert count == 1
        assert partial is False
        mock_helper.assert_called_with(para)

        matches_del, count_del, _ = _grep_text_in_writer(
            model,
            services,
            pattern="deleted",
            max_results=5,
        )
        assert count_del == 0

    # Table cell test
    table = MagicMock()
    table.supportsService.side_effect = lambda s: s == "com.sun.star.text.TextTable"
    table.getCellNames.return_value = ("A1",)
    cell = MagicMock()
    cell.getString.return_value = "deleted cell keyword"
    table.getCellByName.return_value = cell
    services.document.get_paragraph_ranges.return_value = [table]

    with patch("plugin.doc.document_research_grep.get_string_without_tracked_deletions") as mock_helper:
        mock_helper.return_value = "visible cell keyword"

        matches, count, partial = _grep_text_in_writer(
            model,
            services,
            pattern="visible",
            max_results=5,
        )
        assert count == 1
        assert partial is False
        mock_helper.assert_called_with(cell)

        matches_del, count_del, _ = _grep_text_in_writer(
            model,
            services,
            pattern="deleted",
            max_results=5,
        )
        assert count_del == 0


def test_grep_text_in_calc_partial_when_more_hits_exist():
    hits = [
        {"sheet": "Sheet1", "cell": "A1", "value": "val1"},
        {"sheet": "Sheet1", "cell": "A2", "value": "val2"},
    ]
    with patch("plugin.doc.document_research_grep.search_spreadsheet_cells") as mock_search:
        mock_search.return_value = hits
        matches, count, partial = _grep_text_in_calc(MagicMock(), "val", max_results=1)

        assert mock_search.call_args.kwargs.get("max_results") == 2
        assert len(matches) == 1
        assert count == 1
        assert partial is True


def test_grep_text_in_calc_not_partial_when_results_fit():
    hits = [{"sheet": "Sheet1", "cell": "A1", "value": "val1"}]
    with patch("plugin.doc.document_research_grep.search_spreadsheet_cells") as mock_search:
        mock_search.return_value = hits
        matches, count, partial = _grep_text_in_calc(MagicMock(), "val", max_results=1)

        assert mock_search.call_args.kwargs.get("max_results") == 2
        assert len(matches) == 1
        assert count == 1
        assert partial is False


def test_search_opened_document_calc_forwards_partial():
    hits = [
        {"sheet": "Sheet1", "cell": "A1", "value": "val1"},
        {"sheet": "Sheet1", "cell": "A2", "value": "val2"},
    ]
    with patch("plugin.doc.document_research_grep.search_spreadsheet_cells") as mock_search:
        mock_search.return_value = hits
        matches, count, partial, err = _search_opened_document(
            MagicMock(),
            "calc",
            MagicMock(),
            "val",
            regex=False,
            case_sensitive=False,
            max_results_per_file=1,
        )
        assert err is None
        assert partial is True
        assert count == 1
        assert len(matches) == 1


@patch("plugin.doc.document_research_grep.open_document_for_read")
@patch("plugin.doc.document_research_grep.resolve_grep_candidates")
def test_grep_nearby_files_invalid_regex_does_not_open_files(mock_resolve, mock_open):
    result = grep_nearby_files(
        MagicMock(),
        MagicMock(),
        MagicMock(),
        pattern="(",
        regex=True,
    )
    assert result["status"] == "error"
    assert "Invalid regex:" in result["message"]
    mock_resolve.assert_not_called()
    mock_open.assert_not_called()


@patch("plugin.doc.document_research_grep.close_document_research_document")
@patch("plugin.doc.document_research_grep.open_document_for_read")
@patch("plugin.doc.document_research_grep._search_opened_document")
@patch("plugin.doc.document_research_grep.resolve_grep_candidates")
def test_grep_nearby_files_runs_uno_on_main_thread(mock_resolve, mock_search, mock_open, mock_close):
    mock_resolve.return_value = (
        [{"path": "/tmp/Budget.ods", "name": "Budget.ods", "url": "file:///b", "doc_type_guess": "calc", "is_open": False}],
        False,
        None,
    )
    mock_open.return_value = (MagicMock(), "calc", None, True)
    mock_search.return_value = (
        [{"sheet": "Sheet1", "cell": "A1", "value": "Q4 revenue"}],
        1,
        False,
        None,
    )

    with patch("plugin.framework.queue_executor.execute_on_main_thread") as mock_execute:
        def side_effect(func):
            return func()
        mock_execute.side_effect = side_effect

        result = grep_nearby_files(MagicMock(), MagicMock(), MagicMock(), "Q4", file_subset="budget")

        assert mock_execute.call_count == 3
        assert result["status"] == "ok"

@patch("plugin.doc.document_research.os.listdir")
@patch("plugin.doc.document_research.resolve_listing_directory")
@patch("plugin.framework.thread_guard.on_main_thread", return_value=True)
def test_grep_nearby_files_stop_checker_aborts_during_directory_scan(mock_omt, mock_rld, mock_listdir):
    mock_rld.return_value = "/tmp"
    # Create enough files to trigger the modulo check at idx % 50 == 0
    mock_listdir.return_value = [f"file{i}.ods" for i in range(100)]

    stop_checker = MagicMock(side_effect=[False, True])

    result = grep_nearby_files(
        MagicMock(),
        MagicMock(),
        MagicMock(),
        "Q4",
        stop_checker=stop_checker,
    )

    assert result["status"] == "error"
    assert result["code"] == "USER_STOPPED"


@patch("plugin.doc.document_research_grep.close_document_research_document")
@patch("plugin.doc.document_research_grep.open_document_for_read")
@patch("plugin.doc.document_research_grep._search_opened_document")
@patch("plugin.doc.document_research_grep.resolve_grep_candidates")
def test_grep_nearby_files_stop_during_last_file_is_user_stopped(mock_resolve, mock_search, mock_open, mock_close):
    mock_resolve.return_value = (
        [{"path": "/tmp/a.ods", "name": "a.ods", "url": "", "doc_type_guess": "calc", "is_open": False}],
        False,
        None,
    )
    mock_open.return_value = (MagicMock(), "calc", None, True)
    mock_search.return_value = ([{"sheet": "S", "cell": "A1", "value": "x"}], 1, True, None)

    calls = {"n": 0}

    def stop_after_top():
        calls["n"] += 1
        return calls["n"] > 1

    result = grep_nearby_files(
        MagicMock(),
        MagicMock(),
        MagicMock(),
        "x",
        stop_checker=stop_after_top,
    )

    assert result["status"] == "error"
    assert result["code"] == "USER_STOPPED"
    assert "hits" not in result
    mock_close.assert_called_once()


@patch("plugin.doc.document_research_grep.close_document_research_document")
@patch("plugin.doc.document_research_grep.open_document_for_read")
@patch("plugin.doc.document_research_grep._search_opened_document")
@patch("plugin.doc.document_research_grep.resolve_grep_candidates")
def test_grep_nearby_files_close_post_timeout_keeps_result(mock_resolve, mock_search, mock_open, mock_close):
    mock_resolve.return_value = (
        [{"path": "/tmp/Budget.ods", "name": "Budget.ods", "url": "file:///b", "doc_type_guess": "calc", "is_open": False}],
        False,
        None,
    )
    mock_open.return_value = (MagicMock(), "calc", None, True)
    mock_search.return_value = (
        [{"sheet": "Sheet1", "cell": "A1", "value": "Q4 revenue"}],
        1,
        False,
        None,
    )

    call_count = {"n": 0}

    def mock_execute(fn):
        call_count["n"] += 1
        if call_count["n"] == 3:  # close call
            raise TimeoutError("Main thread timed out")
        return fn()

    with patch("plugin.framework.queue_executor.execute_on_main_thread", side_effect=mock_execute), \
         patch("plugin.framework.queue_executor.post_to_main_thread", side_effect=TimeoutError("Queue full")):
        result = grep_nearby_files(MagicMock(), MagicMock(), MagicMock(), "Q4")

    assert result["status"] == "ok"
    assert result["files_with_hits"] == 1
    assert len(result["hits"]) == 1
