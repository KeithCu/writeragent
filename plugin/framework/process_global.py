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
"""Process-wide global singleton helper for LibreOffice double-import environments.

LibreOffice can import a Python module twice (for instance, across different
component contexts or classloader environments within the same LibreOffice process),
giving each copy its own distinct module-level globals.

State that must remain shared across the entire process (such as the event bus,
the logging shared box, or install-once hooks like exception handlers and watchdog
threads) is stored on ``sys.__dict__``. Storing once via ``setdefault`` ensures
that singletons stay singletons and what is installed once stays installed once.
"""

from __future__ import annotations

import sys
from typing import Callable, TypeVar, cast

T = TypeVar("T")


def process_global(name: str, factory: Callable[[], T]) -> T:
    """Return a process-wide singleton stored in ``sys.__dict__``, creating it once.

    If *name* (prefixed with ``_writeragent_``) already exists in ``sys.__dict__``,
    its current value is returned. Otherwise, *factory* is called and its result is
    stored in ``sys.__dict__`` via ``setdefault`` and returned.
    """
    key = f"_writeragent_{name}" if not name.startswith("_writeragent_") else name
    if key in sys.__dict__:
        return cast("T", sys.__dict__[key])
    return cast("T", sys.__dict__.setdefault(key, factory()))
