#!/usr/bin/env python
# coding=utf-8

import threading
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

from plugin.framework.constants import BROWSER_USER_AGENT, USER_AGENT
from plugin.framework.logging import debug_log

try:
    from plugin.framework.history_db import HAS_SQLITE, sqlite3
except Exception:
    HAS_SQLITE = False
    sqlite3 = None  # type: ignore[assignment]

from .local_python_executor import (
    BASE_BUILTIN_MODULES,
    BASE_PYTHON_TOOLS,
    MAX_EXECUTION_TIME_SECONDS,
    evaluate_python_code,
)
from .tools import PipelineTool, Tool

USE_MARKDOWN = False

# ---------------------------------------------------------------------------
# Disk cache for web_search and visit_webpage (SQLite, shared between processes)
# ---------------------------------------------------------------------------

_WEB_CACHE_LOCK = threading.Lock()
_WEB_CACHE_MAX_RETRIES = 5

# VisitWebpageTool: sample size for garbage detection (binary/unreadable content)
_GARBAGE_CHECK_BYTES = 4096

# Minimum decoded length and garbage ratio threshold to treat content as binary
_GARBAGE_MIN_CHARS = 100
_GARBAGE_RATIO_THRESHOLD = 0.5


def _get_user_agent_for_url(url: str | None = None) -> str:
    """Return the appropriate User-Agent for a given URL.

    - DuckDuckGo and Wikipedia: WriterAgent UA (truthful identification).
    - Everything else: browser-style UA (assume random sites are paranoid).
    """
    if not url:
        return BROWSER_USER_AGENT
    try:
        parsed = urlparse(url)
        host = (parsed.netloc or "").lower()
    except Exception:
        return USER_AGENT

    if "duckduckgo.com" in host or "wikipedia.org" in host:
        return USER_AGENT
    return BROWSER_USER_AGENT


def _is_garbage_text(s: str) -> bool:
    """True if the decoded string looks like binary/garbage (e.g. PDF decoded as UTF-8)."""
    if len(s) < _GARBAGE_MIN_CHARS:
        return False
    garbage_count = sum(
        1 for c in s
        if c == "\ufffd" or (not c.isspace() and not c.isprintable())
    )
    return (garbage_count / len(s)) > _GARBAGE_RATIO_THRESHOLD


def _web_cache_ensure_schema(conn: Any) -> None:
    conn.execute(
        "CREATE TABLE IF NOT EXISTS web_cache "
        "(kind TEXT, key TEXT, value TEXT, size INTEGER, created_at REAL, PRIMARY KEY (kind, key))"
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_web_cache_created_at ON web_cache(created_at)")


def _web_cache_with_connection(db_path: str, fn):
    """Run fn(conn) with a locked connection; retry on database locked/busy."""
    if not HAS_SQLITE:
        return None
    for attempt in range(_WEB_CACHE_MAX_RETRIES):
        with _WEB_CACHE_LOCK:
            try:
                conn = sqlite3.connect(db_path, timeout=10.0)
                try:
                    _web_cache_ensure_schema(conn)
                    return fn(conn)
                finally:
                    conn.close()
            except sqlite3.OperationalError as e:
                if attempt < _WEB_CACHE_MAX_RETRIES - 1 and ("locked" in str(e).lower() or "busy" in str(e).lower()):
                    time.sleep(0.15 * (attempt + 1))
                    continue
                raise


def _web_cache_get(db_path: str, kind: str, key: str, max_age_days: int = 7) -> str | None:
    """Return cached value for (kind, key), or None. On hit, touch row (update created_at). No-op when SQLite unavailable."""
    if not HAS_SQLITE or not db_path or not key:
        return None

    def do_get(conn):
        row = conn.execute("SELECT value, created_at FROM web_cache WHERE kind = ? AND key = ?", (kind, key)).fetchone()
        if row is None:
            return None

        value, created_at = row
        max_age_seconds = max_age_days * 86400
        now = time.time()

        if now - created_at > max_age_seconds:
            conn.execute("DELETE FROM web_cache WHERE kind = ? AND key = ?", (kind, key))
            conn.commit()
            return None

        conn.execute(
            "UPDATE web_cache SET created_at = ? WHERE kind = ? AND key = ?",
            (now, kind, key),
        )
        conn.commit()
        return value

    return _web_cache_with_connection(db_path, do_get)


def _web_cache_set(db_path: str, kind: str, key: str, value: str, max_size_bytes: int) -> None:
    """Store (kind, key) -> value; evict oldest entries until total size <= max_size_bytes. No-op when SQLite unavailable."""
    if not HAS_SQLITE or not db_path or not key or max_size_bytes <= 0:
        return
    size = len(value.encode("utf-8"))

    def do_set(conn):
        now = time.time()
        conn.execute(
            "INSERT OR REPLACE INTO web_cache (kind, key, value, size, created_at) VALUES (?, ?, ?, ?, ?)",
            (kind, key, value, size, now),
        )
        while True:
            total = conn.execute("SELECT COALESCE(SUM(size), 0) FROM web_cache").fetchone()[0]
            if total <= max_size_bytes:
                break
            row = conn.execute(
                "SELECT kind, key FROM web_cache ORDER BY created_at ASC LIMIT 1"
            ).fetchone()
            if not row:
                break
            conn.execute("DELETE FROM web_cache WHERE kind = ? AND key = ?", row)
        conn.commit()

    _web_cache_with_connection(db_path, do_set)


@dataclass
class PreTool:
    name: str
    inputs: dict[str, str]
    output_type: type
    task: str
    description: str
    repo_id: str


class PythonInterpreterTool(Tool):
    name = "python_interpreter"
    description = "This is a tool that evaluates python code. It can be used to perform calculations."
    inputs = {
        "code": {
            "type": "string",
            "description": "The python code to run in interpreter",
        }
    }
    output_type = "string"

    def __init__(self, *args, authorized_imports=None, timeout_seconds=MAX_EXECUTION_TIME_SECONDS, **kwargs):
        if authorized_imports is None:
            self.authorized_imports = list(set(BASE_BUILTIN_MODULES))
        else:
            self.authorized_imports = list(set(BASE_BUILTIN_MODULES) | set(authorized_imports))
        self.inputs = {
            "code": {
                "type": "string",
                "description": (
                    "The code snippet to evaluate. All variables used in this snippet must be defined in this same snippet, "
                    f"else you will get an error. This code can only import the following python libraries: {self.authorized_imports}."
                ),
            }
        }
        self.base_python_tools = BASE_PYTHON_TOOLS
        self.python_evaluator = evaluate_python_code
        self.timeout_seconds = timeout_seconds
        super().__init__(*args, **kwargs)

    def forward(self, code: str) -> str:
        state = {}
        output = str(
            self.python_evaluator(
                code,
                state=state,
                static_tools=self.base_python_tools,
                authorized_imports=self.authorized_imports,
                timeout_seconds=self.timeout_seconds,
            )[0]  # The second element is boolean is_final_answer
        )
        return f"Stdout:\n{str(state['_print_outputs'])}\nOutput: {output}"


class FinalAnswerTool(Tool):
    name = "final_answer"
    description = "Provides a final answer to the given problem."
    inputs = {"answer": {"type": "any", "description": "The final answer to the problem"}}
    output_type = "any"

    def forward(self, answer: Any) -> Any:
        return answer


class UserInputTool(Tool):
    name = "user_input"
    description = "Asks for user's input on a specific question"
    inputs = {"question": {"type": "string", "description": "The question to ask the user"}}
    output_type = "string"

    def forward(self, question):
        user_input = input(f"{question} => Type your answer here:")
        return user_input


class DuckDuckGoSearchTool(Tool):
    name = "web_search"
    description = "Performs a duckduckgo web search based on your query (think a Google search) then returns the top search results."
    inputs = {"query": {"type": "string", "description": "The search query to perform."}}
    output_type = "string"

    def __init__(self, max_results: int = 10, cache_path: str | None = None, cache_max_mb: int = 50, cache_max_age_days: int = 7, **kwargs):
        super().__init__()
        self.max_results = max_results
        self._cache_path = cache_path
        self._cache_max_mb = max(cache_max_mb, 0)
        self._cache_max_age_days = max(cache_max_age_days, 1)

    def forward(self, query: str) -> str:
        key = " ".join(str(query).strip().split())
        if self._cache_path and self._cache_max_mb > 0 and key:
            cached = _web_cache_get(self._cache_path, "search", key, max_age_days=self._cache_max_age_days)
            if cached is not None:
                debug_log("web_cache: search hit: %s" % (key[:60] + "..." if len(key) > 60 else key), context="Chat")
                return cached
            debug_log("web_cache: search miss: %s" % (key[:60] + "..." if len(key) > 60 else key), context="Chat")

        import urllib.request
        import urllib.parse
        from html.parser import HTMLParser

        url = "https://lite.duckduckgo.com/lite/"
        data = urllib.parse.urlencode({"q": query}).encode("utf-8")
        req = urllib.request.Request(url, data=data, headers={"User-Agent": _get_user_agent_for_url(url)})
        try:
            with urllib.request.urlopen(req, timeout=10) as response:
                html = response.read().decode("utf-8")
        except Exception as e:
            return f"Error fetching search results: {str(e)}"

        class SimpleResultParser(HTMLParser):
            def __init__(self):
                super().__init__()
                self.results = []
                self.current = {}
                self.capture_title = False
                self.capture_description = False
                self.capture_link = False

            def handle_starttag(self, tag, attrs):
                attrs = dict(attrs)
                if tag == "a" and attrs.get("class") == "result-link":
                    self.capture_title = True
                elif tag == "td" and attrs.get("class") == "result-snippet":
                    self.capture_description = True
                elif tag == "span" and attrs.get("class") == "link-text":
                    self.capture_link = True

            def handle_endtag(self, tag):
                if tag == "a" and self.capture_title:
                    self.capture_title = False
                elif tag == "td" and self.capture_description:
                    self.capture_description = False
                elif tag == "span" and self.capture_link:
                    self.capture_link = False
                elif tag == "tr":
                    if {"title", "description", "link"} <= self.current.keys():
                        self.current["description"] = "".join(self.current["description"])
                        self.results.append(self.current)
                        self.current = {}

            def handle_data(self, data):
                if self.capture_title:
                    self.current["title"] = data.strip()
                elif self.capture_description:
                    self.current.setdefault("description", [])
                    self.current["description"].append(data.strip())
                elif self.capture_link:
                    self.current["link"] = "https://" + data.strip()

        parser = SimpleResultParser()
        parser.feed(html)
        results = parser.results[:self.max_results]

        if len(results) == 0:
            result = "No results found! Try a less restrictive/shorter query."
        else:
            if USE_MARKDOWN:
                postprocessed_results = [f"[{r['title']}]({r['link']})\n{r['description']}" for r in results]
                result = "## Search Results\n\n" + "\n\n".join(postprocessed_results)
            else:
                postprocessed_results = [f"<div><h3><a href='{r['link']}'>{r['title']}</a></h3><p>{r['description']}</p></div>" for r in results]
                result = "<h2>Search Results</h2>\n" + "\n".join(postprocessed_results)

        if self._cache_path and self._cache_max_mb > 0 and key:
            _web_cache_set(
                self._cache_path,
                "search",
                key,
                result,
                self._cache_max_mb * 1024 * 1024,
            )
        return result


class VisitWebpageTool(Tool):
    name = "visit_webpage"
    description = "Visits a webpage at the given url and reads its content as a markdown string. Use this to browse webpages."
    inputs = {"url": {"type": "string", "description": "The url of the webpage to visit."}}
    output_type = "string"

    def __init__(self, max_output_length: int = 40000, cache_path: str | None = None, cache_max_mb: int = 50, cache_max_age_days: int = 7, **kwargs):
        super().__init__()
        self.max_output_length = max_output_length
        self._cache_path = cache_path
        self._cache_max_mb = max(cache_max_mb, 0)
        self._cache_max_age_days = max(cache_max_age_days, 1)

    def _truncate_content(self, content: str, max_length: int) -> str:
        if len(content) <= max_length:
            return content
        return content[:max_length] + f"\n..._This content has been truncated to stay below {max_length} characters_...\n"

    def _return_error(self, key: str, message: str) -> str:
        """Return an error message and cache it when cache is enabled."""
        if self._cache_path and self._cache_max_mb > 0 and key:
            _web_cache_set(
                self._cache_path,
                "page",
                key,
                message,
                self._cache_max_mb * 1024 * 1024,
            )
        return message

    def forward(self, url: str) -> str:
        key = str(url).strip()

        # Cache lookup
        if self._cache_path and self._cache_max_mb > 0 and key:
            cached = _web_cache_get(self._cache_path, "page", key, max_age_days=self._cache_max_age_days)
            if cached is not None:
                debug_log("web_cache: page hit: %s" % (key[:60] + "..." if len(key) > 60 else key), context="Chat")
                return cached
            debug_log("web_cache: page miss: %s" % (key[:60] + "..." if len(key) > 60 else key), context="Chat")

        # Fail fast: URL path ends with .pdf
        parsed = urlparse(url)
        if (parsed.path or "").lower().endswith(".pdf"):
            return self._return_error(
                key,
                "Error fetching the webpage: URL points to a PDF; text content cannot be extracted.",
            )

        import urllib.request
        from html.parser import HTMLParser
        import re

        try:
            req = urllib.request.Request(url, headers={"User-Agent": _get_user_agent_for_url(url)})
            with urllib.request.urlopen(req, timeout=20) as response:
                # Content-Type: reject application/pdf without reading body
                content_type_header = (response.headers.get("Content-Type") or "").lower()
                if "application/pdf" in content_type_header:
                    return self._return_error(
                        key,
                        "Error fetching the webpage: Response is a PDF; text content cannot be extracted.",
                    )

                # Magic bytes: reject PDF by signature
                first_bytes = response.read(8)
                if first_bytes.startswith(b"%PDF-"):
                    return self._return_error(
                        key,
                        "Error fetching the webpage: Response is a PDF; text content cannot be extracted.",
                    )

                # Sample first 4096 bytes for garbage detection
                sample = response.read(_GARBAGE_CHECK_BYTES)
                prefix_bytes = first_bytes + sample
                charset = response.headers.get_content_charset() or "utf-8"
                sample_str = prefix_bytes.decode(charset, errors="ignore")
                if _is_garbage_text(sample_str):
                    return self._return_error(
                        key,
                        "Error fetching the webpage: Content appears to be binary or unreadable.",
                    )

                # Read remainder and decode full body
                rest = response.read()
                raw_body = prefix_bytes + rest
                html = raw_body.decode(charset, errors="ignore")

            class TextExtractor(HTMLParser):
                def __init__(self):
                    super().__init__()
                    self.text = []
                    self.hide = False
                    self.hide_tags = {"script", "style", "noscript", "meta", "head"}

                def handle_starttag(self, tag, attrs):
                    if tag in self.hide_tags:
                        self.hide = True

                def handle_endtag(self, tag):
                    if tag in self.hide_tags:
                        self.hide = False

                def handle_data(self, data):
                    if not self.hide:
                        d = data.strip()
                        if d:
                            self.text.append(d)

            extractor = TextExtractor()
            extractor.feed(html)
            text_content = "\n".join(extractor.text)
            text_content = re.sub(r"\n{3,}", "\n\n", text_content)
            result = self._truncate_content(text_content, self.max_output_length)
            if self._cache_path and self._cache_max_mb > 0 and key:
                _web_cache_set(
                    self._cache_path,
                    "page",
                    key,
                    result,
                    self._cache_max_mb * 1024 * 1024,
                )
            return result

        except Exception as e:
            return self._return_error(key, f"Error fetching the webpage: {str(e)}")


TOOL_MAPPING = {
    tool_class.name: tool_class
    for tool_class in [
        PythonInterpreterTool,
        DuckDuckGoSearchTool,
        VisitWebpageTool,
    ]
}

__all__ = [
    "PythonInterpreterTool",
    "FinalAnswerTool",
    "UserInputTool",
    "DuckDuckGoSearchTool",
    "VisitWebpageTool",
]
