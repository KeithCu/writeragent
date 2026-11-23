"""Tests for per-run chunked webpage storage and reading tools."""

from __future__ import annotations

import threading
import time
from unittest.mock import MagicMock

import pytest

from plugin.chatbot.web_pages import (
    DEFAULT_PART_CHARS,
    FindInPageTool,
    PagedVisitTool,
    PageStore,
    ReadPageTool,
    effective_context_window,
    page_budget_chars,
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

    out = visit.forward(["https://example.test/long"])
    assert "Part 1 of " in out
    assert lines[0] in out
    assert lines[-1] not in out
    assert len(out) < DEFAULT_PART_CHARS + 400


def test_short_page_is_returned_unchanged_and_searchable():
    text = "Short page alpha beta gamma.\n" * 15
    assert len(text) < 600

    inner = MagicMock()
    inner.forward.return_value = text
    store = PageStore()
    visit = PagedVisitTool(inner, store)
    find = FindInPageTool(store)

    url = "https://example.test/short"
    assert visit.forward([url]) == f"URL: {url}\n\n{text}"
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
        out = visit.forward([url])
        assert out.startswith(f"[failed] {url}:")
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
    visit.forward([url])
    page = store.get(url)
    assert page is not None
    assert "".join(page.part(i) for i in range(1, page.part_count + 1)) == text

    bodies = [_strip_part_header(read.forward(url, i)) for i in range(1, page.part_count + 1)]
    expected_joined = "".join(page.part(i).rstrip("\n") for i in range(1, page.part_count + 1))
    assert "".join(bodies) == expected_joined

    starts = split_parts(text)
    for idx in range(len(starts) - 1):
        chunk = text[starts[idx] : starts[idx + 1]]
        if chunk != "H" * DEFAULT_PART_CHARS:
            assert chunk.endswith("\n")


def test_read_page_out_of_range_and_unknown_url():
    text = ("Alpha line.\n" * 1_500).strip()
    inner = MagicMock()
    inner.forward.return_value = text
    store = PageStore()
    visit = PagedVisitTool(inner, store)
    read = ReadPageTool(store)

    url = "https://example.test/bounds"
    visit.forward([url])
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
    visit.forward([url])
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
    visit.forward([many_url])
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

    visit.forward(["https://a.test/x/"])
    out = read.forward("https://a.test/x", 2)
    assert "Part 2 of " in out


def test_store_drops_oldest_page_past_cap():
    store = PageStore(max_pages=2)
    store.put("https://a.test/1", "one")
    store.put("https://a.test/2", "two")
    store.put("https://a.test/3", "three")

    assert store.get("https://a.test/1") is None
    assert store.get("https://a.test/2") is not None
    assert store.get("https://a.test/3") is not None


def test_visit_keeps_cached_page_when_evicted_mid_batch():
    store = PageStore(max_pages=2)
    store.put("https://evict.test/a", "Page A")
    store.put("https://evict.test/filler", "Filler")

    def fetch(url: str) -> str:
        # Evict Page A while fetching B.
        store.put("https://evict.test/extra1", "X1")
        store.put("https://evict.test/extra2", "X2")
        return "Page B"

    inner = MagicMock()
    inner.forward.side_effect = fetch
    visit = PagedVisitTool(inner, store)

    out = visit.forward(["https://evict.test/a", "https://evict.test/b"])
    assert "Page A" in out
    assert "Page B" in out
    assert "[timed out]" not in out


def test_visit_fetches_different_domains_in_parallel():
    barrier = threading.Barrier(3, timeout=5)

    def fetch(url: str) -> str:
        barrier.wait()
        return f"Body for {url}"

    inner = MagicMock()
    inner.forward.side_effect = fetch
    store = PageStore()
    visit = PagedVisitTool(inner, store)

    urls = ["https://p1.test/1", "https://p2.test/2", "https://p3.test/3"]
    out = visit.forward(urls)

    blocks = out.split("\n\nURL: ")
    assert len(blocks) == 3
    assert "https://p1.test/1" in blocks[0] and "Body for https://p1.test/1" in blocks[0]
    assert blocks[1].startswith("https://p2.test/2") and "Body for https://p2.test/2" in blocks[1]
    assert blocks[2].startswith("https://p3.test/3") and "Body for https://p3.test/3" in blocks[2]
    for u in urls:
        assert store.get(u) is not None


def test_visit_serializes_same_domain_in_batch():
    lock = threading.Lock()
    active = 0
    peak = 0
    timings: list[tuple[str, float, float]] = []

    def fetch(url: str) -> str:
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
        start = time.monotonic()
        time.sleep(0.1)
        end = time.monotonic()
        with lock:
            active -= 1
            timings.append((url, start, end))
        return f"Body for {url}"

    inner = MagicMock()
    inner.forward.side_effect = fetch
    store = PageStore()
    visit = PagedVisitTool(inner, store)

    urls = ["https://www.serial.test/1", "https://serial.test/2"]
    out = visit.forward(urls)

    assert peak == 1
    assert [u for u, _s, _e in timings] == urls
    assert timings[1][1] >= timings[0][2]
    for u in urls:
        assert f"Body for {u}" in out
        assert store.get(u) is not None


def test_visit_serializes_same_domain_across_concurrent_calls():
    lock = threading.Lock()
    active = 0
    peak = 0
    entered_first = threading.Event()

    def fetch(url: str) -> str:
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
        entered_first.set()
        time.sleep(0.1)
        with lock:
            active -= 1
        return f"Body for {url}"

    inner_a = MagicMock()
    inner_a.forward.side_effect = fetch
    inner_b = MagicMock()
    inner_b.forward.side_effect = fetch
    visit_a = PagedVisitTool(inner_a, PageStore())
    visit_b = PagedVisitTool(inner_b, PageStore())

    results: dict[str, str] = {}

    def run_a() -> None:
        results["a"] = visit_a.forward(["https://cross.test/a"])

    def run_b() -> None:
        assert entered_first.wait(timeout=2)
        results["b"] = visit_b.forward(["https://www.cross.test/b"])

    t_a = threading.Thread(target=run_a)
    t_b = threading.Thread(target=run_b)
    t_a.start()
    t_b.start()
    t_a.join(timeout=5)
    t_b.join(timeout=5)

    assert peak == 1
    assert "Body for https://cross.test/a" in results["a"]
    assert "Body for https://www.cross.test/b" in results["b"]


@pytest.mark.parametrize(
    "parallel, first_url, second_url",
    [
        pytest.param(True, "https://wait.test/a", "https://www.wait.test/b", id="parallel-same-domain"),
        pytest.param(False, "https://wait-a.test/a", "https://wait-b.test/b", id="serial-cross-domain"),
    ],
)
def test_visit_stop_returns_while_waiting_for_same_domain_turn(monkeypatch, parallel, first_url, second_url):
    from plugin.chatbot import web_pages

    monkeypatch.setattr(web_pages, "PARALLEL_WEB_FETCH", parallel)
    entered = threading.Event()
    release = threading.Event()
    stopped = {"flag": False}

    def fetch_a(url: str) -> str:
        entered.set()
        release.wait(timeout=5)
        return "Page A"

    inner_a = MagicMock()
    inner_a.forward.side_effect = fetch_a
    visit_a = PagedVisitTool(inner_a, PageStore())

    inner_b = MagicMock()
    inner_b.forward.return_value = "Page B"
    store_b = PageStore()
    visit_b = PagedVisitTool(inner_b, store_b, stop_checker=lambda: stopped["flag"])

    thread_a = threading.Thread(target=visit_a.forward, args=([first_url],))
    thread_a.start()
    assert entered.wait(timeout=2)

    def trigger_stop() -> None:
        time.sleep(0.1)
        stopped["flag"] = True

    stopper = threading.Thread(target=trigger_stop)
    stopper.start()
    try:
        # Release is not set until after visit_b returns, so Stop must abort the lock wait.
        out = visit_b.forward([second_url])
    finally:
        release.set()
        stopper.join(timeout=2)
        thread_a.join(timeout=2)

    assert out == f"[stopped] {second_url}"
    time.sleep(0.1)
    assert inner_b.forward.call_count == 0
    assert store_b.get(second_url) is None


@pytest.mark.parametrize(
    "parallel, slow_url, next_url",
    [
        pytest.param(True, "https://queue.test/slow", "https://queue.test/next", id="parallel-same-domain"),
        pytest.param(False, "https://queue-a.test/slow", "https://queue-b.test/next", id="serial-cross-domain"),
    ],
)
def test_visit_deadline_skips_queued_same_domain_url(monkeypatch, parallel, slow_url, next_url):
    from plugin.chatbot import web_pages

    monkeypatch.setattr(web_pages, "PARALLEL_WEB_FETCH", parallel)
    release = threading.Event()
    called: list[str] = []

    def fetch(url: str) -> str:
        called.append(url)
        if url.endswith("/slow"):
            release.wait(timeout=5)
            return "Slow body"
        return "Next body"

    inner = MagicMock()
    inner.forward.side_effect = fetch
    store = PageStore()
    visit = PagedVisitTool(inner, store, wait_sec=0.3)

    try:
        out = visit.forward([slow_url, next_url])
    finally:
        release.set()

    time.sleep(0.1)
    assert f"[timed out] {slow_url}:" in out
    assert f"[timed out] {next_url}:" in out
    assert called == [slow_url]
    assert store.get(next_url) is None


def test_visit_one_failure_keeps_the_other_pages():
    calls: dict[str, int] = {}

    def fetch(url: str) -> str:
        calls[url] = calls.get(url, 0) + 1
        if url.endswith("/raise"):
            raise RuntimeError("boom")
        if url.endswith("/403"):
            return "Error fetching the webpage: 403"
        return "Good content"

    inner = MagicMock()
    inner.forward.side_effect = fetch
    store = PageStore()
    visit = PagedVisitTool(inner, store)

    out = visit.forward(["https://f.test/raise", "https://f.test/403", "https://f.test/ok"])
    assert "[failed] https://f.test/raise:" in out
    assert "[failed] https://f.test/403: Error fetching the webpage: 403" in out
    assert "Good content" in out
    assert store.get("https://f.test/ok") is not None
    assert store.get("https://f.test/raise") is None
    assert store.get("https://f.test/403") is None

    visit.forward(["https://f.test/raise", "https://f.test/403", "https://f.test/ok"])
    assert calls["https://f.test/raise"] == 2
    assert calls["https://f.test/403"] == 2
    assert calls["https://f.test/ok"] == 1


def test_visit_stop_returns_while_fetch_is_blocked():
    entered = threading.Event()
    release = threading.Event()
    stopped = {"flag": False}

    def fetch(url: str) -> str:
        entered.set()
        release.wait(timeout=5)
        return "Late page"

    inner = MagicMock()
    inner.forward.side_effect = fetch
    store = PageStore()
    visit = PagedVisitTool(inner, store, stop_checker=lambda: stopped["flag"])

    def trigger_stop() -> None:
        assert entered.wait(timeout=2)
        time.sleep(0.1)
        stopped["flag"] = True

    stopper = threading.Thread(target=trigger_stop, daemon=True)
    stopper.start()
    start = time.monotonic()
    out = visit.forward(["https://stop.test/slow"])
    elapsed = time.monotonic() - start
    stopper.join(timeout=2)

    assert elapsed < 2.0
    assert out == "[stopped] https://stop.test/slow"
    assert store.get("https://stop.test/slow") is None

    release.set()
    time.sleep(0.05)
    assert store.get("https://stop.test/slow") is None


def test_visit_timeout_reports_only_the_slow_page():
    release = threading.Event()

    def fetch(url: str) -> str:
        if url.endswith("/slow"):
            release.wait(timeout=5)
            return "Slow body"
        return "Fast body"

    inner = MagicMock()
    inner.forward.side_effect = fetch
    store = PageStore()
    visit = PagedVisitTool(inner, store, wait_sec=0.3)

    try:
        out = visit.forward(["https://t.test/fast", "https://t.test/slow"])
    finally:
        release.set()

    assert "Fast body" in out
    assert "[timed out] https://t.test/slow:" in out
    assert store.get("https://t.test/fast") is not None
    assert store.get("https://t.test/slow") is None


def test_visit_caps_at_five_urls():
    inner = MagicMock()
    inner.forward.side_effect = lambda u: f"Body {u}"
    store = PageStore()
    visit = PagedVisitTool(inner, store)

    urls = [f"https://cap.test/{i}" for i in range(7)]
    out = visit.forward(urls)
    assert inner.forward.call_count == 5
    assert "[skipped] https://cap.test/5:" in out
    assert "[skipped] https://cap.test/6:" in out


def test_store_reuses_page_for_same_url():
    inner = MagicMock()
    inner.forward.return_value = "Cached body"
    store = PageStore()
    visit = PagedVisitTool(inner, store)

    first = visit.forward(["https://dup.test/a", "https://dup.test/a/"])
    second = visit.forward(["https://dup.test/a/"])
    assert inner.forward.call_count == 1
    assert "Cached body" in first
    assert "Cached body" in second


@pytest.mark.parametrize(
    "window, is_ollama, expected_window, expected_budget, expected_part",
    [
        pytest.param(4_000, False, 4_000, 2_000, 1_000, id="4k"),
        pytest.param(4_096, True, 4_096, 2_048, 1_024, id="4096"),
        pytest.param(8_000, False, 8_000, 4_000, 2_000, id="8k"),
        pytest.param(8_192, True, 8_192, 4_096, 2_048, id="8192"),
        pytest.param(32_000, False, 32_000, 16_000, 8_000, id="32k"),
        pytest.param(32_768, False, 32_768, 16_384, 8_192, id="32768"),
        pytest.param(128_000, False, 128_000, 48_000, 24_000, id="128k"),
        pytest.param(200_000, False, 200_000, 48_000, 24_000, id="200k"),
        pytest.param(1_000_000, False, 1_000_000, 48_000, 24_000, id="1M"),
        pytest.param(1_000, False, 1_000, 2_000, 1_000, id="below-floor-clamped"),
        pytest.param(None, True, 4_096, 2_048, 1_024, id="unknown-ollama"),
        pytest.param(None, False, 24_000, 12_000, 6_000, id="unknown-other"),
    ],
)
def test_page_budget_formula_table(window, is_ollama, expected_window, expected_budget, expected_part):
    assert effective_context_window(window, is_ollama=is_ollama) == expected_window
    budget = page_budget_chars(window, is_ollama=is_ollama)
    assert budget == expected_budget
    store = PageStore(budget_chars=budget)
    assert store.budget_chars == expected_budget
    assert store.part_chars == expected_part


def test_page_store_splits_according_to_budget():
    lines = [f"Line {i:04d}: " + ("a" * 80) for i in range(440)]
    text = "\n".join(lines)
    assert 39_000 < len(text) < 42_000

    # Large window (128k+ -> 48k budget): 40k page fits whole in one visit call.
    large_store = PageStore(budget_chars=page_budget_chars(200_000))
    inner = MagicMock()
    inner.forward.return_value = text
    large_out = PagedVisitTool(inner, large_store).forward(["https://scale.test/big"])
    assert large_out == f"URL: https://scale.test/big\n\n{text}"
    large_page = large_store.get("https://scale.test/big")
    assert large_page is not None and large_page.part_count == 2

    # Small window (8k -> 4k budget, 2k parts): same page splits into ~20 parts.
    small_store = PageStore(budget_chars=page_budget_chars(8_000))
    small_out = PagedVisitTool(inner, small_store).forward(["https://scale.test/big"])
    small_page = small_store.get("https://scale.test/big")
    assert small_page is not None and small_page.part_count >= 20
    assert f"Part 1 of {small_page.part_count}" in small_out
    assert len(_strip_part_header(small_out)) <= small_store.part_chars


def test_visit_per_url_split_scales_with_batch_size():
    # 9,000-char page fits in a 12,000 single-URL budget, exceeds 12,000 // 2 = 6,000.
    medium = "\n".join(f"Row {i:03d}: " + ("m" * 80) for i in range(100))
    assert 8_500 < len(medium) < 9_500
    short = "Short page body.\n" * 60
    assert len(short) < 2_000

    inner = MagicMock()
    inner.forward.side_effect = lambda u: short if u.endswith("/short") else medium
    store = PageStore(budget_chars=12_000)
    visit = PagedVisitTool(inner, store)

    # 1 URL -> whole page (<= 12,000).
    single = visit.forward(["https://batch.test/1"])
    assert single == f"URL: https://batch.test/1\n\n{medium}"

    # 2 URLs -> per-URL budget 6,000: 9,000-char pages return Part 1 of 2, not whole.
    two = visit.forward(["https://batch.test/1", "https://batch.test/2"])
    two_blocks = two.split("\n\nURL: ")
    assert len(two_blocks) == 2
    for block in two_blocks:
        assert "Part 1 of 2" in block
        assert len(_strip_part_header(block)) <= 6_000

    # 5 URLs -> per-URL budget 2,400: short fits whole, medium is clipped to <= 2,400 with read_page(url, 1) hint.
    five = visit.forward([
        "https://batch.test/short",
        "https://batch.test/a",
        "https://batch.test/b",
        "https://batch.test/c",
        "https://batch.test/d",
    ])
    five_blocks = five.split("\n\nURL: ")
    assert len(five_blocks) == 5
    assert "Part 1 of " not in five_blocks[0]
    assert short in five_blocks[0]
    total_body_chars = len(_strip_part_header(five_blocks[0]))
    for block in five_blocks[1:]:
        assert "Part 1 of 2" in block
        assert "read_page(url, 1)" in block
        body = _strip_part_header(block)
        assert len(body) <= 2_400
        total_body_chars += len(body)
    assert total_body_chars <= store.budget_chars

    # read_page(url, 1) still returns the full ~6,000-char part 1 from PageStore.
    full_part1 = _strip_part_header(ReadPageTool(store).forward("https://batch.test/a", 1))
    assert 5_500 <= len(full_part1) <= 6_000


def test_visit_five_large_pages_on_8k_budget_stays_within_budget():
    long_page = "\n".join(f"Line {i:03d}: " + ("x" * 70) for i in range(150))
    assert len(long_page) > 10_000

    inner = MagicMock()
    inner.forward.return_value = long_page
    store = PageStore(budget_chars=page_budget_chars(8_000))
    visit = PagedVisitTool(inner, store)

    out = visit.forward([f"https://small8k.test/{i}" for i in range(5)])
    blocks = out.split("\n\nURL: ")
    assert len(blocks) == 5
    bodies = [_strip_part_header(b) for b in blocks]
    assert all(0 < len(body) <= 800 for body in bodies)
    assert sum(len(body) for body in bodies) <= store.budget_chars


def test_visit_failed_urls_do_not_shrink_surviving_page_budget():
    medium = "\n".join(f"Row {i:03d}: " + ("m" * 80) for i in range(100))
    assert 8_500 < len(medium) < 9_500

    def fetch(url: str) -> str:
        if url.endswith("/ok"):
            return medium
        return "Error fetching the webpage: 403"

    inner = MagicMock()
    inner.forward.side_effect = fetch
    store = PageStore(budget_chars=12_000)
    visit = PagedVisitTool(inner, store)

    out = visit.forward([
        "https://fail.test/1",
        "https://fail.test/2",
        "https://fail.test/3",
        "https://fail.test/4",
        "https://fail.test/ok",
    ])
    assert "Part 1 of " not in out
    assert f"URL: https://fail.test/ok\n\n{medium}" in out


@pytest.mark.parametrize("budget", [2_048, 4_096])
def test_find_in_page_output_respects_budget_chars(budget):
    many_text = "\n".join(f"item {i}: TARGETWORD " + ("z" * 300) for i in range(25))
    store = PageStore(budget_chars=budget)
    url = "https://find-bound.test/page"
    store.put(url, many_text)

    res = FindInPageTool(store).forward(url, "targetword")
    assert len(res) <= budget
    shown_lines = res.splitlines()[1:]
    assert len(shown_lines) >= 1
    if budget == 2_048:
        assert len(shown_lines) < 10


def test_parallel_flag_defaults_on():
    from plugin.chatbot import web_pages

    assert web_pages.PARALLEL_WEB_FETCH is True
    visit = PagedVisitTool(MagicMock(), PageStore())
    assert list(visit.inputs) == ["urls"]
    assert "one to five" in visit.description.lower()


def test_parallel_flag_off_serializes_different_domains_and_concurrent_calls(monkeypatch):
    from plugin.chatbot import web_pages

    monkeypatch.setattr(web_pages, "PARALLEL_WEB_FETCH", False)

    lock = threading.Lock()
    active = 0
    peak = 0
    order: list[str] = []

    def fetch(url: str) -> str:
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
            order.append(url)
        time.sleep(0.05)
        with lock:
            active -= 1
        return f"Body for {url}"

    inner = MagicMock()
    inner.forward.side_effect = fetch
    store = PageStore()
    visit = PagedVisitTool(inner, store)

    assert list(visit.inputs) == ["url"]
    assert "five" not in visit.description.lower()
    assert "at once" not in visit.description.lower()
    assert "don't" not in visit.description.lower()
    assert "do not" not in visit.description.lower()

    urls = ["https://d1.test/1", "https://d2.test/2", "https://d3.test/3"]
    out = visit.forward(urls=urls)
    assert peak == 1
    assert order == urls
    for u in urls:
        assert f"Body for {u}" in out
        assert store.get(u) is not None

    # Concurrent visit_webpage calls for different domains also serialize.
    entered = threading.Event()
    release = threading.Event()
    concurrent_peak = 0
    concurrent_active = 0

    def fetch_concurrent(url: str) -> str:
        nonlocal concurrent_active, concurrent_peak
        with lock:
            concurrent_active += 1
            concurrent_peak = max(concurrent_peak, concurrent_active)
        if url.endswith("/a"):
            entered.set()
            release.wait(timeout=5)
        with lock:
            concurrent_active -= 1
        return f"Body for {url}"

    inner_a = MagicMock()
    inner_a.forward.side_effect = fetch_concurrent
    inner_b = MagicMock()
    inner_b.forward.side_effect = fetch_concurrent
    visit_a = PagedVisitTool(inner_a, PageStore())
    visit_b = PagedVisitTool(inner_b, PageStore())
    results: dict[str, str] = {}

    def run_a() -> None:
        results["a"] = visit_a.forward(url="https://d4.test/a")

    def run_b() -> None:
        assert entered.wait(timeout=2)
        results["b"] = visit_b.forward(url="https://d5.test/b")

    t_a = threading.Thread(target=run_a)
    t_b = threading.Thread(target=run_b)
    t_a.start()
    t_b.start()
    assert entered.wait(timeout=2)
    time.sleep(0.05)
    release.set()
    t_a.join(timeout=5)
    t_b.join(timeout=5)

    assert concurrent_peak == 1
    assert "Body for https://d4.test/a" in results["a"]
    assert "Body for https://d5.test/b" in results["b"]


def test_parallel_flag_off_chunking_pagestore_and_stop(monkeypatch):
    from plugin.chatbot import web_pages

    monkeypatch.setattr(web_pages, "PARALLEL_WEB_FETCH", False)

    long_text = "\n".join(f"Line {i:04d}: " + ("x" * 70) for i in range(450))
    inner = MagicMock()
    inner.forward.return_value = long_text
    store = PageStore()
    visit = PagedVisitTool(inner, store)
    read = ReadPageTool(store)
    find = FindInPageTool(store)

    url = "https://serial-chunk.test/long"
    out = visit.forward(url=url)
    assert "Part 1 of " in out
    assert "Part 2 of " in read.forward(url, 2)
    assert "1 match(es)" in find.forward(url, "Line 0400:")

    # Stop returns promptly while a fetch is blocked.
    entered = threading.Event()
    release = threading.Event()
    stopped = {"flag": False}

    def blocked_fetch(u: str) -> str:
        entered.set()
        release.wait(timeout=5)
        return "Late"

    blocked_inner = MagicMock()
    blocked_inner.forward.side_effect = blocked_fetch
    blocked_store = PageStore()
    stop_visit = PagedVisitTool(blocked_inner, blocked_store, stop_checker=lambda: stopped["flag"])

    def trigger_stop() -> None:
        assert entered.wait(timeout=2)
        stopped["flag"] = True

    stopper = threading.Thread(target=trigger_stop)
    stopper.start()
    try:
        stop_out = stop_visit.forward(url="https://serial-stop.test/slow")
    finally:
        release.set()
        stopper.join(timeout=2)

    assert stop_out == "[stopped] https://serial-stop.test/slow"
    assert blocked_store.get("https://serial-stop.test/slow") is None
