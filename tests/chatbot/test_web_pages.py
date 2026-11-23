"""Tests for per-run chunked webpage storage and reading tools."""

from __future__ import annotations

from unittest.mock import MagicMock

from plugin.chatbot import web_pages
from plugin.chatbot.web_pages import (
    _PART_CHARS,
    FindInPageTool,
    PagedVisitTool,
    PageStore,
    ReadPageTool,
    split_parts,
)


def _strip_part_header(output: str) -> str:
    _, body = output.split("\n\n", 1)
    return body


def test_long_page_visit_returns_first_part_and_count():
    lines = [f"Line {i:04d}: " + ("x" * 60) for i in range(450)]
    text = "\n".join(lines)
    assert len(text) > 30_000

    inner = MagicMock()
    inner.forward.return_value = text
    store = PageStore()
    visit = PagedVisitTool(inner, store)

    out = visit.forward("https://example.test/long")
    assert "Part 1 of " in out
    assert lines[0] in out
    assert lines[-1] not in out
    assert len(out) < _PART_CHARS + 400


def test_short_page_is_returned_unchanged_and_searchable():
    text = "Short page alpha beta gamma.\n" * 15
    assert len(text) < 600

    inner = MagicMock()
    inner.forward.return_value = text
    store = PageStore()
    visit = PagedVisitTool(inner, store)
    find = FindInPageTool(store)

    url = "https://example.test/short"
    assert visit.forward(url) == text
    found = find.forward(url, "beta")
    assert "15 match(es)" in found
    assert "Part 1:" in found


def test_fetch_error_passes_through_and_is_not_stored():
    inner = MagicMock()
    store = PageStore()
    visit = PagedVisitTool(inner, store)
    read = ReadPageTool(store)

    for err in ("Error fetching the webpage: timeout", "   \n\t "):
        inner.forward.return_value = err
        url = "https://example.test/err"
        assert visit.forward(url) == err
        assert "No text is stored" in read.forward(url, 1)


def test_parts_rejoin_to_the_full_page():
    hard_line = "H" * 9_000
    normal_lines = "\n".join(f"Row {i:04d} " + ("y" * 80) for i in range(200))
    text = f"Header\n{hard_line}\n{normal_lines}\nTail"

    inner = MagicMock()
    inner.forward.return_value = text
    store = PageStore()
    visit = PagedVisitTool(inner, store)
    read = ReadPageTool(store)

    url = "https://example.test/rejoin"
    visit.forward(url)
    page = store.get(url)
    assert page is not None
    assert "".join(page.part(i) for i in range(1, page.part_count + 1)) == text

    bodies = [_strip_part_header(read.forward(url, i)) for i in range(1, page.part_count + 1)]
    expected_joined = "".join(page.part(i).rstrip("\n") for i in range(1, page.part_count + 1))
    assert "".join(bodies) == expected_joined

    starts = split_parts(text)
    for idx in range(len(starts) - 1):
        chunk = text[starts[idx] : starts[idx + 1]]
        if chunk != "H" * _PART_CHARS:
            assert chunk.endswith("\n")


def test_read_page_out_of_range_and_unknown_url():
    text = ("Alpha line.\n" * 1_500).strip()
    inner = MagicMock()
    inner.forward.return_value = text
    store = PageStore()
    visit = PagedVisitTool(inner, store)
    read = ReadPageTool(store)

    url = "https://example.test/bounds"
    visit.forward(url)
    page = store.get(url)
    assert page is not None
    count = page.part_count
    assert count > 1

    assert "does not exist" in read.forward(url, 0)
    assert f"has {count} part(s)" in read.forward(url, count + 1)
    assert "No text is stored" in read.forward("https://example.test/never", 1)


def test_find_in_page_reports_part_and_context():
    filler = ("Standard filler sentence for pagination.\n" * 600)
    text = filler + "UniqueNeedle near the end: C++ $5 special.\n" + ("Trailing line.\n" * 20)
    inner = MagicMock()
    inner.forward.return_value = text
    store = PageStore()
    visit = PagedVisitTool(inner, store)
    find = FindInPageTool(store)

    url = "https://example.test/find"
    visit.forward(url)
    page = store.get(url)
    assert page is not None

    res = find.forward(url, "uniqueneedle")
    assert f"Part {page.part_count}:" in res
    assert "UniqueNeedle near the end" in res

    literal_res = find.forward(url, "C++ $5")
    assert "1 match(es)" in literal_res
    assert "C++ $5 special" in literal_res

    many_text = "\n".join(f"item {i}: TARGETWORD " + ("z" * 300) for i in range(25))
    many_url = "https://example.test/many"
    inner.forward.return_value = many_text
    visit.forward(many_url)
    many_res = find.forward(many_url, "targetword")
    assert '25 match(es) for "targetword"' in many_res
    assert "(10 shown)" in many_res
    assert len(many_res.splitlines()) == 11


def test_url_trailing_slash_reads_same_page():
    text = "Line of text for slash normalization.\n" * 500
    inner = MagicMock()
    inner.forward.return_value = text
    store = PageStore()
    visit = PagedVisitTool(inner, store)
    read = ReadPageTool(store)

    visit.forward("https://a.test/x/")
    out = read.forward("https://a.test/x", 2)
    assert "Part 2 of " in out


def test_store_drops_oldest_page_past_cap(monkeypatch):
    monkeypatch.setattr(web_pages, "_MAX_PAGES", 2)
    store = PageStore()
    store.put("https://a.test/1", "one")
    store.put("https://a.test/2", "two")
    store.put("https://a.test/3", "three")

    assert store.get("https://a.test/1") is None
    assert store.get("https://a.test/2") is not None
    assert store.get("https://a.test/3") is not None
