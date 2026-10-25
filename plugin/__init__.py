# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2024 John Balis
# Copyright (c) 2026 KeithCu (modifications and relicensing)
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
"""WriterAgent / LibrePy / LibreHarper top-level package.

``extend_path`` merges only this top-level directory when two OXTs are installed
side by side. Child packages (``plugin.framework``, ``plugin.chatbot``, and the
rest) are ordinary packages: the first root on ``__path__`` supplies every
submodule. Those copies must be the same version. ``extend_path`` does not merge
them.
"""

from __future__ import annotations

import logging
import os
from pkgutil import extend_path
import re

__path__ = extend_path(__path__, __name__)

_VERSION_LINE = re.compile(r'(?m)^EXTENSION_VERSION\s*=\s*["\']([^"\']+)["\']')
_AUDIT_HINT = "Run only one OXT version, or update both."

_logged_audit_key: tuple[tuple[str, str], ...] | None = None
_logged_audit_to_file = False


def audit_plugin_roots(roots: list[str] | None = None) -> str:
    """Audit plugin roots for side-by-side OXT installations and version consistency."""
    global _logged_audit_key, _logged_audit_to_file

    if roots is None:
        roots = list(__path__)

    seen_keys: set[str] = set()
    versioned_roots: list[tuple[str, str]] = []
    for entry in roots:
        if not isinstance(entry, str) or not entry:
            continue
        real_path = os.path.realpath(entry)
        canonical = os.path.normcase(real_path)
        if canonical in seen_keys:
            continue
        seen_keys.add(canonical)
        version_file = os.path.join(real_path, "version.py")
        try:
            with open(version_file, encoding="utf-8") as f:
                content = f.read()
        except OSError:
            continue
        match = _VERSION_LINE.search(content)
        if not match:
            continue
        versioned_roots.append((real_path, match.group(1)))

    if len(versioned_roots) <= 1:
        return "ok"

    versions = {v for _, v in versioned_roots}
    code = "warn" if len(versions) == 1 else "error"

    audit_key = tuple(versioned_roots)
    has_handlers = bool(logging.getLogger("plugin").handlers)

    should_log = False
    if audit_key != _logged_audit_key:
        should_log = True
    elif has_handlers and not _logged_audit_to_file:
        should_log = True

    if should_log:
        _logged_audit_key = audit_key
        _logged_audit_to_file = has_handlers
        logger = logging.getLogger("plugin")
        if code == "warn":
            shared_version = versioned_roots[0][1]
            paths_str = "; ".join(path for path, _ in versioned_roots)
            logger.warning(
                "Dual OXT plugin roots at version %s: %s. "
                "extend_path does not merge child packages; "
                "the first root wins every plugin.<pkg> submodule.",
                shared_version,
                paths_str,
            )
        else:
            pairs_str = "; ".join(f"{path}={version}" for path, version in versioned_roots)
            logger.error(
                f"Dual OXT plugin roots have different versions: %s. {_AUDIT_HINT}",
                pairs_str,
            )

    return code


# Child packages are first-wins. This check only logs.
try:
    audit_plugin_roots()
except Exception:
    logging.getLogger("plugin").exception("plugin path audit failed")
