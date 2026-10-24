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
"""GitHub bug-report URL builder and browser launcher.

Opens https://github.com/KeithCu/writeragent/issues/new with a pre-filled body
(OS, LibreOffice, endpoint, chat model, etc.). No GitHub CLI required.

Browser open order:
1. UNO ``com.sun.star.system.SystemShellExecute`` with ``URIS_ONLY``
2. stdlib ``webbrowser.open`` fallback
"""

from __future__ import annotations

import logging
import os
import platform
import sys
from typing import Any
from urllib.parse import urlencode

from plugin.framework.errors import best_effort

log = logging.getLogger("writeragent.bugreport")

BUG_REPORT_REPO = "KeithCu/writeragent"
BUG_REPORT_ISSUES_URL = f"https://github.com/{BUG_REPORT_REPO}/issues/new"

# Practical URL length limit for issue body query param.
_MAX_BODY_CHARS = 7500


def _config_has_property(ca: Any, name: str) -> bool:
    """Return True when *name* exists on a ConfigurationAccess node."""
    info = ca.getPropertySetInfo()
    if info is None or not hasattr(info, "hasPropertyByName"):
        return False
    return bool(info.hasPropertyByName(name))


def _read_config_string(ca: Any, prop: str) -> str | None:
    """Read a non-empty string property from ConfigurationAccess, or None."""
    if not _config_has_property(ca, prop):
        return None
    val = ca.getPropertyValue(prop)
    if val is None:
        return None
    text = str(val).strip()
    return text or None


def _get_lo_product_info(ctx: Any) -> dict[str, str]:
    """Read LibreOffice version strings from setup configuration."""
    if not ctx:
        return {}

    def _read() -> dict[str, str]:
        import uno

        out: dict[str, str] = {}
        smgr = ctx.getServiceManager()
        config_provider = smgr.createInstanceWithContext("com.sun.star.configuration.ConfigurationProvider", ctx)
        node = uno.createUnoStruct("com.sun.star.beans.PropertyValue", Name="nodepath", Value="/org.openoffice.Setup/Product")
        ca = config_provider.createInstanceWithArguments("com.sun.star.configuration.ConfigurationAccess", (node,))
        for key, prop in (
            ("name", "ooName"),
            ("about", "ooSetupVersionAboutBox"),
            ("version", "ooSetupVersion"),
            ("suffix", "ooSetupVersionAboutBoxSuffix"),
        ):
            val = _read_config_string(ca, prop)
            if val:
                out[key] = val
        return out

    return best_effort(_read, "bug_report: read LO product info", {})


def _format_lo_version(product: dict[str, str]) -> str:
    about = product.get("about", "").strip()
    if about:
        suffix = product.get("suffix", "").strip()
        return f"{about} {suffix}".strip()
    name = product.get("name", "").strip()
    version = product.get("version", "").strip()
    nv = " ".join(part for part in (name, version) if part)
    return nv or "unknown"


def _issue_sections(ctx: Any | None = None) -> tuple[str, str]:
    """Return (header, rest) sections for environment and issue body."""
    from plugin.framework.config import get_config_str, get_current_endpoint, user_config_dir
    from plugin.framework.i18n import get_lo_locale
    from plugin.framework.constants import EXTENSION_ID_LIBREPY
    from plugin.framework.logging import get_debug_log_path
    from plugin.framework.uno_context import product_display_name, resolve_package_extension_id
    from plugin.version import EXTENSION_VERSION

    ext_id = resolve_package_extension_id(ctx)
    product_label = product_display_name(ctx)

    lines = [
        "### Environment",
        f"- {product_label}: {EXTENSION_VERSION}",
    ]

    product = _get_lo_product_info(ctx)
    lines.append(f"- LibreOffice: {_format_lo_version(product)}")
    lines.append(f"- OS: {platform.platform()} ({platform.machine()})")

    locale = get_lo_locale(ctx) if ctx else get_lo_locale()
    lines.append(f"- Locale: {locale}")

    lines.append(f"- Python: {sys.version.split()[0]} ({sys.platform})")

    if ext_id == EXTENSION_ID_LIBREPY:
        venv_path = get_config_str("scripting.python_venv_path") or "(not set)"
        lines.append(f"- Python venv path: {venv_path}")
    else:
        endpoint = get_current_endpoint() or "(not set)"
        model = "(not set)"
        try:
            from plugin.framework.client.model_fetcher import get_text_model

            model = get_text_model() or "(not set)"
        except Exception:
            log.debug("bug_report: get_text_model unavailable", exc_info=True)
        lines.append(f"- Endpoint: {endpoint}")
        lines.append(f"- Chat model: {model}")

    header = "\n".join(lines)

    rest_lines = []
    log_path = get_debug_log_path()
    if not log_path:
        log_dir = best_effort(user_config_dir, "bug_report: user_config_dir")
        if log_dir:
            log_path = os.path.join(log_dir, "writeragent_debug.log")
    if log_path:
        rest_lines.extend(
            [
                "### Debug log",
                f"{product_label} debug log: `{log_path}`",
                "If you can, open that file and skim it for errors or stack traces from around the time of the bug; paste anything useful under Additional context below.",
                "",
            ]
        )

    rest_lines.extend(
        [
            "### Description",
            "(Please describe the bug)",
            "",
            "### Steps to reproduce",
            "1.",
            "",
            "### Additional context",
        ]
    )
    rest = "\n".join(rest_lines)
    return header, rest


def _truncate_body(body: str) -> str:
    if len(body) <= _MAX_BODY_CHARS:
        return body
    suffix = "\n\n[truncated]"
    return body[: _MAX_BODY_CHARS - len(suffix)] + suffix


def _compose_issue_body(header: str, rest: str, extra_body: str) -> str:
    """Keep the environment header, then the exception tail.

    A long traceback used to fill the whole 7500-character budget, so version,
    OS, locale, Python, endpoint, and model never reached the GitHub URL.
    The header is reserved. The exception tail fills what remains. The rest of
    the template (description prompts) is included only when both fit.
    """
    extra = (extra_body or "").strip()
    header = (header or "").strip()
    rest = (rest or "").strip()
    template = f"{header}\n\n{rest}".strip() if (header or rest) else ""
    if not extra:
        return _truncate_body(template)
    if not header and not rest:
        return _truncate_body(extra)
    suffix = "\n\n[truncated]"
    room = _MAX_BODY_CHARS - len(suffix)
    if len(header) >= room:
        return header[:room] + suffix
    extra_room = room - len(header) - 1
    if len(extra) <= extra_room:
        body = f"{header}\n{extra}" if header else extra
        leftover = _MAX_BODY_CHARS - len(body) - 1
        if rest and leftover > 0:
            if len(rest) <= leftover:
                return f"{body}\n{rest}"
            return f"{body}\n{rest[:leftover]}{suffix}"
        return body
    return f"{header}\n{extra[-extra_room:]}{suffix}"


def build_github_issue_url(*, title: str = "", extra_body: str = "", ctx: Any | None = None) -> str:
    """Build a GitHub new-issue URL with title and pre-filled body."""
    header, rest = _issue_sections(ctx)
    body = _compose_issue_body(header, rest, extra_body)

    params: dict[str, str] = {"body": body}
    title_clean = (title or "").strip()
    if title_clean:
        params["title"] = title_clean[:200]
    return f"{BUG_REPORT_ISSUES_URL}?{urlencode(params)}"


def open_url_in_browser(ctx: Any, url: str) -> bool:
    """Open *url* in the system browser (SystemShellExecute, then webbrowser)."""
    if not url:
        return False

    if ctx:
        def _open_shell() -> bool:
            from com.sun.star.system import SystemShellExecuteFlags

            smgr = ctx.getServiceManager()
            shell = smgr.createInstanceWithContext("com.sun.star.system.SystemShellExecute", ctx)
            shell.execute(url, "", SystemShellExecuteFlags.URIS_ONLY)
            return True

        if best_effort(_open_shell, f"bug_report: SystemShellExecute failed for {url}"):
            return True

    def _open_browser() -> bool:
        import webbrowser

        return bool(webbrowser.open(url))

    if best_effort(_open_browser, f"bug_report: webbrowser.open failed for {url}"):
        return True

    log.warning("bug_report: could not open URL in browser: %s", url)
    return False


def open_bug_report_in_browser(ctx: Any, *, title: str = "", extra_body: str = "") -> bool:
    """Open a pre-filled GitHub new-issue page."""
    url = build_github_issue_url(title=title, extra_body=extra_body, ctx=ctx)
    return open_url_in_browser(ctx, url)
