# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.
"""Per-run in-memory page store and chunked-reading tools for web research."""

from __future__ import annotations

import bisect
import logging
import queue
import re
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable

from plugin.contrib.smolagents.tools import Tool
from plugin.framework.worker_pool import run_in_background

log = logging.getLogger(__name__)

# Shared by both HTTP and CDP visit tools; raises HTTP page-cache row sizes accordingly.
# Pre-existing 40,000-character HTTP cache rows continue to be served until they expire.
PAGE_TEXT_MAX_CHARS = 200_000
MAX_URLS_PER_VISIT = 5
_VISIT_WAIT_SEC = 45.0
# Per-result character budget: clamp(window_tokens // 2, 2_000, 48_000), roughly one eighth
# of the context window at ~4 chars/token. Part size is budget // 2.
MIN_PAGE_BUDGET_CHARS = 2_000
MAX_PAGE_BUDGET_CHARS = 48_000
# Unknown-window defaults: Ollama omits num_ctx from /api/show unless set in a Modelfile
# (treat as its 4,096 default); other endpoints fall back to 24,000 tokens (12k budget / 6k parts).
OLLAMA_DEFAULT_CONTEXT_TOKENS = 4_096
FALLBACK_CONTEXT_TOKENS = 24_000
# System prompt + WEB_RESEARCH_EXAMPLES (~1,370-1,450 rough tokens) plus wire tool schemas
# (~565 rough tokens) consume ~2,000 tokens before any query or observation; below 4,000 tokens
# there is too little room left for search, page reading, and an answer.
MIN_WEB_RESEARCH_CONTEXT_TOKENS = 4_000
DEFAULT_PAGE_BUDGET_CHARS = FALLBACK_CONTEXT_TOKENS // 2
# Split into char parts rather than lines because HTML TextExtractor emits 7-14-char fragments.
DEFAULT_PART_CHARS = DEFAULT_PAGE_BUDGET_CHARS // 2
_MAX_PAGES = 100
_FIND_MAX_MATCHES = 10
_FIND_CONTEXT_CHARS = 120

VISIT_WEBPAGE_DESCRIPTION = (
    "Fetches one to five web pages at once and returns their text. A long page is split into "
    "numbered parts; its result then holds part 1 and the number of parts, and read_page / find_in_page read the rest."
)


def visit_webpage_inputs() -> dict[str, dict[str, Any]]:
    """Return the smolagents input schema for visit_webpage."""
    return {
        "urls": {
            "type": "array",
            "items": {"type": "string"},
            "description": "One to five http or https URLs to visit.",
        }
    }


def url_key(url: str) -> str:
    """Normalize a URL for page-store and visit-dedup lookups."""
    return str(url or "").strip().rstrip("/")


def is_page_text(text: str) -> bool:
    """Return True when fetched text is non-empty and not an error or failure message."""
    body = str(text or "").strip()
    return bool(body) and not body.startswith(("Error", "Failed"))


def effective_context_window(window_tokens: int | None, *, is_ollama: bool = False) -> int:
    """Resolve an unknown context window to its provider default (4,096 for Ollama, 24,000 otherwise)."""
    if window_tokens is not None:
        return window_tokens
    return OLLAMA_DEFAULT_CONTEXT_TOKENS if is_ollama else FALLBACK_CONTEXT_TOKENS


def page_budget_chars(window_tokens: int | None, *, is_ollama: bool = False) -> int:
    """Per-tool-result character budget derived from the model's context window."""
    resolved = effective_context_window(window_tokens, is_ollama=is_ollama)
    return max(MIN_PAGE_BUDGET_CHARS, min(MAX_PAGE_BUDGET_CHARS, resolved // 2))


def _clip_at_newline(text: str, limit: int) -> str:
    """Return a prefix of `text` of at most `limit` chars, cutting at a newline when non-empty."""
    if len(text) <= limit:
        return text
    cut = text.rfind("\n", 1, limit + 1)
    if cut > 0 and text[:cut].strip():
        return text[: cut + 1]
    return text[:limit]


def split_parts(text: str, size: int = DEFAULT_PART_CHARS) -> tuple[int, ...]:
    """Return 0-based start offsets splitting text into roughly `size`-char parts at newlines."""
    if not text:
        return (0,)
    starts = [0]
    s = 0
    n = len(text)
    while s + size < n:
        cut = text.rfind("\n", s + 1, s + size + 1)
        s = cut + 1 if cut > s else s + size
        if s >= n:
            break
        starts.append(s)
    return tuple(starts)


@dataclass(frozen=True)
class StoredPage:
    """Immutable fetched webpage text and its part-start offsets."""

    text: str
    starts: tuple[int, ...]

    @property
    def part_count(self) -> int:
        return len(self.starts)

    def part(self, number: int) -> str:
        """Return the 1-based part slice."""
        start = self.starts[number - 1]
        end = self.starts[number] if number < len(self.starts) else None
        return self.text[start:end]

    def part_at(self, offset: int) -> int:
        """Return the 1-based part number containing `offset`."""
        return bisect.bisect_right(self.starts, offset)


class PageStore:
    """Text of the pages one research run has fetched.

    Created once per WebResearchTool.execute call and freed with its params.
    Deep-research workers share one store through dataclasses.replace.
    """

    _lock: threading.Lock
    _pages: dict[str, StoredPage]
    _max_pages: int
    _budget_chars: int
    _part_chars: int

    def __init__(
        self,
        *,
        max_pages: int = _MAX_PAGES,
        budget_chars: int = DEFAULT_PAGE_BUDGET_CHARS,
    ) -> None:
        self._lock = threading.Lock()
        self._pages = {}
        self._max_pages = max_pages
        self._budget_chars = budget_chars
        self._part_chars = max(1, budget_chars // 2)

    @property
    def budget_chars(self) -> int:
        return self._budget_chars

    @property
    def part_chars(self) -> int:
        return self._part_chars

    def put(self, url: str, text: str) -> StoredPage:
        """Store fetched page text under its normalized URL, evicting the oldest past max_pages."""
        key = url_key(url)
        page = StoredPage(text=text, starts=split_parts(text, self._part_chars))
        with self._lock:
            if key in self._pages:
                del self._pages[key]
            self._pages[key] = page
            while len(self._pages) > self._max_pages:
                self._pages.pop(next(iter(self._pages)))
        return page

    def get(self, url: str) -> StoredPage | None:
        """Return the StoredPage for `url`, or None if not stored."""
        key = url_key(url)
        with self._lock:
            return self._pages.get(key)


def _render_part(url: str, page: StoredPage, number: int) -> str:
    """Format one stored page part with its navigation header.

    The header must never start with "Error" or "Failed" so is_page_text treats it as valid text.
    """
    body = page.part(number).rstrip("\n")
    return (
        f"URL: {url}\n"
        f"Part {number} of {page.part_count} ({len(page.text)} characters in total). "
        "read_page(url, part) returns another part; find_in_page(url, text) shows where a phrase appears.\n\n"
        f"{body}"
    )


def _format_page(url: str, page: StoredPage, whole_limit: int) -> str:
    """Return the full page when it fits in `whole_limit`, else part 1 (clipped if `whole_limit` < part 1)."""
    if len(page.text) <= whole_limit:
        return f"URL: {url}\n\n{page.text}"
    part1 = page.part(1)
    if len(part1) <= whole_limit:
        return _render_part(url, page, 1)
    body = _clip_at_newline(part1, whole_limit).rstrip("\n")
    return (
        f"URL: {url}\n"
        f"Part 1 of {page.part_count} ({len(page.text)} characters in total; showing first {len(body)}). "
        "read_page(url, 1) returns full part 1; find_in_page(url, text) shows where a phrase appears.\n\n"
        f"{body}"
    )


class PagedVisitTool(Tool):
    """Fetches one to five URLs in parallel, stores valid pages, and returns part 1 for long ones."""

    name: str = "visit_webpage"
    description: str = VISIT_WEBPAGE_DESCRIPTION
    inputs: dict[str, dict[str, Any]] = visit_webpage_inputs()
    output_type: str = "string"
    _inner: Tool
    _pages: PageStore
    _stop_checker: Callable[[], bool] | None
    _wait_sec: float

    def __init__(
        self,
        inner: Tool,
        pages: PageStore,
        stop_checker: Callable[[], bool] | None = None,
        *,
        wait_sec: float = _VISIT_WAIT_SEC,
    ) -> None:
        super().__init__()
        self._inner = inner
        self._pages = pages
        self._stop_checker = stop_checker
        self._wait_sec = wait_sec

    def _fetch_all(self, urls: list[str]) -> tuple[dict[str, str], bool]:
        """Fetch `urls` concurrently on dedicated threads up to wait_sec."""
        if not urls:
            return {}, False
        results_q: queue.SimpleQueue[tuple[str, str]] = queue.SimpleQueue()

        def _fetch_one(target_url: str) -> None:
            try:
                text = self._inner.forward(target_url)
            except Exception as exc:
                log.exception("visit_webpage failed for %s", target_url)
                text = f"Error fetching the webpage: {exc}"
            results_q.put((target_url, text))

        for target_url in urls:
            # Dedicated threads: the caller waits on these, and the shared pool has
            # two slots which the agent thread may already hold.
            run_in_background(_fetch_one, target_url, name="visit-webpage", dedicated=True)

        texts: dict[str, str] = {}
        stopped = False
        deadline = time.monotonic() + self._wait_sec
        while len(texts) < len(urls):
            if self._stop_checker and self._stop_checker():
                stopped = True
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                u, text = results_q.get(timeout=min(0.25, remaining))
            except queue.Empty:
                continue
            texts[u] = text

        while True:
            try:
                u, text = results_q.get_nowait()
            except queue.Empty:
                break
            texts[u] = text
        return texts, stopped

    def forward(self, urls: list[str]) -> str:
        seen: set[str] = set()
        unique: list[str] = []
        for raw in urls:
            u = str(raw or "").strip()
            key = url_key(u)
            if not key or key in seen:
                continue
            seen.add(key)
            unique.append(u)

        batch = unique[:MAX_URLS_PER_VISIT]
        skipped = unique[MAX_URLS_PER_VISIT:]
        if not batch:
            return "visit_webpage needs one to five URLs."

        cached = {u: self._pages.get(u) for u in batch}
        pending = [u for u in batch if cached[u] is None]
        texts, stopped = self._fetch_all(pending)

        whole_limit = max(1, self._pages.budget_chars // len(batch))
        blocks: list[str] = []
        for u in batch:
            existing = cached[u]
            if existing is not None:
                blocks.append(_format_page(u, existing, whole_limit))
            elif u in texts:
                text = texts[u]
                # Only store valid page text so an Error/Failed reply or blank extract stays retryable.
                if is_page_text(text):
                    page = self._pages.put(u, text)
                    blocks.append(_format_page(u, page, whole_limit))
                else:
                    reason = text.strip() or "no text could be extracted"
                    blocks.append(f"[failed] {u}: {reason}")
            elif stopped:
                blocks.append(f"[stopped] {u}")
            else:
                blocks.append(f"[timed out] {u}: no response within {int(self._wait_sec)} s")

        for u in skipped:
            blocks.append(f"[skipped] {u}: at most {MAX_URLS_PER_VISIT} URLs per call")

        return "\n\n".join(blocks)


class ReadPageTool(Tool):
    """Reads one numbered part of a webpage previously fetched with visit_webpage."""

    name: str = "read_page"
    description: str = "Returns one numbered part of the text of a page fetched with visit_webpage."
    inputs: dict[str, dict[str, str | type[Any] | bool]] = {
        "url": {"type": "string", "description": "The url passed to visit_webpage."},
        "part": {"type": "integer", "description": "Part number, starting at 1."},
    }
    output_type: str = "string"
    _pages: PageStore

    def __init__(self, pages: PageStore) -> None:
        super().__init__()
        self._pages = pages

    def forward(self, url: str, part: int) -> str:
        page = self._pages.get(url)
        if page is None:
            return f"No text is stored for {url}; visit_webpage fetches it."
        count = page.part_count
        if part < 1 or part > count:
            return f"Part {part} does not exist; this page has {count} part(s)."
        return _render_part(url, page, part)


class FindInPageTool(Tool):
    """Searches a previously fetched webpage for a phrase and returns matching snippets with part numbers."""

    name: str = "find_in_page"
    description: str = (
        "Searches the full text of a page fetched with visit_webpage for a phrase "
        "(case-insensitive, exact characters) and returns each match with the text around it and its part number."
    )
    inputs: dict[str, dict[str, str | type[Any] | bool]] = {
        "url": {"type": "string", "description": "The url passed to visit_webpage."},
        "text": {"type": "string", "description": "The phrase to find."},
    }
    output_type: str = "string"
    _pages: PageStore

    def __init__(self, pages: PageStore) -> None:
        super().__init__()
        self._pages = pages

    def forward(self, url: str, text: str) -> str:
        page = self._pages.get(url)
        if page is None:
            return f"No text is stored for {url}; visit_webpage fetches it."
        needle = text.strip()
        if not needle:
            return "find_in_page needs a non-empty text."
        total = 0
        snippets: list[str] = []
        last_window_end = -1
        for match in re.finditer(re.escape(needle), page.text, re.IGNORECASE):
            total += 1
            s, e = match.start(), match.end()
            if s < last_window_end:
                continue
            if len(snippets) < _FIND_MAX_MATCHES:
                win_start = max(0, s - _FIND_CONTEXT_CHARS)
                win_end = min(len(page.text), e + _FIND_CONTEXT_CHARS)
                snippet = page.text[win_start:win_end].replace("\n", " ")
                part_num = page.part_at(s)
                snippets.append(f"Part {part_num}: …{snippet}…")
                last_window_end = win_end
        if total == 0:
            return f'No match for "{needle}" in {url} ({page.part_count} part(s)).'
        capped = f" ({len(snippets)} shown)" if total > len(snippets) else ""
        header = f'{total} match(es) for "{needle}" in {url}{capped}'
        return header + "\n" + "\n".join(snippets)
