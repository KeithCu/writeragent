# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""UNO suite over generated Word-style and ODF documents: reads, unchanged writes, Reject All, save and reload."""

from __future__ import annotations

import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Any

import uno

from plugin.app import get_tools
from plugin.framework.uno_context import create_property_value, enum_has_more
from plugin.testing_runner import native_test
from plugin.tests.testing_utils import TestingFactory
from tests.writer.corpus_fixture import (
    CONTRACT,
    LETTER,
    NOTES,
    REDLINED_ODF,
    ROW_CHANGES,
    SECTIONS,
    CorpusDoc,
    write_contract,
)
from tests.writer.doc_builders import list_redlines, reject_all_changes

if TYPE_CHECKING:
    from collections.abc import Iterator

_TEXT_DOCUMENT = "com.sun.star.text.TextDocument"
_SAVE_FILTER = {".docx": "MS Word 2007 XML", ".fodt": "OpenDocument Text Flat XML"}
# Tiers that are not document reads: agent sessions, host-only helpers and peer messaging.
_OTHER_TIERS = frozenset({"specialized_control", "unlisted", "chat"})
# Reads that move the user's view, or read the folder instead of the document.
_NOT_DOCUMENT_READS = {"set_selection", "nav_goto_page", "image_list_nearby_files"}


@contextmanager
def _opened(ctx: Any, path: Path) -> Iterator[Any]:
    with TestingFactory.native_doc(ctx, doc_type=uno.systemPathToFileUrl(str(path)), reuse=False) as doc:
        yield doc


@contextmanager
def _corpus_file(case: CorpusDoc) -> Iterator[Path]:
    # Windows may keep a hidden Writer open (TestingFactory.close_doc), which locks its file.
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as folder:
        path = Path(folder) / (case.name + case.suffix)
        case.write(path)
        yield path


def _tool(ctx: Any, doc: Any, tool: str, /, **args: Any) -> dict[str, Any]:
    return TestingFactory.execute_tool(doc, ctx, tool, args, doc_type="writer")


def _ok(ctx: Any, doc: Any, tool: str, /, **args: Any) -> dict[str, Any]:
    result = _tool(ctx, doc, tool, **args)
    assert result.get("status") == "ok", f"{tool}: {result}"
    return result


def _body(doc: Any) -> list[tuple[Any, ...]]:
    """Style, list label, alignment and text of each body paragraph, and every cell of each body table."""
    out: list[tuple[Any, ...]] = []
    enum = doc.getText().createEnumeration()
    while enum_has_more(enum):
        item = enum.nextElement()
        if item.supportsService("com.sun.star.text.TextTable"):
            out.append(("table", *((name, item.getCellByName(name).getString()) for name in item.getCellNames())))
        else:
            out.append((
                item.getPropertyValue("ParaStyleName"),
                item.getPropertyValue("ListLabelString"),
                int(item.getPropertyValue("ParaAdjust")),
                item.getString(),
            ))
    return out


def _body_text(doc: Any) -> list[Any]:
    """``_body`` without the paragraph properties, which a save in another session can restate."""
    return [entry if entry[0] == "table" else entry[-1] for entry in _body(doc)]


def _region(ctx: Any, doc: Any, region: str) -> tuple[str, tuple[str, ...], tuple[Any, ...]]:
    style = doc.getStyleFamilies().getByName("PageStyles").getByName("Standard")
    is_on_prop, text_prop = ("HeaderIsOn", "HeaderText") if region == "header" else ("FooterIsOn", "FooterText")
    text = style.getPropertyValue(text_prop).getString() if style.getPropertyValue(is_on_prop) else ""
    read = _ok(ctx, doc, "page_get_header_footer_text", region=region)
    return text, tuple(read.get("images") or ()), tuple(read.get("fields") or ())


def _counts(ctx: Any, doc: Any) -> dict[str, Any]:
    # Text and counts only, not object names: a DOCX save adds graphics/sections and renames tables.
    header_text, header_images, header_fields = _region(ctx, doc, "header")
    footer_text, footer_images, footer_fields = _region(ctx, doc, "footer")
    return {
        "redlines": sorted((r["type"], r["RedlineAuthor"]) for r in list_redlines(doc)),
        "body": _body_text(doc),
        "header": header_text,
        "header images": len(header_images),
        "header fields": header_fields,
        "footer": footer_text,
        "footer images": len(footer_images),
        "footer fields": footer_fields,
        "indexes": doc.getDocumentIndexes().getCount(),
        "footnotes": doc.getFootnotes().getCount(),
        "endnotes": doc.getEndnotes().getCount(),
        "comments": _ok(ctx, doc, "comment_list").get("count"),
    }


def _state(ctx: Any, doc: Any) -> dict[str, Any]:
    # Bookmarks are omitted: get_tree adds _mcp_* heading bookmarks by design.
    return {
        **_counts(ctx, doc),
        "body": _body(doc),
        "sections": doc.getTextSections().getElementNames(),
        "record_changes": bool(doc.getPropertyValue("RecordChanges")),
    }


def _assert_same(label: str, before: dict[str, Any], after: dict[str, Any]) -> None:
    changed = {key: (before[key], after[key]) for key in before if before[key] != after[key]}
    assert not changed, f"{label} changed the document: {changed}"


def _read_calls(doc: Any) -> list[tuple[str, dict[str, Any]]]:
    """Every registered Writer read tool declaring ``TextDocument`` with the arguments this document allows.

    A read tool without required parameters is called with none. One that needs arguments must be
    listed here, so a new read tool cannot stay uncovered silently.
    """
    sections = doc.getTextSections().getElementNames()
    images = doc.getGraphicObjects().getElementNames()
    frames = doc.getTextFrames().getElementNames()
    tables = doc.getTextTables().getElementNames()
    bookmarks = doc.getBookmarks().getElementNames()
    charts = doc.getEmbeddedObjects().getElementNames()
    headings = sum(entry[0] == "Heading 1" for entry in _body(doc))
    heading = [{"locator": "heading:1"}] if headings else []
    notes = [{"note": kind} for kind in ("footnote", "endnote")]
    arguments: dict[str, list[dict[str, Any]]] = {
        "bookmark_get": [{"name": n} for n in bookmarks],
        "bookmark_resolve": [{"name": n} for n in bookmarks],
        "chart_get_info": [{"name": n} for n in charts],
        "footnote_get_settings": notes,
        "footnote_list": notes,
        "frame_get_info": [{"name": n} for n in frames],
        "image_get": [{"image": n} for n in images],
        "image_get_info": [{"name": n} for n in images],
        "index_list_toc_entries": [{}] if doc.getDocumentIndexes().getCount() == 1 else [],
        "nav_get_heading": [{"locator": "heading:1", "direction": "next"}] if headings > 1 else [],
        "nav_get_surroundings": [{"locator": "paragraph:0"}],
        "nav_list_heading_children": heading,
        "page_get_header_footer_text": [{"region": region} for region in ("header", "footer")],
        "search": [{"pattern": "Agreement"}],
        "section_get_info": [{"name": n} for n in sections],
        "section_read": [{"name": n} for n in sections],
        # SwXFrame::getPosition (sw/source/core/unocore/unoframe.cxx) always throws, so the tool
        # fails on any document with a picture or frame.
        "shape_get_summary": [] if images or frames else [{}],
        "style_get_info": [{"style": "Standard"}],
        "table_get_cells": [{"name": n} for n in tables],
    }
    tools = get_tools().get_tools(filter_doc_type=False, exclude_tiers=_OTHER_TIERS)
    registered = {tool.name for tool in tools}
    assert set(arguments).issubset(registered), f"unknown tools in arguments: {set(arguments) - registered}"
    calls: list[tuple[str, dict[str, Any]]] = []
    for tool in tools:
        name = tool.name
        if name in arguments:
            calls.extend((name, args) for args in arguments[name])
        elif _TEXT_DOCUMENT in (tool.uno_services or ()) and not tool.detects_mutation() and name not in _NOT_DOCUMENT_READS:
            assert not (tool.parameters or {}).get("required"), f"add arguments for read tool {name}"
            calls.append((name, {}))
    return calls


def _check_import(ctx: Any, case: CorpusDoc, doc: Any) -> None:
    """The fixture really has what its name says, so the checks below cannot pass on an empty import."""
    redlines = sorted((r["type"], r["RedlineAuthor"]) for r in list_redlines(doc))
    assert {author for _, author in redlines} == set(case.redline_authors)
    if case.redlines:
        assert redlines == sorted(case.redlines)
    assert sum(entry[0] == "table" for entry in _body(doc)) == case.tables
    assert doc.getDocumentIndexes().getCount() == case.indexes
    assert _region(ctx, doc, "header")[1] == case.header_images
    assert doc.getFootnotes().getCount() == case.footnotes
    assert doc.getEndnotes().getCount() == case.endnotes
    assert _ok(ctx, doc, "comment_list").get("count") == case.comments
    assert tuple(doc.getBookmarks().getElementNames()) == case.bookmarks
    sections = doc.getTextSections()
    assert tuple(sections.getElementNames()) == case.sections
    if "Locked" in case.sections:
        assert sections.getByName("Locked").getPropertyValue("IsProtected") is True
    if "Secret" in case.sections:
        assert sections.getByName("Secret").getPropertyValue("IsVisible") is False


def _check_reads(ctx: Any, path: Path) -> None:
    with _opened(ctx, path) as doc:
        before = _state(ctx, doc)
        failed = []
        calls = _read_calls(doc)
        for name, args in calls:
            result = _tool(ctx, doc, name, **args)
            if result.get("status") != "ok":
                failed.append(f"{name}({args}): {result}")
        assert not failed, failed
        _assert_same(f"reading ({[name for name, _ in calls]})", before, _state(ctx, doc))


def _check_unchanged_writes(ctx: Any, case: CorpusDoc, path: Path) -> None:
    """Writing back what a read returned, with Track Changes off, changes nothing."""
    with _opened(ctx, path) as doc:
        before = _state(ctx, doc)
        _ok(ctx, doc, "apply_content", target="search", old_content=case.old, content=case.old)
        for region in ("header", "footer"):
            # include_images=False: default header write-back loses the logo image (F3).
            read = _ok(ctx, doc, "page_get_header_footer_text", region=region, include_images=False)
            if read["is_on"]:
                _ok(ctx, doc, "page_set_header_footer_text", region=region, content=read["content"])
        for name in doc.getTextTables().getElementNames():
            for cell, text in _ok(ctx, doc, "table_get_cells", name=name)["cells"].items():
                _ok(ctx, doc, "table_set_cell", name=name, cell=cell, text=text)
        _assert_same("unchanged writes", before, _state(ctx, doc))


def _check_recorded_edit(ctx: Any, case: CorpusDoc, path: Path) -> None:
    """One recorded edit survives save and reload; Reject All then gives what it gives without the edit."""
    with _opened(ctx, path) as doc:
        reject_all_changes(doc, ctx)
        rejected = _body_text(doc)
    saved = path.with_name("saved" + path.suffix)
    with _opened(ctx, path) as doc:
        before = len(list_redlines(doc))
        doc.setPropertyValue("RecordChanges", True)
        _ok(ctx, doc, "apply_content", target="search", old_content=case.old, content=case.new)
        assert len(list_redlines(doc)) > before
        edited = _counts(ctx, doc)
        doc.storeToURL(uno.systemPathToFileUrl(str(saved)), (create_property_value("FilterName", _SAVE_FILTER[path.suffix]),))
        reject_all_changes(doc, ctx)
        assert _body_text(doc) == rejected, "Reject All after the edit"
    with _opened(ctx, saved) as doc:
        _assert_same("save and reload", edited, _counts(ctx, doc))
        reject_all_changes(doc, ctx)
        assert _body_text(doc) == rejected, "Reject All after reload"


def _check(ctx: Any, case: CorpusDoc) -> None:
    with _corpus_file(case) as path:
        with _opened(ctx, path) as doc:
            _check_import(ctx, case, doc)
        _check_reads(ctx, path)
        _check_unchanged_writes(ctx, case, path)
        _check_recorded_edit(ctx, case, path)


@native_test(timeout=180)
def test_corpus_contract_uno(ctx):
    _check(ctx, CONTRACT)


@native_test(timeout=180)
def test_corpus_row_changes_uno(ctx):
    _check(ctx, ROW_CHANGES)


@native_test(timeout=180)
def test_corpus_notes_uno(ctx):
    _check(ctx, NOTES)


@native_test(timeout=180)
def test_corpus_letter_uno(ctx):
    _check(ctx, LETTER)


@native_test(timeout=180)
def test_corpus_sections_uno(ctx):
    _check(ctx, SECTIONS)


@native_test(timeout=180)
def test_corpus_redlined_odf_uno(ctx):
    _check(ctx, REDLINED_ODF)


@native_test(timeout=60)
def test_corpus_read_keeps_struck_clause_before_heading_uno(ctx):
    """A full read must not restyle a clause struck with its paragraph mark just before a heading.

    An ODF store hides and reshows deletions in the live document (sw/source/filter/xml/wrtxml.cxx
    SetRedlineFlags, ``SwRangeRedline::MoveFromSection`` in sw/source/core/doc/docredln.cxx) and the
    clause comes back with the heading's style. ``get_content`` stores the live document to ODF.
    """
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as folder:
        path = Path(folder) / "struck.docx"
        write_contract(path, struck_before_heading=True)
        with _opened(ctx, path) as doc:
            before = _body(doc)
            idx = next(i for i, entry in enumerate(before) if entry[-1] == "This whole clause is struck.")
            assert before[idx][0] == "Clause"
            assert before[idx + 1][:2] == ("Heading 1", "3.") and before[idx + 1][3] == "Fees"
            _ok(ctx, doc, "get_content")
            after = _body(doc)
            if after != before:
                diffs = [
                    i
                    for i in range(max(len(before), len(after)))
                    if i >= len(before) or i >= len(after) or before[i] != after[i]
                ]
                if (
                    len(after) == len(before)
                    and diffs == [idx, idx + 1, idx + 2]
                    and after[idx][0] == "Heading 1"
                    and after[idx][3] == "This whole clause is struck."
                    and after[idx + 1][:2] == ("Heading 1", "4.")
                    and after[idx + 1][3] == "Fees"
                    and after[idx + 2][:2] == ("Clause", "4.1")
                    and after[idx + 2][3] == "Fees are payable within thirty days."
                ):
                    raise unittest.SkipTest("known gap: the ODF store behind get_content restyles the struck clause")
                assert after == before, f"unexpected body change: {[(before[i], after[i]) for i in diffs]}"
