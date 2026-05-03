# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and updates)
"""Unit tests for translate_missing.py review helpers (no HTTP)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import polib
import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import translate_missing as tm  # noqa: E402


def test_libreoffice_gettext_rules_text_covers_extension_context() -> None:
    text = tm._libreoffice_gettext_rules_text()
    assert "LibreOffice" in text
    assert "WriterAgent" in text
    assert "Calc" in text and "Writer" in text


def test_strip_json_fenced_content_plain() -> None:
    assert tm._strip_json_fenced_content('[1]') == "[1]"


def test_strip_json_fenced_content_json_fence() -> None:
    raw = '```json\n[{"a": 1}]\n```'
    assert tm._strip_json_fenced_content(raw) == '[{"a": 1}]'


def test_parse_review_dense_response_positional() -> None:
    content = json.dumps(
        [
            {"index": 1, "action": "ok", "reasoning_en": tm.REVIEW_NO_ERRORS},
            {"index": 2, "action": "suggest", "suggested_msgstr": "b", "reasoning_en": "typo"},
        ]
    )
    out = tm.parse_review_dense_response(content, 2)
    assert len(out) == 2
    assert out[0] is not None and out[0]["action"] == "ok"
    assert out[1] is not None and out[1]["suggested_msgstr"] == "b"


def test_parse_review_dense_response_reordered_by_index() -> None:
    content = json.dumps(
        [
            {"index": 2, "action": "ok", "reasoning_en": tm.REVIEW_NO_ERRORS},
            {"index": 1, "action": "ok", "reasoning_en": tm.REVIEW_NO_ERRORS},
        ]
    )
    out = tm.parse_review_dense_response(content, 2)
    assert out[0] is not None and int(out[0]["index"]) == 1
    assert out[1] is not None and int(out[1]["index"]) == 2


def test_parse_review_dense_response_length_mismatch() -> None:
    content = json.dumps([{"index": 1, "action": "ok", "reasoning_en": tm.REVIEW_NO_ERRORS}])
    out = tm.parse_review_dense_response(content, 2)
    assert out == [None, None]


def test_parse_review_dense_response_invalid_json() -> None:
    out = tm.parse_review_dense_response("not json", 1)
    assert out == [None]


def test_merge_review_dense_ok_forces_no_errors_reason() -> None:
    batch = [
        {"msgid": "OK", "msgstr": "确定", "fuzzy": False, "msgid_plural": None, "msgstr_plural": None},
    ]
    parsed = [{"index": 1, "action": "ok", "reasoning_en": "verbose praise should be dropped"}]
    merged = tm.merge_review_dense(batch, parsed, "zh_CN")
    assert len(merged) == 1
    assert merged[0]["action"] == "ok"
    assert merged[0]["reasoning_en"] == tm.REVIEW_NO_ERRORS


def test_merge_review_dense_suggest() -> None:
    batch = [
        {"msgid": "Hello", "msgstr": "Hola", "fuzzy": False, "msgid_plural": None, "msgstr_plural": None},
    ]
    parsed = [
        {
            "index": 1,
            "action": "suggest",
            "suggested_msgstr": "Hola!",
            "reasoning_en": "More natural",
        }
    ]
    merged = tm.merge_review_dense(batch, parsed, "es")
    assert merged[0]["action"] == "suggest"
    assert merged[0]["suggested_msgstr"] == "Hola!"
    assert merged[0]["reasoning_en"] == "More natural"


def test_default_review_output_path() -> None:
    assert tm.default_review_output_path(["de"]) == "translation_review_de.json"
    assert tm.default_review_output_path(["fr", "de"]) == "translation_review_de_fr.json"


def test_elide_for_terminal() -> None:
    assert tm._elide_for_terminal("hi") == "hi"
    long = "x" * 50
    out = tm._elide_for_terminal(long, max_len=12)
    assert len(out) == 12
    assert out.endswith("…")


def test_print_review_rows_live_ok_is_short(capsys: pytest.CaptureFixture[str]) -> None:
    rows = [
        {
            "locale": "de",
            "msgid": "Save",
            "fuzzy": False,
            "current_msgstr": "Speichern",
            "action": "ok",
            "reasoning_en": tm.REVIEW_NO_ERRORS,
            "suggested_msgstr": None,
        },
    ]
    tm.print_review_rows_live(rows)
    out = capsys.readouterr().out
    assert tm.REVIEW_NO_ERRORS in out
    assert "Save" in out
    assert "Speichern" not in out


def test_print_review_rows_live_suggest(capsys: pytest.CaptureFixture[str]) -> None:
    rows = [
        {
            "locale": "de",
            "msgid": "Open",
            "fuzzy": True,
            "current_msgstr": "Offen",
            "action": "suggest",
            "suggested_msgstr": "Öffnen",
            "reasoning_en": "Wrong word for file open",
        },
    ]
    tm.print_review_rows_live(rows)
    out = capsys.readouterr().out
    assert "Open" in out and "Öffnen" in out and "fuzzy" in out


def test_print_review_rows_live_empty(capsys: pytest.CaptureFixture[str]) -> None:
    tm.print_review_rows_live([])
    assert capsys.readouterr().out.strip() == tm.REVIEW_NO_ERRORS


def test_review_rows_for_json_report_filters_ok() -> None:
    rows = [
        {"action": "ok", "msgid": "a"},
        {"action": "suggest", "msgid": "b"},
        {"action": "error", "msgid": "c"},
    ]
    out = tm.review_rows_for_json_report(rows)
    assert len(out) == 2
    assert {r["msgid"] for r in out} == {"b", "c"}


def test_load_review_report_ok(tmp_path: Path) -> None:
    p = tmp_path / "r.json"
    p.write_text(json.dumps({"suggestions": [], "mode": "review"}), encoding="utf-8")
    d = tm.load_review_report(p)
    assert d["suggestions"] == []


def test_load_review_report_missing_suggestions(tmp_path: Path) -> None:
    p = tmp_path / "bad.json"
    p.write_text("{}", encoding="utf-8")
    with pytest.raises(ValueError, match="suggestions"):
        tm.load_review_report(p)


def test_apply_review_rows_to_po_happy_and_clears_fuzzy(tmp_path: Path) -> None:
    po_path = tmp_path / "writeragent.po"
    po = polib.POFile(wrapwidth=0)
    po.metadata = {"Content-Type": "text/plain; charset=utf-8\n"}
    po.append(polib.POEntry(msgid="Hello", msgstr="Hola", flags=["fuzzy"]))
    po.save(str(po_path))

    rows = [
        {
            "locale": "es",
            "msgid": "Hello",
            "fuzzy": True,
            "current_msgstr": "Hola",
            "msgid_plural": None,
            "current_msgstr_plural": None,
            "action": "suggest",
            "suggested_msgstr": "Hola!",
            "suggested_msgstr_plural": None,
            "reasoning_en": "test",
        }
    ]
    applied, skipped, warns = tm.apply_review_rows_to_po(str(po_path), rows, strict_current=True)
    assert applied == 1
    assert skipped == 0
    assert warns == []

    po2 = polib.pofile(str(po_path))
    ent = next(e for e in po2 if e.msgid == "Hello")
    assert ent.msgstr == "Hola!"
    assert "fuzzy" not in ent.flags


def test_apply_review_rows_to_po_stale_strict_skip(tmp_path: Path) -> None:
    po_path = tmp_path / "writeragent.po"
    po = polib.POFile(wrapwidth=0)
    po.metadata = {"Content-Type": "text/plain; charset=utf-8\n"}
    po.append(polib.POEntry(msgid="Hello", msgstr="Hola"))
    po.save(str(po_path))

    rows = [
        {
            "locale": "es",
            "msgid": "Hello",
            "fuzzy": False,
            "current_msgstr": "OLD",
            "msgid_plural": None,
            "current_msgstr_plural": None,
            "action": "suggest",
            "suggested_msgstr": "Hola!",
            "suggested_msgstr_plural": None,
            "reasoning_en": "test",
        }
    ]
    applied, skipped, warns = tm.apply_review_rows_to_po(str(po_path), rows, strict_current=True)
    assert applied == 0
    assert skipped == 1

    po2 = polib.pofile(str(po_path))
    ent = next(e for e in po2 if e.msgid == "Hello")
    assert ent.msgstr == "Hola"


def test_apply_review_rows_to_po_force_applies(tmp_path: Path) -> None:
    po_path = tmp_path / "writeragent.po"
    po = polib.POFile(wrapwidth=0)
    po.metadata = {"Content-Type": "text/plain; charset=utf-8\n"}
    po.append(polib.POEntry(msgid="Hello", msgstr="Hola"))
    po.save(str(po_path))

    rows = [
        {
            "locale": "es",
            "msgid": "Hello",
            "fuzzy": False,
            "current_msgstr": "OLD",
            "msgid_plural": None,
            "current_msgstr_plural": None,
            "action": "suggest",
            "suggested_msgstr": "Hola!",
            "suggested_msgstr_plural": None,
            "reasoning_en": "test",
        }
    ]
    applied, skipped, warns = tm.apply_review_rows_to_po(str(po_path), rows, strict_current=False)
    assert applied == 1
    assert skipped == 0

    po2 = polib.pofile(str(po_path))
    ent = next(e for e in po2 if e.msgid == "Hello")
    assert ent.msgstr == "Hola!"


def test_apply_review_rows_to_po_plural(tmp_path: Path) -> None:
    po_path = tmp_path / "writeragent.po"
    po = polib.POFile(wrapwidth=0)
    po.metadata = {"Content-Type": "text/plain; charset=utf-8\n"}
    po.append(
        polib.POEntry(
            msgid="one file",
            msgid_plural="%d files",
            msgstr="ein Datei",
            msgstr_plural={"0": "ein Datei", "1": "%d Dateien"},
        )
    )
    po.save(str(po_path))

    rows = [
        {
            "locale": "de",
            "msgid": "one file",
            "fuzzy": False,
            "current_msgstr": "ein Datei",
            "msgid_plural": "%d files",
            "current_msgstr_plural": {"0": "ein Datei", "1": "%d Dateien"},
            "action": "suggest",
            "suggested_msgstr": "eine Datei",
            "suggested_msgstr_plural": {"0": "eine Datei", "1": "%d Dateien"},
            "reasoning_en": "grammar",
        }
    ]

    applied, skipped, warns = tm.apply_review_rows_to_po(str(po_path), rows, strict_current=True)
    assert applied == 1
    assert skipped == 0
    assert warns == []

    po2 = polib.pofile(str(po_path))
    ent = next(e for e in po2 if e.msgid == "one file")
    # Polib round-trips plural catalogs with ``msgstr`` empty; singular form is ``msgstr_plural[0]``.
    pl = ent.msgstr_plural or {}
    assert pl.get(0) == "eine Datei"
    assert pl.get(1) == "%d Dateien"


def test_apply_review_skips_non_suggest_and_empty_suggestion(tmp_path: Path) -> None:
    po_path = tmp_path / "writeragent.po"
    po = polib.POFile(wrapwidth=0)
    po.metadata = {"Content-Type": "text/plain; charset=utf-8\n"}
    po.append(polib.POEntry(msgid="A", msgstr="a"))
    po.append(polib.POEntry(msgid="B", msgstr="b"))
    po.save(str(po_path))

    rows = [
        {
            "locale": "x",
            "msgid": "A",
            "current_msgstr": "a",
            "msgid_plural": None,
            "current_msgstr_plural": None,
            "action": "error",
            "suggested_msgstr": None,
            "suggested_msgstr_plural": None,
            "reasoning_en": "parse error",
        },
        {
            "locale": "x",
            "msgid": "B",
            "current_msgstr": "b",
            "msgid_plural": None,
            "current_msgstr_plural": None,
            "action": "suggest",
            "suggested_msgstr": "   ",
            "suggested_msgstr_plural": None,
            "reasoning_en": "empty",
        },
    ]
    applied, skipped, warns = tm.apply_review_rows_to_po(str(po_path), rows, strict_current=False)
    assert applied == 0
    assert skipped == 2
    assert not warns
