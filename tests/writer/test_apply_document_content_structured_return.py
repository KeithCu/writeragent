# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# Pure pytest (no LibreOffice): the structured-return logic of apply_document_content.
# We mock the range finders + the replace helpers so only the replaced_count / status logic
# is exercised: replaced_count == 0 -> status "error", N > 0 -> "ok".
import types
from unittest.mock import MagicMock

import pytest

import plugin.writer.html_import as format_mod
import plugin.writer.search as search_mod
from plugin.writer.apply_document_content import (
    ApplyDocumentContent,
    build_search_replace_response,
)


def test_heading_rewrite_uno_skips_windows_leftover_hidden_apply() -> None:
    """GHA 34681661844: leftover Hidden _default swriter hung on second apply."""
    from pathlib import Path

    src = Path(__file__).with_name(
        "test_apply_document_content_heading_rewrite_uno.py"
    ).read_text(encoding="utf-8")
    assert "skip_windows_leftover_hidden_load" in src
    assert "apply_document_content Hidden _default swriter" in src
    assert "34681661844" in src
    assert "html_to_plain_text" in src


def test_later_apply_uno_skips_windows_leftover_hidden_apply() -> None:
    """GHA 34683742049: leftover Hidden _default hung on content-style write."""
    from pathlib import Path

    writer = Path(__file__).parent
    for name in (
        "test_content_style_model_uno.py",
        "test_format_uno.py",
        "test_track_changes_reviewable_uno.py",
    ):
        src = (writer / name).read_text(encoding="utf-8")
        # Suites that share native_writer_tool_context pass the reason in;
        # the skip call itself lives in testing_utils.
        assert (
            "skip_windows_leftover_hidden_load" in src
            or "native_writer_tool_context" in src
        ), name
        assert "apply_document_content Hidden _default swriter" in src, name
    style_src = (writer / "test_content_style_model_uno.py").read_text(encoding="utf-8")
    assert "34683742049" in style_src
    assert "html_to_plain_text" in style_src
    track_src = (writer / "test_track_changes_reviewable_uno.py").read_text(encoding="utf-8")
    assert "track_changes wait timeout leftover reuse" in track_src
    assert "34692834349" in track_src


def _ctx():
    doc = MagicMock()
    um = MagicMock()
    um.isLocked.return_value = False
    doc.getUndoManager.return_value = um
    return types.SimpleNamespace(
        doc=doc, ctx=object(),
        services=types.SimpleNamespace(get=lambda key, default=None: None),
    )


@pytest.fixture(autouse=True)
def _no_libreoffice(monkeypatch):
    # Keep everything in-memory: plain-text content (use_preserve path) and no real replace.
    monkeypatch.setattr(format_mod, "content_has_markup", lambda *a, **k: False)
    monkeypatch.setattr(search_mod, "normalize_search_string_for_find", lambda s: s)
    monkeypatch.setattr(format_mod, "replace_preserving_format", lambda *a, **k: None)
    monkeypatch.setattr(format_mod, "replace_single_range_with_content", lambda *a, **k: None)
    monkeypatch.setattr(search_mod, "drawing_shape_object_containing", lambda *a, **k: None)


class MockRange:
    def __init__(self, text: str = "foo") -> None:
        self._text = text

    def getString(self) -> str:
        return self._text

    def getText(self):
        return MagicMock()


def test_search_no_match_returns_error_zero(monkeypatch):
    monkeypatch.setattr(search_mod, "find_first_range", lambda doc, s: None)
    res = ApplyDocumentContent().execute(_ctx(), target="search", old_content="zzz", content="BAR")
    assert res["status"] == "error", res
    assert res["replaced_count"] == 0, res


def test_search_single_success(monkeypatch):
    monkeypatch.setattr(search_mod, "find_first_range", lambda doc, s: MockRange())
    res = ApplyDocumentContent().execute(_ctx(), target="search", old_content="foo", content="BAR")
    assert res["status"] == "ok", res
    assert res["replaced_count"] == 1, res


def test_search_all_matches_reports_count(monkeypatch):
    monkeypatch.setattr(search_mod, "find_all_ranges", lambda doc, s: [MockRange(), MockRange(), MockRange()])
    res = ApplyDocumentContent().execute(
        _ctx(), target="search", old_content="foo", content="BAR", all_matches=True)
    assert res["status"] == "ok", res
    assert res["replaced_count"] == 3, res


def test_search_all_matches_no_match_errors(monkeypatch):
    monkeypatch.setattr(search_mod, "find_all_ranges", lambda doc, s: [])
    res = ApplyDocumentContent().execute(
        _ctx(), target="search", old_content="zzz", content="BAR", all_matches=True)
    assert res["status"] == "error", res
    assert res["replaced_count"] == 0, res
    assert res["message"].startswith("Replaced 0 occurrence"), res


# --- data-lo-para warning annotations on write --------------------------------------


def test_mapped_data_lo_para_full_document_has_no_warnings():
    """Clean write with no unapplied properties produces no warnings."""
    from plugin.writer.apply_document_content import _attach_para_warnings

    res = _attach_para_warnings(
        {"status": "ok", "message": "Replaced entire document."},
        [],
    )
    assert "warnings" not in res


def test_unmapped_data_lo_para_full_document_warns_for_unmapped_property():
    """Unmapped properties report one warning per dropped property name."""
    from plugin.writer.apply_document_content import _attach_para_warnings

    res = _attach_para_warnings(
        {"status": "ok", "message": "Replaced entire document."},
        ["font-size"],
    )
    assert res.get("warnings") == ["data-lo-para property 'font-size' was not applied"]


def test_failed_write_is_not_annotated():
    """Nothing was written, so there is nothing to say about what was not applied."""
    from plugin.writer.apply_document_content import _attach_para_warnings

    original = {"status": "error", "message": "old_content not found."}
    assert _attach_para_warnings(original, ["margin-left"]) == original


def test_search_target_warns_for_mapped_property(monkeypatch):
    """Search replacement does not apply data-lo-para; every property warns."""
    monkeypatch.setattr(search_mod, "find_first_range", lambda doc, s: MockRange())
    res = ApplyDocumentContent().execute(
        _ctx(),
        target="search",
        old_content="foo",
        content='<p data-lo-para="text-align:center">x</p>',
    )
    assert res.get("status") == "ok", res
    assert res.get("warnings") == ["data-lo-para property 'text-align' was not applied"]


def test_content_without_the_attribute_is_untouched(monkeypatch):
    """Content without data-lo-para or body text mentioning the words produces no warnings."""
    monkeypatch.setattr(search_mod, "find_first_range", lambda doc, s: MockRange())
    res = ApplyDocumentContent().execute(
        _ctx(),
        target="search",
        old_content="foo",
        content="<p>The words data-lo-para describe a read-only report.</p>",
    )
    assert res.get("status") == "ok", res
    assert "warnings" not in res

def test_search_occurrence_selects_requested_match(monkeypatch):
    first = MockRange()
    second = MockRange()
    selected = []

    monkeypatch.setattr(
        search_mod,
        "find_all_ranges",
        lambda doc, s: [first, second],
    )
    monkeypatch.setattr(
        "plugin.writer.apply_document_content.record_preserve_replace",
        lambda session, doc, found, content, ctx, reviewable: selected.append(found),
    )

    res = ApplyDocumentContent().execute(
        _ctx(),
        target="search",
        old_content="foo",
        content="BAR",
        occurrence=1,
    )

    assert res["status"] == "ok", res
    assert res["replaced_count"] == 1, res
    assert res["occurrence"] == 1, res
    assert selected == [second]


def test_search_occurrence_rejects_all_matches():
    res = ApplyDocumentContent().execute(
        _ctx(),
        target="search",
        old_content="foo",
        content="BAR",
        occurrence=0,
        all_matches=True,
    )

    assert res["status"] == "error", res
    assert "cannot be combined with all_matches=true" in res["message"]


def test_search_occurrence_out_of_range(monkeypatch):
    monkeypatch.setattr(
        search_mod,
        "find_all_ranges",
        lambda doc, s: [MockRange(), MockRange()],
    )

    res = ApplyDocumentContent().execute(
        _ctx(),
        target="search",
        old_content="foo",
        content="BAR",
        occurrence=2,
    )

    assert res["status"] == "error", res
    assert res["code"] == "OCCURRENCE_OUT_OF_RANGE", res
    assert res["details"]["count"] == 2, res
    assert "use 0..1" in res["message"]


def test_search_occurrence_dry_run_selects_without_editing(monkeypatch):
    first = MockRange()
    second = MockRange()

    monkeypatch.setattr(
        search_mod,
        "find_all_ranges",
        lambda doc, s: [first, second],
    )
    monkeypatch.setattr(
        search_mod,
        "describe_match_location",
        lambda found, doc, label_cache=None:
            "first" if found is first else "second",
    )
    monkeypatch.setattr(
        search_mod,
        "sweep_draw_shape_preview_matches",
        lambda *args, **kwargs: [],
    )
    monkeypatch.setattr(
        search_mod,
        "sweep_comment_preview_matches",
        lambda *args, **kwargs: [],
    )

    res = ApplyDocumentContent().execute(
        _ctx(),
        target="search",
        old_content="foo",
        content="BAR",
        occurrence=1,
        dry_run=True,
    )

    assert res["status"] == "ok", res
    assert res["dry_run"] is True, res
    assert res["count"] == 2, res
    assert res["replaceable_count"] == 2, res
    assert res["selected_occurrence"] == 1, res
    assert res["selected_match"]["location"] == "second", res
    assert "matches" not in res, res
    assert res["replaceable"][0]["occurrence"] == 0, res
    assert res["replaceable"][1]["occurrence"] == 1, res
    assert res["not_replaceable"] == [], res


def test_search_occurrence_rejects_non_search_target():
    res = ApplyDocumentContent().execute(
        _ctx(),
        target="end",
        content="BAR",
        occurrence=0,
    )

    assert res["status"] == "error", res
    assert res["code"] == "INVALID_PARAM", res
    assert "only applies to target='search'" in res["message"]


def test_search_occurrence_rejects_bool():
    res = ApplyDocumentContent().execute(
        _ctx(),
        target="search",
        old_content="foo",
        content="BAR",
        occurrence=True,
    )

    assert res["status"] == "error", res
    assert res["code"] == "INVALID_PARAM", res
    assert "non-negative integer" in res["message"]


def test_search_occurrence_regex_selects_requested_match(monkeypatch):
    first = MockRange()
    second = MockRange()
    selected = []

    monkeypatch.setattr(
        search_mod,
        "find_ranges_regex_case",
        lambda *args, **kwargs: [first, second],
    )
    def unused_find_all(doc, s):
        raise AssertionError("regex path must not use find_all_ranges")

    monkeypatch.setattr(search_mod, "find_all_ranges", unused_find_all)
    monkeypatch.setattr(
        "plugin.writer.apply_document_content.record_preserve_replace",
        lambda session, doc, found, content, ctx, reviewable: selected.append(found),
    )

    res = ApplyDocumentContent().execute(
        _ctx(),
        target="search",
        old_content="foo.",
        content="BAR",
        occurrence=1,
        regex=True,
    )

    assert res["status"] == "ok", res
    assert res["occurrence"] == 1, res
    assert selected == [second]


def test_search_occurrence_position_after_uses_selected_match(monkeypatch):
    first = MagicMock()
    second = MagicMock()
    cursor = MagicMock()
    cursor.getPropertyValue.return_value = None
    second.getText.return_value.createTextCursorByRange.return_value = cursor

    monkeypatch.setattr(
        search_mod,
        "find_all_ranges",
        lambda doc, s: [first, second],
    )
    monkeypatch.setattr("plugin.writer.apply_document_content.collapsed_anchor", lambda found: None)
    monkeypatch.setattr(format_mod, "html_fragment_contains_mixed_math", lambda content: False)
    monkeypatch.setattr(format_mod, "content_has_markup", lambda *args, **kwargs: True)
    inserted = []
    monkeypatch.setattr(
        format_mod,
        "insert_html_at_cursor",
        lambda *args, **kwargs: inserted.append(True),
    )
    monkeypatch.setattr(
        "plugin.writer.apply_document_content.record_html_atomically",
        lambda session, doc, mutate, *rest, **kwargs: mutate(),
    )

    res = ApplyDocumentContent().execute(
        _ctx(),
        target="search",
        old_content="foo",
        content=["<p>novo</p>"],
        occurrence=1,
        position="after",
    )

    assert res["status"] == "ok", res
    assert res["inserted"] is True, res
    assert res["position"] == "after", res
    assert res["occurrence"] == 1, res
    assert "replaced_count" not in res
    assert inserted
    second.getEnd.assert_called()


def test_search_occurrence_empty_ranges_falls_through_to_not_found(monkeypatch):
    monkeypatch.setattr(search_mod, "find_all_ranges", lambda doc, s: [])

    res = ApplyDocumentContent().execute(
        _ctx(),
        target="search",
        old_content="zzz",
        content="BAR",
        occurrence=0,
    )

    assert res["status"] == "error", res
    assert res.get("code") != "OCCURRENCE_OUT_OF_RANGE", res
    assert res["replaced_count"] == 0, res


def test_search_occurrence_dry_run_oor_includes_replaceable(monkeypatch):
    first = MockRange()
    second = MockRange()

    monkeypatch.setattr(
        search_mod,
        "find_all_ranges",
        lambda doc, s: [first, second],
    )
    monkeypatch.setattr(
        search_mod,
        "describe_match_location",
        lambda found, doc, label_cache=None:
            "first" if found is first else "second",
    )
    monkeypatch.setattr(
        search_mod,
        "sweep_draw_shape_preview_matches",
        lambda *args, **kwargs: [],
    )
    monkeypatch.setattr(
        search_mod,
        "sweep_comment_preview_matches",
        lambda *args, **kwargs: [],
    )

    res = ApplyDocumentContent().execute(
        _ctx(),
        target="search",
        old_content="foo",
        content="BAR",
        occurrence=5,
        dry_run=True,
    )

    assert res["status"] == "error", res
    assert res["code"] == "OCCURRENCE_OUT_OF_RANGE", res
    assert res["details"]["replaceable_count"] == 2, res
    assert "matches" not in res["details"], res
    assert res["details"]["replaceable"][0]["occurrence"] == 0, res
    assert res["details"]["replaceable"][1]["location"] == "second", res
    assert res["details"]["not_replaceable"] == [], res
    assert "use 0..1" in res["message"]


def test_dry_run_splits_replaceable_and_not_replaceable(monkeypatch):
    first = MockRange("first text")
    second = MockRange("second text")

    monkeypatch.setattr(
        search_mod,
        "find_all_ranges",
        lambda doc, s: [first, second],
    )
    monkeypatch.setattr(
        search_mod,
        "describe_match_location",
        lambda found, doc, label_cache=None:
            "first_loc" if found is first else "second_loc",
    )
    monkeypatch.setattr(
        search_mod,
        "sweep_draw_shape_preview_matches",
        lambda *args, **kwargs: [
            {"location": "shape 'Box'", "text": "shape text", "context": "extra context"},
        ],
    )
    monkeypatch.setattr(
        search_mod,
        "sweep_comment_preview_matches",
        lambda *args, **kwargs: [
            {"location": "comment by 'Ana'", "text": "comment text", "context": "more context"},
        ],
    )

    # Success dry_run (no occurrence)
    res = ApplyDocumentContent().execute(
        _ctx(),
        target="search",
        old_content="foo",
        content="BAR",
        dry_run=True,
    )

    assert res["status"] == "ok", res
    assert res["dry_run"] is True, res
    assert "matches" not in res, res
    assert res["count"] == 4, res
    assert res["replaceable_count"] == 2, res
    assert res["replaceable"] == [
        {"occurrence": 0, "location": "first_loc", "text": "first text"},
        {"occurrence": 1, "location": "second_loc", "text": "second text"},
    ], res
    assert res["not_replaceable"] == [
        {"location": "shape 'Box'", "text": "shape text"},
        {"location": "comment by 'Ana'", "text": "comment text"},
    ], res

    # Same fixture with occurrence=2 (the shape's old position in matches)
    res_oor = ApplyDocumentContent().execute(
        _ctx(),
        target="search",
        old_content="foo",
        content="BAR",
        occurrence=2,
        dry_run=True,
    )

    assert res_oor["status"] == "error", res_oor
    assert res_oor["code"] == "OCCURRENCE_OUT_OF_RANGE", res_oor
    assert "use 0..1" in res_oor["message"], res_oor
    assert "matches" not in res_oor["details"], res_oor
    assert res_oor["details"]["replaceable_count"] == 2, res_oor
    assert res_oor["details"]["replaceable"] == [
        {"occurrence": 0, "location": "first_loc", "text": "first text"},
        {"occurrence": 1, "location": "second_loc", "text": "second text"},
    ], res_oor
    assert res_oor["details"]["not_replaceable"] == [
        {"location": "shape 'Box'", "text": "shape text"},
        {"location": "comment by 'Ana'", "text": "comment text"},
    ], res_oor


def test_dry_run_shared_preview_cap(monkeypatch):
    body_ranges = [MockRange(f"body {i}") for i in range(19)]
    monkeypatch.setattr(
        search_mod,
        "find_all_ranges",
        lambda doc, s: body_ranges,
    )
    monkeypatch.setattr(
        search_mod,
        "describe_match_location",
        lambda found, doc, label_cache=None: "body",
    )
    monkeypatch.setattr(
        search_mod,
        "sweep_draw_shape_preview_matches",
        lambda *args, **kwargs: [
            {"location": "shape 'Box1'", "text": "shape 1"},
            {"location": "shape 'Box2'", "text": "shape 2"},
        ],
    )
    monkeypatch.setattr(
        search_mod,
        "sweep_comment_preview_matches",
        lambda *args, **kwargs: [
            {"location": "comment by 'Ana'", "text": "comment 1"},
        ],
    )

    res = ApplyDocumentContent().execute(
        _ctx(),
        target="search",
        old_content="foo",
        content="BAR",
        dry_run=True,
    )

    assert res["status"] == "ok", res
    assert res["count"] == 22, res
    assert res["replaceable_count"] == 19, res
    assert len(res["replaceable"]) == 19, res
    assert len(res["not_replaceable"]) == 1, res
    assert res["not_replaceable"][0]["location"] == "shape 'Box1'", res


def test_build_search_replace_response():
    # Single replacement
    single = build_search_replace_response(1)
    assert single["status"] == "ok"
    assert single["replaced_count"] == 1
    assert single["message"] == "Replaced 1 occurrence (by old_content)."

    single_preserve = build_search_replace_response(1, use_preserve=True)
    assert single_preserve["status"] == "ok"
    assert "formatting preserved" in single_preserve["message"]

    # Multiple replacements
    multi = build_search_replace_response(3)
    assert multi["status"] == "ok"
    assert multi["replaced_count"] == 3
    assert multi["message"] == "Replaced 3 occurrence(s)."

    multi_preserve = build_search_replace_response(3, use_preserve=True)
    assert multi_preserve["status"] == "ok"
    assert "formatting preserved" in multi_preserve["message"]


def test_full_document_structured_return_paragraphs_and_headings():
    from unittest.mock import patch

    content = ["<h1>Title</h1>", '<p data-lo-style="Heading2">Section</p>', "<p>Body</p>"]
    with patch("plugin.writer.html_import.replace_full_document") as mock_replace:
        mock_replace.return_value = []
        res = ApplyDocumentContent().execute(_ctx(), content=content, target="full_document")
    assert res["status"] == "ok"
    assert res["message"] == "Replaced entire document."
    assert res["paragraph_count"] == 3
    assert res["headings"] == [
        {"level": 1, "text": "Title"},
        {"level": 2, "text": "Section"},
    ]
    assert "warnings" not in res


def test_full_document_non_heading_style_dropped_from_headings():
    from unittest.mock import patch

    content = '<h1 data-lo-style="Quotations">Title</h1>'
    with patch("plugin.writer.html_import.replace_full_document") as mock_replace:
        mock_replace.return_value = []
        res = ApplyDocumentContent().execute(_ctx(), content=content, target="full_document")
    assert res["status"] == "ok"
    assert res["paragraph_count"] == 1
    assert res["headings"] == []
    assert "warnings" not in res


def test_full_document_empty_content_returns_zero_paragraphs():
    from unittest.mock import patch

    with patch("plugin.writer.html_import.replace_full_document") as mock_replace:
        mock_replace.return_value = []
        res = ApplyDocumentContent().execute(_ctx(), content="", target="full_document")
    assert res["status"] == "ok"
    assert res["paragraph_count"] == 0
    assert res["headings"] == []


def test_full_document_data_lo_para_warnings():
    from unittest.mock import patch

    content = '<p data-lo-para="line-height:150%">Hello</p>'
    with patch("plugin.writer.html_import.replace_full_document") as mock_replace:
        mock_replace.return_value = ["line-height"]
        res = ApplyDocumentContent().execute(_ctx(), content=content, target="full_document")
    assert res["status"] == "ok"
    assert res["paragraph_count"] == 1
    assert res["headings"] == []
    assert res["warnings"] == ["data-lo-para property 'line-height' was not applied"]
