# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Unit tests for in-memory eval document simulator (scripts/prompt_optimization)."""

import json
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
_PO = _REPO / "scripts" / "prompt_optimization"
if str(_PO) not in sys.path:
    sys.path.insert(0, str(_PO))

from string_eval_tools import StringDocState, dispatch_string_tool


def test_get_full_and_range():
    s = StringDocState("<p>Hello</p>")
    r = s.get_document_content(scope="full")
    assert r["status"] == "ok"
    assert "Hello" in r["content"]
    r2 = s.get_document_content(scope="range", start=0, end=4)
    assert r2["content"] == "<p>H"


def test_apply_search_replace():
    s = StringDocState("foo bar foo")
    r = s.apply_document_content(
        target="search",
        old_content="foo",
        content="baz",
    )
    assert r["status"] == "ok"
    assert s.get_html() == "baz bar foo"
    r2 = s.apply_document_content(
        target="search",
        old_content="foo",
        content="x",
        all_matches=True,
    )
    assert r2["status"] == "ok"
    assert "foo" not in s.get_html()


def test_apply_full_and_end():
    s = StringDocState("a")
    s.apply_document_content(target="full_document", content="<h1>x</h1>")
    assert s.get_html() == "<h1>x</h1>"
    s.apply_document_content(target="end", content="y")
    assert s.get_html().endswith("y")


def test_find_text():
    s = StringDocState("AaA")
    r = s.find_text("a", case_sensitive=False, limit=2)
    assert r["status"] == "ok"
    assert len(r["ranges"]) == 2


def test_dispatch_tools_json():
    s = StringDocState("hello")
    out = dispatch_string_tool(s, "find_text", json.dumps({"search": "ll"}))
    data = json.loads(out)
    assert data["status"] == "ok"
    assert data["ranges"]

