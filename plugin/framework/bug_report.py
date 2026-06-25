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

log = logging.getLogger("writeragent.bugreport")

BUG_REPORT_REPO = "KeithCu/writeragent"
BUG_REPORT_ISSUES_URL = f"https://github.com/{BUG_REPORT_REPO}/issues/new"

# Practical URL length limit for issue body query param.
_MAX_BODY_CHARS = 7500

# Exception codes that are user/config mistakes, not extension bugs.
_NON_REPORTABLE_CODES = frozenset(
    {
        "CONFIG_ERROR",
        "CONFIG_KEY_NOT_FOUND",
        "CONFIG_DIR_ERROR",
        "INVALID_API_KEY",
    }
)


def should_offer_bug_report(*, exc: BaseException | None = None, code: str | None = None) -> bool:
    """Heuristic for whether a Report bug affordance makes sense."""
    if code and code in _NON_REPORTABLE_CODES:
        return False
    if exc is not None:
        from plugin.framework.errors import WriterAgentException

        if isinstance(exc, WriterAgentException) and exc.code in _NON_REPORTABLE_CODES:
            return False
    return True


def _get_lo_product_info(ctx: Any) -> dict[str, str]:
    """Read LibreOffice version strings from setup configuration."""
    out: dict[str, str] = {}
    if not ctx:
        return out
    try:
        import uno

        smgr = ctx.getServiceManager()
        config_provider = smgr.createInstanceWithContext("com.sun.star.configuration.ConfigurationProvider", ctx)
        node = uno.createUnoStruct("com.sun.star.beans.PropertyValue", Name="nodepath", Value="/org.openoffice.Setup/Product")
        ca = config_provider.createInstanceWithArguments("com.sun.star.configuration.ConfigurationAccess", (node,))
        for key, prop in (
            ("about", "ooSetupVersionAboutBox"),
            ("version", "ooSetupVersion"),
            ("build", "ooSetupBuild"),
        ):
            try:
                val = ca.getPropertyValue(prop)
                if val is not None and str(val).strip():
                    out[key] = str(val).strip()
            except Exception:
                log.debug("bug_report: missing LO product property %s", prop, exc_info=True)
    except Exception:
        log.debug("bug_report: could not read LO product info", exc_info=True)
    return out


def _format_lo_version(product: dict[str, str]) -> str:
    if product.get("about"):
        return product["about"]
    parts = [product.get("version", ""), product.get("build", "")]
    joined = " ".join(p for p in parts if p).strip()
    return joined or "unknown"


def collect_environment_block(ctx: Any | None = None) -> str:
    """Plain-text environment block for a GitHub issue body."""
    from plugin.framework.config import get_current_endpoint, user_config_dir
    from plugin.framework.client.model_fetcher import get_text_model
    from plugin.framework.i18n import get_lo_locale
    from plugin.version import EXTENSION_VERSION

    lines = [
        "### Environment",
        f"- WriterAgent: {EXTENSION_VERSION}",
    ]

    product = _get_lo_product_info(ctx)
    lines.append(f"- LibreOffice: {_format_lo_version(product)}")
    lines.append(f"- OS: {platform.platform()} ({platform.machine()})")

    try:
        locale = get_lo_locale(ctx) if ctx else get_lo_locale()
        lines.append(f"- Locale: {locale}")
    except Exception:
        lines.append("- Locale: unknown")

    lines.append(f"- Python: {sys.version.split()[0]} ({sys.platform})")

    endpoint = get_current_endpoint() or "(not set)"
    model = get_text_model() or "(not set)"
    lines.append(f"- Endpoint: {endpoint}")
    lines.append(f"- Chat model: {model}")

    log_dir = None
    try:
        log_dir = user_config_dir()
    except Exception:
        log.debug("bug_report: user_config_dir failed", exc_info=True)
    if log_dir:
        log_path = os.path.join(log_dir, "writeragent_debug.log")
        lines.extend(
            [
                "",
                "### Debug log",
                f"WriterAgent debug log: `{log_path}`",
                "If you can, open that file and skim it for errors or stack traces from around the time of the bug; paste anything useful under Additional context below.",
            ]
        )

    lines.append("")
    lines.append("### Description")
    lines.append("(Please describe the bug)")
    lines.append("")
    lines.append("### Steps to reproduce")
    lines.append("1.")
    lines.append("")
    lines.append("### Additional context")

    return "\n".join(lines)


def _truncate_body(body: str) -> str:
    if len(body) <= _MAX_BODY_CHARS:
        return body
    suffix = "\n\n[truncated]"
    return body[: _MAX_BODY_CHARS - len(suffix)] + suffix


def build_github_issue_url(*, title: str = "", extra_body: str = "", ctx: Any | None = None) -> str:
    """Build a GitHub new-issue URL with title and pre-filled body."""
    body = collect_environment_block(ctx)
    extra = (extra_body or "").strip()
    if extra:
        body = f"{body}\n{extra}"
    body = _truncate_body(body)

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
        try:
            from com.sun.star.system import SystemShellExecuteFlags

            smgr = ctx.getServiceManager()
            shell = smgr.createInstanceWithContext("com.sun.star.system.SystemShellExecute", ctx)
            shell.execute(url, "", SystemShellExecuteFlags.URIS_ONLY)
            return True
        except Exception:
            log.debug("bug_report: SystemShellExecute failed for %s", url, exc_info=True)

    try:
        import webbrowser

        if webbrowser.open(url):
            return True
    except Exception:
        log.debug("bug_report: webbrowser.open failed for %s", url, exc_info=True)

    log.warning("bug_report: could not open URL in browser: %s", url)
    return False


def open_bug_report_in_browser(ctx: Any, *, title: str = "", extra_body: str = "") -> bool:
    """Open a pre-filled GitHub new-issue page."""
    url = build_github_issue_url(title=title, extra_body=extra_body, ctx=ctx)
    return open_url_in_browser(ctx, url)
