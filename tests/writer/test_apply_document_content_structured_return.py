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

_real_content_has_markup = format_mod.content_has_markup
_real_normalize_search_string_for_find = search_mod.normalize_search_string_for_find


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
        "test_apply_document_content_anchor_uno.py",
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
    assert res["normalized_old_content"] == "zzz", res


def test_search_single_success(monkeypatch):
    monkeypatch.setattr(search_mod, "find_first_range", lambda doc, s: MockRange())
    res = ApplyDocumentContent().execute(_ctx(), target="search", old_content="foo", content="BAR")
    assert res["status"] == "ok", res
    assert res["replaced_count"] == 1, res
    assert res["normalized_old_content"] == "foo", res


def test_search_all_matches_reports_count(monkeypatch):
    monkeypatch.setattr(search_mod, "find_all_ranges", lambda doc, s: [MockRange(), MockRange(), MockRange()])
    res = ApplyDocumentContent().execute(
        _ctx(), target="search", old_content="foo", content="BAR", all_matches=True)
    assert res["status"] == "ok", res
    assert res["replaced_count"] == 3, res
    assert res["normalized_old_content"] == "foo", res


def test_search_all_matches_no_match_errors(monkeypatch):
    monkeypatch.setattr(search_mod, "find_all_ranges", lambda doc, s: [])
    res = ApplyDocumentContent().execute(
        _ctx(), target="search", old_content="zzz", content="BAR", all_matches=True)
    assert res["status"] == "error", res
    assert res["replaced_count"] == 0, res
    assert res["normalized_old_content"] == "zzz", res
    assert res["message"].startswith("Replaced 0 occurrence"), res


def test_search_tagged_needle_normalized(monkeypatch):
    monkeypatch.setattr(format_mod, "content_has_markup", _real_content_has_markup)
    monkeypatch.setattr(search_mod, "normalize_search_string_for_find", _real_normalize_search_string_for_find)

    captured = {}

    def fake_find(doc, s):
        captured["needle"] = s
        return MockRange()

    monkeypatch.setattr(search_mod, "find_first_range", fake_find)
    res = ApplyDocumentContent().execute(
        _ctx(), target="search", old_content="<p>Hello <b>World</b></p>", content="BAR"
    )
    assert res["status"] == "ok", res
    assert captured["needle"] == "Hello World"
    assert res["normalized_old_content"] == "Hello World"


def test_search_oversized_old_content_refused_on_miss(monkeypatch):
    monkeypatch.setattr(search_mod, "find_first_range", lambda doc, s: None)
    monkeypatch.setattr(search_mod, "old_content_covers_most", lambda doc, n: True)
    res = ApplyDocumentContent().execute(
        _ctx(), target="search", old_content="huge content", content="BAR"
    )
    assert res["status"] == "error", res
    assert res["replaced_count"] == 0, res
    assert res["normalized_old_content"] == "huge content"
    assert "target='full_document'" in res["message"]


def test_search_oversized_old_content_never_blocks_match(monkeypatch):
    monkeypatch.setattr(search_mod, "find_first_range", lambda doc, s: MockRange())
    monkeypatch.setattr(search_mod, "old_content_covers_most", lambda doc, n: True)
    res = ApplyDocumentContent().execute(
        _ctx(), target="search", old_content="huge content", content="BAR"
    )
    assert res["status"] == "ok", res
    assert res["replaced_count"] == 1, res
    assert res["normalized_old_content"] == "huge content"


def test_search_oversized_old_content_with_near_miss_reports_hint_not_refusal(monkeypatch):
    monkeypatch.setattr(search_mod, "find_first_range", lambda doc, s: None)
    monkeypatch.setattr(search_mod, "old_content_covers_most", lambda doc, n: True)
    monkeypatch.setattr(
        search_mod,
        "search_closest_mismatch",
        lambda doc, s, lenient=True: {
            "closest_match": "huge content…",
            "diff_at": 10,
            "message": "closest match starts 'huge content…', differs at character 10: x vs y",
        },
    )
    res = ApplyDocumentContent().execute(
        _ctx(), target="search", old_content="huge content", content="BAR"
    )
    assert res["status"] == "error", res
    assert "target='full_document'" not in res["message"]
    assert "differs at character 10" in res["message"]
    assert res["closest_match"] == "huge content…"
    assert res["diff_at"] == 10


def test_dry_run_oversized_retains_dry_run_keys(monkeypatch):
    monkeypatch.setattr(search_mod, "find_all_ranges", lambda doc, s: [])
    monkeypatch.setattr(search_mod, "sweep_draw_shape_preview_matches", lambda *a, **k: [])
    monkeypatch.setattr(search_mod, "sweep_comment_preview_matches", lambda *a, **k: [])
    monkeypatch.setattr(search_mod, "old_content_covers_most", lambda doc, n: True)
    res = ApplyDocumentContent().execute(
        _ctx(), target="search", old_content="huge content", content="BAR", dry_run=True
    )
    assert res["status"] == "ok", res
    assert res["dry_run"] is True
    assert res["count"] == 0
    assert res["replaceable_count"] == 0
    assert res["replaceable"] == []
    assert res["not_replaceable"] == []
    assert "target='full_document'" in res["message"]
    assert res["normalized_old_content"] == "huge content"


def test_search_empty_after_normalization_reports_normalized_key():
    res = ApplyDocumentContent().execute(
        _ctx(), target="search", old_content="   ", content="BAR"
    )
    assert res["status"] == "error", res
    assert res["normalized_old_content"] == ""
    assert "empty after normalization" in res["message"]


def test_dry_run_reports_normalized_old_content_and_miss_fields(monkeypatch):
    monkeypatch.setattr(search_mod, "find_all_ranges", lambda doc, s: [])
    monkeypatch.setattr(search_mod, "sweep_draw_shape_preview_matches", lambda *a, **k: [])
    monkeypatch.setattr(search_mod, "sweep_comment_preview_matches", lambda *a, **k: [])
    monkeypatch.setattr(
        search_mod,
        "search_closest_mismatch",
        lambda doc, s, lenient=True: {
            "closest_match": "The revenue grew…",
            "diff_at": 17,
            "message": "closest match starts 'The revenue grew…', differs at character 17: f vs l",
        },
    )
    res = ApplyDocumentContent().execute(
        _ctx(), dry_run=True, old_content="The revenue grew fast", content="BAR"
    )
    assert res["status"] == "ok", res
    assert res["dry_run"] is True
    assert res["replaceable_count"] == 0
    assert res["normalized_old_content"] == "The revenue grew fast"
    assert res["closest_match"] == "The revenue grew…"
    assert res["diff_at"] == 17
    assert "closest match starts 'The revenue grew…', differs at character 17: f vs l" in res["message"]


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


def test_build_search_replace_response_custom_message():
    resp = build_search_replace_response(1, message="Replaced the range from start_anchor through end_anchor.")
    assert resp["status"] == "ok"
    assert resp["replaced_count"] == 1
    assert resp["message"] == "Replaced the range from start_anchor through end_anchor."

    resp_preserve = build_search_replace_response(
        1, use_preserve=True, message="Replaced the range from start_anchor through end_anchor."
    )
    assert resp_preserve["status"] == "ok"
    assert resp_preserve["replaced_count"] == 1
    assert resp_preserve["message"] == "Replaced the range from start_anchor through end_anchor. (formatting preserved)"


def test_search_target_requires_old_content_or_anchors():
    tool = ApplyDocumentContent()
    res = tool.execute(_ctx(), target="search", content="new")
    assert res["status"] == "error"
    assert res["message"] == "target='search' requires old_content or start_anchor and end_anchor."


def test_anchor_both_required():
    tool = ApplyDocumentContent()
    res1 = tool.execute(_ctx(), start_anchor="foo", content="new")
    assert res1["status"] == "error"
    assert "start_anchor and end_anchor are both required" in res1["message"]

    res2 = tool.execute(_ctx(), end_anchor="bar", content="new")
    assert res2["status"] == "error"
    assert "start_anchor and end_anchor are both required" in res2["message"]


def test_anchor_non_strings():
    tool = ApplyDocumentContent()
    res1 = tool.execute(_ctx(), start_anchor=123, end_anchor="bar", content="new")
    assert res1["status"] == "error"
    assert "start_anchor and end_anchor must both be strings" in res1["message"]

    res2 = tool.execute(_ctx(), start_anchor="foo", end_anchor=456, content="new")
    assert res2["status"] == "error"
    assert "start_anchor and end_anchor must both be strings" in res2["message"]


def test_anchor_empty_after_normalization(monkeypatch):
    monkeypatch.setattr(search_mod, "normalize_search_string_for_find", lambda s: s.strip())
    tool = ApplyDocumentContent()
    res1 = tool.execute(_ctx(), start_anchor="   ", end_anchor="bar", content="new")
    assert res1["status"] == "error"
    assert res1["message"] == "start_anchor is empty after normalization."

    res2 = tool.execute(_ctx(), start_anchor="foo", end_anchor="   ", content="new")
    assert res2["status"] == "error"
    assert res2["message"] == "end_anchor is empty after normalization."


def test_anchor_conflict_with_old_content():
    tool = ApplyDocumentContent()
    res = tool.execute(_ctx(), old_content="x", start_anchor="foo", end_anchor="bar", content="new")
    assert res["status"] == "error"
    assert res["message"] == "Pass old_content or start_anchor and end_anchor, not both."


def test_anchor_non_search_target():
    tool = ApplyDocumentContent()
    res = tool.execute(_ctx(), target="full_document", start_anchor="foo", end_anchor="bar", content="new")
    assert res["status"] == "error"
    assert res["message"] == "start_anchor and end_anchor require target='search'."


def test_anchor_rejects_regex_and_all_matches_and_position_and_dry_run():
    tool = ApplyDocumentContent()
    res_regex = tool.execute(_ctx(), start_anchor="foo", end_anchor="bar", content="new", regex=True)
    assert res_regex["status"] == "error"
    assert res_regex["message"] == "regex does not apply to start_anchor and end_anchor."

    res_all = tool.execute(_ctx(), start_anchor="foo", end_anchor="bar", content="new", all_matches=True)
    assert res_all["status"] == "error"
    assert res_all["message"] == "all_matches cannot be combined with start_anchor and end_anchor."

    res_pos = tool.execute(_ctx(), start_anchor="foo", end_anchor="bar", content="new", position="before")
    assert res_pos["status"] == "error"
    assert res_pos["message"] == "position='before'/'after' does not apply to an anchor range."

    res_dry = tool.execute(_ctx(), start_anchor="foo", end_anchor="bar", content="new", dry_run=True)
    assert res_dry["status"] == "error"
    assert res_dry["message"] == "dry_run does not apply to start_anchor and end_anchor."

    res_url = tool.execute(
        _ctx(), start_anchor="foo", end_anchor="bar", content="new", hyperlink_url="http://example.com"
    )
    assert res_url["status"] == "error"
    assert res_url["message"] == "hyperlink_url does not apply to start_anchor and end_anchor."


def test_anchor_omitted_target_and_success_messages(monkeypatch):
    monkeypatch.setattr(search_mod, "find_anchor_span", lambda doc, s, e, **k: (MockRange(), None))
    tool = ApplyDocumentContent()

    # Plain text:
    res_plain = tool.execute(_ctx(), start_anchor="foo", end_anchor="bar", content="new")
    assert res_plain["status"] == "ok"
    assert res_plain["replaced_count"] == 1
    assert res_plain["message"] == "Replaced the range from start_anchor through end_anchor."

    # HTML path (use_preserve=False):
    monkeypatch.setattr(format_mod, "content_has_markup", lambda *a, **k: True)
    res_html = tool.execute(_ctx(), target="search", start_anchor="foo", end_anchor="bar", content="<p>new</p>")
    assert res_html["status"] == "ok"
    assert res_html["replaced_count"] == 1
    assert res_html["message"] == "Replaced the range from start_anchor through end_anchor."


def test_anchor_miss_start(monkeypatch):
    monkeypatch.setattr(search_mod, "find_anchor_span", lambda doc, s, e, **k: (None, "start"))
    tool = ApplyDocumentContent()
    res = tool.execute(_ctx(), start_anchor="foo", end_anchor="bar", content="new")
    assert res["status"] == "error"
    assert res["replaced_count"] == 0
    assert "start_anchor not found in document." in res["message"]


def test_anchor_miss_end(monkeypatch):
    monkeypatch.setattr(search_mod, "find_anchor_span", lambda doc, s, e, **k: (None, "end"))
    tool = ApplyDocumentContent()
    res = tool.execute(_ctx(), start_anchor="foo", end_anchor="bar", content="new")
    assert res["status"] == "error"
    assert res["replaced_count"] == 0
    assert "end_anchor not found after start_anchor." in res["message"]


def test_anchor_miss_different_text(monkeypatch):
    monkeypatch.setattr(search_mod, "find_anchor_span", lambda doc, s, e, **k: (None, "different_text"))
    tool = ApplyDocumentContent()
    res = tool.execute(_ctx(), start_anchor="foo", end_anchor="bar", content="new")
    assert res["status"] == "error"
    assert res["replaced_count"] == 0
    assert "end_anchor after start_anchor is not in the same text, so it cannot be one range." in res["message"]


def test_anchor_shape_miss_does_not_replace_text_in_shape(monkeypatch):
    from unittest.mock import MagicMock
    import plugin.writer.apply_document_content as apply_mod

    monkeypatch.setattr(search_mod, "find_anchor_span", lambda doc, s, e, **k: (None, "start"))
    mock_shape = MagicMock()
    monkeypatch.setattr(search_mod, "drawing_shape_object_containing", lambda doc, text, **k: mock_shape)
    monkeypatch.setattr(search_mod, "shape_display_name", lambda shape: "MyBox")
    replace_shape_called = False

    def _replace_shape(*args, **kwargs):
        nonlocal replace_shape_called
        replace_shape_called = True
        return True

    monkeypatch.setattr(apply_mod, "replace_text_in_shape", _replace_shape)
    tool = ApplyDocumentContent()
    res = tool.execute(_ctx(), start_anchor="foo", end_anchor="bar", content="new")
    assert res["status"] == "error"
    assert res["replaced_count"] == 0
    assert "start_anchor is only inside a drawing shape / floating text box ('MyBox')" in res["message"]
    assert not replace_shape_called


def test_anchor_occurrence_index_error(monkeypatch):
    def _raise_oor(*args, **kwargs):
        raise IndexError("occurrence 2 out of range (found 1 match(es), use 0..0).")

    monkeypatch.setattr(search_mod, "find_anchor_span", _raise_oor)
    tool = ApplyDocumentContent()
    res = tool.execute(_ctx(), start_anchor="foo", end_anchor="bar", occurrence=2, content="new")
    assert res["status"] == "error"
    assert res["code"] == "OCCURRENCE_OUT_OF_RANGE"
    assert "occurrence 2 out of range" in res["message"]


def test_anchor_span_containing_table_refused(monkeypatch):
    table_elem = MagicMock()
    table_elem.supportsService.side_effect = lambda s: s == "com.sun.star.text.TextTable"
    enum = MagicMock()
    enum.hasMoreElements.side_effect = [True, False]
    enum.nextElement.return_value = table_elem
    mock_range = MockRange()
    mock_range.createEnumeration = lambda: enum

    monkeypatch.setattr(search_mod, "find_anchor_span", lambda doc, s, e, **k: (mock_range, None))
    tool = ApplyDocumentContent()
    res = tool.execute(_ctx(), start_anchor="foo", end_anchor="bar", content="new")
    assert res["status"] == "error"
    assert res["code"] == "TABLE_IN_ANCHOR_RANGE"
    assert "table_delete" in res["message"]


def test_anchor_plain_text_wraps_in_p_and_skips_hyperlinks(monkeypatch):
    from unittest.mock import patch

    monkeypatch.setattr(search_mod, "find_anchor_span", lambda doc, s, e, **k: (MockRange(), None))
    recorded_content = []
    monkeypatch.setattr(
        format_mod,
        "replace_single_range_with_content",
        lambda doc, found, content, ctx: recorded_content.append(content),
    )
    with patch("plugin.writer.apply_document_content.capture_outline_hyperlinks") as mock_capture:
        res = ApplyDocumentContent().execute(
            _ctx(),
            start_anchor="start",
            end_anchor="end",
            content="New paragraph one\nNew paragraph two",
        )
    assert res["status"] == "ok"
    assert recorded_content == ["<p>New paragraph one</p>\n<p>New paragraph two</p>"]
    mock_capture.assert_not_called()
    assert "hyperlinks" not in res


def test_anchor_markup_not_wrapped_again(monkeypatch):
    monkeypatch.setattr(format_mod, "content_has_markup", _real_content_has_markup)
    monkeypatch.setattr(search_mod, "find_anchor_span", lambda doc, s, e, **k: (MockRange(), None))
    recorded_content = []
    monkeypatch.setattr(
        format_mod,
        "replace_single_range_with_content",
        lambda doc, found, content, ctx: recorded_content.append(content),
    )
    res = ApplyDocumentContent().execute(
        _ctx(),
        start_anchor="start",
        end_anchor="end",
        content="<p>already</p>",
    )
    assert res["status"] == "ok"
    assert recorded_content == ["<p>already</p>"]


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
