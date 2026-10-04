# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
"""Unit tests for document_research grep (grep_nearby_files)."""

from __future__ import annotations

import os
import tempfile
from unittest.mock import MagicMock, patch

from plugin.doc.document_research_grep import grep_nearby_files, resolve_grep_candidates


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
        assert candidates[0]["path"] == os.path.normpath(os.path.abspath(path))
    finally:
        os.unlink(path)


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


@patch("plugin.doc.document_research_grep._process_events_if_available")
@patch("plugin.doc.document_research_grep.close_document_research_document")
@patch("plugin.doc.document_research_grep.open_document_for_read")
@patch("plugin.doc.document_research_grep._search_opened_document")
@patch("plugin.doc.document_research_grep.resolve_grep_candidates")
def test_grep_nearby_files_aggregates_hits(mock_resolve, mock_search, mock_open, mock_close, mock_events):
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


@patch("plugin.doc.document_research_grep._process_events_if_available")
@patch("plugin.doc.document_research_grep.close_document_research_document")
@patch("plugin.doc.document_research_grep.open_document_for_read")
@patch("plugin.doc.document_research_grep._search_opened_document")
@patch("plugin.doc.document_research_grep.resolve_grep_candidates")
def test_grep_nearby_files_stopped_early_on_total_cap(mock_resolve, mock_search, mock_open, mock_close, mock_events):
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


@patch("plugin.doc.document_research_grep._process_events_if_available")
@patch("plugin.doc.document_research_grep.close_document_research_document")
@patch("plugin.doc.document_research_grep.open_document_for_read")
@patch("plugin.doc.document_research_grep._search_opened_document")
@patch("plugin.doc.document_research_grep.resolve_grep_candidates")
def test_grep_nearby_files_stop_checker(mock_resolve, mock_search, mock_open, mock_close, mock_events):
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


@patch("plugin.doc.document_research_grep._process_events_if_available")
@patch("plugin.doc.document_research_grep.close_document_research_document")
@patch("plugin.doc.document_research_grep.open_document_for_read")
@patch("plugin.doc.document_research_grep.resolve_grep_candidates")
def test_grep_nearby_files_open_error_continues(mock_resolve, mock_open, mock_close, mock_events):
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
    from plugin.doc.document_research_grep import _grep_text_in_draw

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
    assert partial is False
    # Remaining budget was 1, so the second page stops after its first hit.
    assert page1.getByIndex.call_count == 1


def test_grep_text_in_draw_count_matches_sliced_return_when_page_overshoots():
    from plugin.doc.document_research_grep import _grep_text_in_draw

    def overshoot(*args: object, **kwargs: object) -> tuple[list[dict[str, str]], int, bool]:
        assert kwargs["max_results"] == 2
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
    assert partial is False

def test_grep_text_in_writer_includes_table_text():
    from plugin.doc.document_research_grep import _grep_text_in_writer

    model = MagicMock()
    services = MagicMock()

    # Create paragraphs and a table
    para1 = MagicMock()
    para1.supportsService.side_effect = lambda s: s == "com.sun.star.text.Paragraph"
    para1.getString.return_value = "Normal text before table."

    table = MagicMock()
    table.supportsService.side_effect = lambda s: s == "com.sun.star.text.TextTable"
    table.getCellNames.return_value = ("A1", "B1")

    cellA1 = MagicMock()
    cellA1.getString.return_value = "Cell one text."
    cellB1 = MagicMock()
    cellB1.getString.return_value = "target keyword cell."

    def get_cell_by_name(name):
        if name == "A1": return cellA1
        elif name == "B1": return cellB1
        raise Exception()

    table.getCellByName.side_effect = get_cell_by_name

    services.document.get_paragraph_ranges.return_value = [para1, table]

    matches, count = _grep_text_in_writer(
        model,
        services,
        pattern="keyword",
        max_results=5
    )

    assert count == 1


@patch("plugin.doc.document_research_grep._process_events_if_available")
@patch("plugin.doc.document_research_grep.close_document_research_document")
@patch("plugin.doc.document_research_grep.open_document_for_read")
@patch("plugin.doc.document_research_grep._search_opened_document")
@patch("plugin.doc.document_research_grep.resolve_grep_candidates")
def test_grep_nearby_files_runs_uno_on_main_thread(mock_resolve, mock_search, mock_open, mock_close, mock_events):
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

        assert mock_execute.call_count >= 4
        assert result["status"] == "ok"
