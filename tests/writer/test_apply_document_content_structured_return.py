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
    def getString(self) -> str:
        return "foo"

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


# --- data-lo-para is a read report, not an instruction --------------------------------------


def test_execute_search_plain_content_has_empty_warnings(monkeypatch):
    """Clean document writes return a status ok dict with an empty warnings list."""
    monkeypatch.setattr(search_mod, "find_first_range", lambda doc, s: MockRange())
    res = ApplyDocumentContent().execute(_ctx(), target="search", old_content="foo", content="BAR")
    assert res["status"] == "ok"
    assert res["warnings"] == []


def test_execute_search_dropped_attributes_generates_warnings(monkeypatch):
    """Attributes dropped during write emit structured warnings with suggested tool calls.

    The umbrella data-lo-para warning and specific paragraph property warnings are all emitted
    with suggested_call targeting apply_style, and the legacy note message is appended.
    """
    monkeypatch.setattr(search_mod, "find_first_range", lambda doc, s: MockRange())
    content = '<p data-lo-para="text-align:center; color:#ff0000; margin-left:1cm; text-indent:2cm">x</p>'
    res = ApplyDocumentContent().execute(_ctx(), target="search", old_content="foo", content=content)
    assert res["status"] == "ok"
    assert "ignored_attributes" not in res
    assert "apply_style" in res["message"]

    expected_codes = {
        "data_lo_para_ignored",
        "paragraph_alignment_ignored",
        "paragraph_color_ignored",
        "paragraph_margin_ignored",
        "paragraph_indent_ignored",
    }
    actual_codes = {w["code"] for w in res["warnings"]}
    assert actual_codes == expected_codes

    for w in res["warnings"]:
        assert "attribute" in w
        assert "message" in w
        assert w.get("suggested_call", {}).get("tool") == "apply_style"


def test_attach_write_warnings_body_text_mentioning_data_lo_para():
    """Body text that mentions data-lo-para is content, not an attribute, so no warning is emitted."""
    from plugin.writer.html_import_styles import attach_write_warnings

    res = {"status": "ok", "message": "done"}
    out = attach_write_warnings(res, "<p>The words data-lo-para are in text.</p>", styles_applied=True)
    assert out["warnings"] == []


def test_attach_write_warnings_html_comment_is_ignored():
    """An HTML comment containing markup does not trigger dropped attribute warnings."""
    from plugin.writer.html_import_styles import attach_write_warnings

    res = {"status": "ok"}
    out = attach_write_warnings(res, '<!-- <p data-lo-para="text-align:center">ignored</p> -->', styles_applied=False)
    assert out["warnings"] == []


def test_attach_write_warnings_error_result_untouched():
    """Failed operations return the error dictionary untouched without adding a warnings key."""
    from plugin.writer.html_import_styles import attach_write_warnings

    original = {"status": "error", "message": "match not found"}
    out = attach_write_warnings(original, '<p data-lo-para="margin:1cm">x</p>', styles_applied=False)
    assert out == original
    assert "warnings" not in out


def test_attach_write_warnings_applied_styles_and_inline_style():
    """When styles_applied is True, standard block styles and inline char overrides produce no warnings."""
    from plugin.writer.html_import_styles import attach_write_warnings

    res = {"status": "ok"}
    content = '<p data-lo-style="Caption">text <span style="color:#ff0000">red</span></p>'
    out = attach_write_warnings(res, content, styles_applied=True)
    assert out["warnings"] == []


def test_attach_write_warnings_table_cell_style_ignored():
    """Styles inside tables cannot be applied by block style passes, producing table_cell_style_ignored."""
    from plugin.writer.html_import_styles import attach_write_warnings

    res = {"status": "ok"}
    content = '<table><tr><td><p data-lo-style="Heading1">c</p></td></tr></table>'
    out = attach_write_warnings(res, content, styles_applied=True)
    assert len(out["warnings"]) == 1
    assert out["warnings"][0]["code"] == "table_cell_style_ignored"


def test_attach_write_warnings_styles_not_applied_flags_data_lo_style():
    """When styles_applied is False, data-lo-style is not applied and reports data_lo_style_ignored."""
    from plugin.writer.html_import_styles import attach_write_warnings

    res = {"status": "ok"}
    content = '<p data-lo-style="Heading1">heading</p>'
    out = attach_write_warnings(res, content, styles_applied=False)
    assert len(out["warnings"]) == 1
    assert out["warnings"][0]["code"] == "data_lo_style_ignored"


def test_attach_write_warnings_non_block_tag_style_ignored():
    """Inline tags like span cannot receive paragraph styles, emitting data_lo_style_ignored."""
    from plugin.writer.html_import_styles import attach_write_warnings

    res = {"status": "ok"}
    content = '<span data-lo-style="Emphasis">text</span>'
    out = attach_write_warnings(res, content, styles_applied=True)
    assert len(out["warnings"]) == 1
    assert out["warnings"][0]["code"] == "data_lo_style_ignored"


def test_attach_write_warnings_self_closing_tag_reported_once():
    """Self-closing tags are parsed exactly once rather than duplicating warnings across start and end."""
    from plugin.writer.html_import_styles import attach_write_warnings

    res = {"status": "ok"}
    content = '<p data-lo-para="text-indent:1cm"/>'
    out = attach_write_warnings(res, content, styles_applied=False)
    indent_warnings = [w for w in out["warnings"] if w["code"] == "paragraph_indent_ignored"]
    assert len(indent_warnings) == 1


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
