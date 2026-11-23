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
import re
import threading
from dataclasses import dataclass
from typing import Any

from plugin.contrib.smolagents.tools import Tool

PAGE_TEXT_MAX_CHARS = 200_000
_WHOLE_PAGE_MAX_CHARS = 12_000
_PART_CHARS = 6_000
_MAX_PAGES = 100
_FIND_MAX_MATCHES = 10
_FIND_CONTEXT_CHARS = 120

VISIT_WEBPAGE_DESCRIPTION = (
    "Fetches the webpage at the given url and returns its text. A long page is split into "
    "numbered parts; the result then holds part 1 and the number of parts."
)


def visit_webpage_inputs() -> dict[str, dict[str, str | type[Any] | bool]]:
    """Return the smolagents input schema for visit_webpage."""
    return {"url": {"type": "string", "description": "The url of the webpage to visit."}}


def url_key(url: str) -> str:
    """Normalize a URL for page-store and visit-dedup lookups."""
    return str(url or "").strip().rstrip("/")


def is_page_text(text: str) -> bool:
    """Return True when fetched text is non-empty and not an error or failure message."""
    body = str(text or "").strip()
    return bool(body) and not body.startswith(("Error", "Failed"))


def split_parts(text: str, size: int = _PART_CHARS) -> tuple[int, ...]:
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

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._pages = {}

    def put(self, url: str, text: str) -> StoredPage:
        """Store fetched page text under its normalized URL, evicting the oldest past _MAX_PAGES."""
        key = url_key(url)
        page = StoredPage(text=text, starts=split_parts(text))
        with self._lock:
            if key in self._pages:
                del self._pages[key]
            self._pages[key] = page
            while len(self._pages) > _MAX_PAGES:
                self._pages.pop(next(iter(self._pages)))
        return page

    def get(self, url: str) -> StoredPage | None:
        """Return the StoredPage for `url`, or None if not stored."""
        key = url_key(url)
        with self._lock:
            return self._pages.get(key)


def _render_part(url: str, page: StoredPage, number: int) -> str:
    body = page.part(number).rstrip("\n")
    return (
        f"URL: {url}\n"
        f"Part {number} of {page.part_count} ({len(page.text)} characters in total). "
        "read_page(url, part) returns another part; find_in_page(url, text) shows where a phrase appears.\n\n"
        f"{body}"
    )


class PagedVisitTool(Tool):
    """Wraps visit_webpage to store fetched pages and return long ones as part 1."""

    name: str = "visit_webpage"
    description: str = VISIT_WEBPAGE_DESCRIPTION
    inputs: dict[str, dict[str, str | type[Any] | bool]] = visit_webpage_inputs()
    output_type: str = "string"
    _inner: Tool
    _pages: PageStore

    def __init__(self, inner: Tool, pages: PageStore) -> None:
        super().__init__()
        self._inner = inner
        self._pages = pages

    def forward(self, url: str) -> str:
        text = self._inner.forward(url)
        if not is_page_text(text):
            return text
        page = self._pages.put(url, text)
        if len(text) <= _WHOLE_PAGE_MAX_CHARS:
            return text
        return _render_part(url, page, 1)


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
            return f"No text is stored for {url}; visit_webpage(url) fetches it."
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
            return f"No text is stored for {url}; visit_webpage(url) fetches it."
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
            return f'No match for "{text}" in {url} ({page.part_count} part(s)).'
        capped = (
            f" (first {_FIND_MAX_MATCHES} shown)"
            if len(snippets) >= _FIND_MAX_MATCHES and total > _FIND_MAX_MATCHES
            else ""
        )
        header = f'{total} match(es) for "{text}" in {url}{capped}'
        return header + "\n" + "\n".join(snippets)
