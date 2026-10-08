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

import plugin.writer.format as format_mod
import plugin.writer.search as search_mod
from plugin.writer.content import ApplyDocumentContent


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
    def getString(self) -> str:
        return "foo"


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


# --- data-lo-para is a read report, not an instruction --------------------------------------


def test_read_only_attribute_sent_back_is_flagged():
    """The write path drops data-lo-para. Say so: a silent no-op is the failure this tool's
    callers already get bitten by."""
    from plugin.writer.content import _note_read_only_attrs

    res = _note_read_only_attrs(
        {"status": "ok", "message": "Replaced entire document."},
        ['<p data-lo-para="margin-left:3.25cm">A quoted clause.</p>'])

    assert res["ignored_attributes"] == ["data-lo-para"]
    assert "apply_style" in res["message"]


@pytest.mark.parametrize(
    "value",
    [
        pytest.param("<p>A quoted clause.</p>", id="test_content_without_the_attribute_is_untouched"),
        # Substring 'data-lo-para' in body text is not the attribute; do not flag ignored_attributes.
        pytest.param("<p>The words data-lo-para describe a read-only report.</p>", id="test_body_text_mentioning_data_lo_para_is_not_a_false_positive"),
    ],
)
def test_content_without_the_attribute_is_untouched(value):
    from plugin.writer.content import _note_read_only_attrs

    original = {"status": "ok", "message": "Replaced entire document."}
    res = _note_read_only_attrs(original, [value])

    assert res == original
    assert "ignored_attributes" not in res


def test_failed_write_is_not_annotated():
    """Nothing was written, so there is nothing to say about what was ignored."""
    from plugin.writer.content import _note_read_only_attrs

    original = {"status": "error", "message": "old_content not found."}

    assert _note_read_only_attrs(original, ['<p data-lo-para="margin-left:3cm">x</p>']) == original


def test_plain_string_content_is_accepted():
    """content is normally a list, but the checker must not choke on a bare string."""
    from plugin.writer.content import _note_read_only_attrs

    res = _note_read_only_attrs({"status": "ok"}, '<p data-lo-para="text-align:center">x</p>')

    assert res["ignored_attributes"] == ["data-lo-para"]

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
        "plugin.writer.content.record_preserve_replace",
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
    assert res["matches"][0]["occurrence"] == 0, res
    assert res["matches"][1]["occurrence"] == 1, res


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
        "plugin.writer.content.record_preserve_replace",
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
    monkeypatch.setattr("plugin.writer.content.collapsed_anchor", lambda found: None)
    monkeypatch.setattr(format_mod, "html_fragment_contains_mixed_math", lambda content: False)
    monkeypatch.setattr(format_mod, "content_has_markup", lambda *args, **kwargs: True)
    inserted = []
    monkeypatch.setattr(
        format_mod,
        "insert_html_at_cursor",
        lambda *args, **kwargs: inserted.append(True),
    )
    monkeypatch.setattr(
        "plugin.writer.content.record_html_atomically",
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


def test_search_occurrence_dry_run_oor_includes_matches(monkeypatch):
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
    assert res["details"]["matches"][0]["occurrence"] == 0, res
    assert res["details"]["matches"][1]["location"] == "second", res
    assert "use 0..1" in res["message"]
