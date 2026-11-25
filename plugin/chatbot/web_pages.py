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
from contextlib import contextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Callable, final

from plugin.contrib.smolagents.tools import Tool
from plugin.framework.url_utils import get_url_domain
from plugin.framework.worker_pool import run_in_background

if TYPE_CHECKING:
    from collections.abc import Generator

log = logging.getLogger(__name__)

# Shared by both HTTP and CDP visit tools; raises HTTP page-cache row sizes accordingly.
# Pre-existing 40,000-character HTTP cache rows continue to be served until they expire.
PAGE_TEXT_MAX_CHARS = 200_000
MAX_URLS_PER_VISIT = 5
_VISIT_WAIT_SEC = 45.0
# Controls the parallel web-fetch feature: multi-URL visit_webpage calls (up to MAX_URLS_PER_VISIT)
# with per-domain parallelism and 45 s batch wait. When False, page fetches run one at a time
# process-wide and visit_webpage advertises a single-url schema (while still accepting urls arrays).
PARALLEL_WEB_FETCH = True
# Per-result character budget: clamp(window_tokens // 2, 2_000, 48_000), roughly one eighth
# of the context window at ~4 chars/token. Part size is budget // 2.
MIN_PAGE_BUDGET_CHARS = 2_000
MAX_PAGE_BUDGET_CHARS = 48_000
# Window assumed when a non-Ollama endpoint reports none (12k budget / 6k parts).
FALLBACK_CONTEXT_TOKENS = 24_000
# Web research warns below this reported window, and assumes it when Ollama reports none.
# The system prompt, WEB_RESEARCH_EXAMPLES and wire tool schemas use about 2,000 tokens.
# Ollama's /api/show omits num_ctx unless the Modelfile sets it, and its real default is
# 4,096, 32,768 or 262,144 by VRAM (Ollama server/routes.go, Serve), so the assumed value
# keeps page parts small enough for the smallest of those; otherwise Ollama silently truncates the prompt.
RECOMMENDED_CONTEXT_TOKENS = 8_192
_MAX_PAGES = 100
_FIND_MAX_MATCHES = 10
_FIND_CONTEXT_CHARS = 120
_FIND_SNIPPET_OVERHEAD_CHARS = 40


@final
class _DomainTurn:
    """One domain's fetch lock and the number of threads holding or waiting for it."""

    __slots__ = ("lock", "users")
    lock: threading.Lock
    users: int

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.users = 0


_DOMAIN_TURNS_GUARD = threading.Lock()
_DOMAIN_TURNS: dict[str, _DomainTurn] = {}
# Every fetch shares this key when PARALLEL_WEB_FETCH is False so _domain_turn acts as one process-wide lock.
_SERIAL_FETCH_KEY = "__serial__"


@contextmanager
def _domain_turn(domain: str, give_up: threading.Event) -> Generator[bool]:
    """Hold `domain`'s process-wide fetch lock; yield False instead if `give_up` is set first."""
    # The map holds only domains currently being fetched or waited on, so a long session does not
    # grow it. The lock is per process so concurrent research runs and deep-research workers take
    # turns per domain. The key is get_url_domain, so www. and other subdomains share a turn.
    with _DOMAIN_TURNS_GUARD:
        turn = _DOMAIN_TURNS.get(domain)
        if turn is None:
            turn = _DomainTurn()
            _DOMAIN_TURNS[domain] = turn
        turn.users += 1

    acquired = False
    try:
        while not give_up.is_set():
            if turn.lock.acquire(timeout=0.05):
                if give_up.is_set():
                    turn.lock.release()
                else:
                    acquired = True
                break
        try:
            yield acquired
        finally:
            if acquired:
                turn.lock.release()
    finally:
        with _DOMAIN_TURNS_GUARD:
            turn.users -= 1
            if turn.users == 0:
                del _DOMAIN_TURNS[domain]

VISIT_WEBPAGE_DESCRIPTION = (
    "Fetches one to five web pages at once and returns their text. A long page is split into "
    "numbered parts; its result then holds part 1 and the number of parts, and read_page / find_in_page read the rest."
)
VISIT_WEBPAGE_SINGLE_DESCRIPTION = (
    "Fetches a web page and returns its text. A long page is split into "
    "numbered parts; its result then holds part 1 and the number of parts, and read_page / find_in_page read the rest."
)


def visit_webpage_description() -> str:
    """Return the visit_webpage tool description for the active PARALLEL_WEB_FETCH mode."""
    return VISIT_WEBPAGE_DESCRIPTION if PARALLEL_WEB_FETCH else VISIT_WEBPAGE_SINGLE_DESCRIPTION


def visit_webpage_inputs() -> dict[str, dict[str, Any]]:
    """Return the smolagents input schema for visit_webpage."""
    if PARALLEL_WEB_FETCH:
        return {
            "urls": {
                "type": "array",
                "items": {"type": "string"},
                "description": "One to five http or https URLs to visit.",
            }
        }
    return {
        "url": {
            "type": "string",
            "description": "The http or https URL to visit.",
        }
    }


def url_key(url: str) -> str:
    """Normalize a URL for page-store and visit-dedup lookups."""
    return str(url or "").strip().rstrip("/")


def is_page_text(text: str) -> bool:
    """Return True when fetched text is non-empty and not an error or failure message."""
    body = str(text or "").strip()
    return bool(body) and not body.startswith(("Error", "Failed"))


def page_budget_chars(window_tokens: int) -> int:
    """Per-tool-result character budget derived from the model's context window."""
    return max(MIN_PAGE_BUDGET_CHARS, min(MAX_PAGE_BUDGET_CHARS, window_tokens // 2))


DEFAULT_PAGE_BUDGET_CHARS = page_budget_chars(FALLBACK_CONTEXT_TOKENS)
# Split into char parts rather than lines because HTML TextExtractor emits 7-14-char fragments.
DEFAULT_PART_CHARS = DEFAULT_PAGE_BUDGET_CHARS // 2


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

    def snapshot(self) -> list[tuple[str, StoredPage]]:
        """Return (url key, page) pairs, oldest stored first."""
        with self._lock:
            return list(self._pages.items())


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
    """Fetches URLs (in parallel across domains when PARALLEL_WEB_FETCH is True, else one at a time), stores valid pages, and returns part 1 for long ones."""

    name: str = "visit_webpage"
    description: str = visit_webpage_description()
    inputs: dict[str, dict[str, Any]] = visit_webpage_inputs()
    output_type: str = "string"
    # Skipped because forward accepts both `url` and `urls` while `inputs` names only one.
    skip_forward_signature_validation: bool = True
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
        # Re-read the flag so the active PARALLEL_WEB_FETCH mode applies at construction.
        self.description = visit_webpage_description()
        self.inputs = visit_webpage_inputs()
        super().__init__()
        self._inner = inner
        self._pages = pages
        self._stop_checker = stop_checker
        self._wait_sec = wait_sec

    def _fetch_all(self, urls: list[str]) -> tuple[dict[str, str], bool]:
        """Fetch `urls` up to wait_sec: per-domain parallel when PARALLEL_WEB_FETCH is True, else one at a time process-wide."""
        if not urls:
            return {}, False
        results_q: queue.SimpleQueue[tuple[str, str]] = queue.SimpleQueue()
        give_up = threading.Event()

        by_domain: dict[str, list[str]] = {}
        for target_url in urls:
            turn_key = get_url_domain(target_url) if PARALLEL_WEB_FETCH else _SERIAL_FETCH_KEY
            by_domain.setdefault(turn_key, []).append(target_url)

        def _fetch_domain(domain: str, domain_urls: list[str]) -> None:
            for target_url in domain_urls:
                with _domain_turn(domain, give_up) as granted:
                    if not granted:
                        return
                    try:
                        text = self._inner.forward(target_url)
                    except Exception as exc:
                        log.exception("visit_webpage failed for %s", target_url)
                        text = f"Error fetching the webpage: {exc}"
                    results_q.put((target_url, text))

        for domain, domain_urls in by_domain.items():
            # Dedicated threads: the caller waits on these, and the shared pool has
            # two slots which the agent thread may already hold.
            run_in_background(_fetch_domain, domain, domain_urls, name="visit-webpage", dedicated=True)

        texts: dict[str, str] = {}
        stopped = False
        deadline = time.monotonic() + self._wait_sec
        try:
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
        finally:
            give_up.set()

        while True:
            try:
                u, text = results_q.get_nowait()
            except queue.Empty:
                break
            texts[u] = text
        return texts, stopped

    def forward(self, url: str | list[str] | None = None, urls: list[str] | str | None = None) -> str:
        raw_items: list[Any] = []
        for val in (urls, url):
            if val is None:
                continue
            if isinstance(val, (list, tuple)):
                raw_items.extend(val)
            else:
                raw_items.append(val)

        seen: set[str] = set()
        unique: list[str] = []
        for raw in raw_items:
            u = str(raw or "").strip()
            key = url_key(u)
            if not key or key in seen:
                continue
            seen.add(key)
            unique.append(u)

        batch = unique[:MAX_URLS_PER_VISIT]
        skipped = unique[MAX_URLS_PER_VISIT:]
        if not batch:
            return "visit_webpage needs one to five URLs." if PARALLEL_WEB_FETCH else "visit_webpage needs a URL."

        cached = {u: self._pages.get(u) for u in batch}
        pending = [u for u in batch if cached[u] is None]
        texts, stopped = self._fetch_all(pending)

        # Store fetched pages first so failed, timed-out, or stopped URLs do not shrink
        # the surviving pages' share of the body budget.
        pages_by_url: dict[str, StoredPage] = {}
        for u in batch:
            existing = cached[u]
            if existing is not None:
                pages_by_url[u] = existing
            elif u in texts and is_page_text(texts[u]):
                pages_by_url[u] = self._pages.put(u, texts[u])

        # Budget covers page bodies (per-block URL and part headers are fixed overhead).
        whole_limit = max(1, self._pages.budget_chars // max(1, len(pages_by_url)))
        blocks: list[str] = []
        for u in batch:
            page = pages_by_url.get(u)
            if page is not None:
                blocks.append(_format_page(u, page, whole_limit))
            elif u in texts:
                reason = texts[u].strip() or "no text could be extracted"
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


def _find_snippets(page: StoredPage, needle: str, limit: int) -> tuple[int, list[str]]:
    """Return the match count of `needle` in `page` and up to `limit` non-overlapping snippets."""
    total = 0
    snippets: list[str] = []
    last_window_end = -1
    for match in re.finditer(re.escape(needle), page.text, re.IGNORECASE):
        total += 1
        s, e = match.start(), match.end()
        if s < last_window_end or len(snippets) >= limit:
            continue
        win_start = max(0, s - _FIND_CONTEXT_CHARS)
        win_end = min(len(page.text), e + _FIND_CONTEXT_CHARS)
        snippet = page.text[win_start:win_end].replace("\n", " ")
        snippets.append(f"Part {page.part_at(s)}: …{snippet}…")
        last_window_end = win_end
    return total, snippets


def _shown_note(total: int, shown: int) -> str:
    return f" ({shown} shown)" if total > shown else ""


class FindInPageTool(Tool):
    """Searches one fetched page, or every fetched page, for a phrase and returns matching snippets with part numbers."""

    name: str = "find_in_page"
    description: str = (
        "Searches the full text of fetched pages for a phrase (case-insensitive, exact characters) "
        "and returns each match with the text around it and its part number. "
        "Without url, it searches every page fetched so far."
    )
    inputs: dict[str, dict[str, str | type[Any] | bool]] = {
        "text": {"type": "string", "description": "The phrase to find."},
        "url": {
            "type": "string",
            "description": "The url passed to visit_webpage. Omit to search all fetched pages.",
            "nullable": True,
        },
    }
    output_type: str = "string"
    _pages: PageStore

    def __init__(self, pages: PageStore) -> None:
        super().__init__()
        self._pages = pages

    def forward(self, text: str, url: str | None = None) -> str:
        # Models send null or "" for an optional argument they leave out.
        target = (url or "").strip()
        page = self._pages.get(target) if target else None
        if target and page is None:
            return f"No text is stored for {url}; visit_webpage fetches it."
        needle = text.strip()
        if not needle:
            return "find_in_page needs a non-empty text."
        per_snippet = 2 * _FIND_CONTEXT_CHARS + len(needle) + _FIND_SNIPPET_OVERHEAD_CHARS
        max_matches = max(1, min(_FIND_MAX_MATCHES, self._pages.budget_chars // per_snippet))
        if page is None:
            return self._find_in_all(needle, max_matches)
        total, snippets = _find_snippets(page, needle, max_matches)
        if total == 0:
            return f'No match for "{needle}" in {url} ({page.part_count} part(s)).'
        header = f'{total} match(es) for "{needle}" in {url}{_shown_note(total, len(snippets))}'
        return header + "\n" + "\n".join(snippets)

    def _find_in_all(self, needle: str, max_matches: int) -> str:
        """Search every stored page, newest first, sharing max_matches and the budget across pages."""
        pages = self._pages.snapshot()
        if not pages:
            return "No pages are stored yet; visit_webpage fetches them."
        hits: list[tuple[str, int, list[str]]] = []
        for url, page in reversed(pages):
            total, snippets = _find_snippets(page, needle, max_matches)
            if total:
                hits.append((url, total, snippets))
        if not hits:
            return f'No match for "{needle}" in any of the {len(pages)} stored page(s).'
        grand_total = sum(total for _url, total, _snippets in hits)
        header = f'{grand_total} match(es) for "{needle}" in {len(hits)} of {len(pages)} stored page(s)'
        # The extra characters leave room for the "(K page(s) shown)" note.
        used = len(header) + _FIND_SNIPPET_OVERHEAD_CHARS
        left = max_matches
        blocks: list[str] = []
        for idx, (url, total, snippets) in enumerate(hits):
            # An even share of the snippets left; at least one until they run out.
            quota = min(left, max(1, left // (len(hits) - idx)))
            shown = snippets[:quota]
            block = "\n".join([f"{total} match(es) in {url}{_shown_note(total, len(shown))}", *shown])
            if blocks and used + len(block) + 2 > self._pages.budget_chars:
                break
            blocks.append(block)
            used += len(block) + 2
            left -= len(shown)
        if len(blocks) < len(hits):
            header += f" ({len(blocks)} page(s) shown)"
        return "\n\n".join([header, *blocks])
