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
"""Process-wide singletons that survive a second import of a module.

LibreOffice can import a Python module twice, and each copy gets its own module
globals. State that must be shared by the whole process (the event bus, the
logging box, install-once hooks) is stored on ``sys`` instead, which both copies
see. This is the one place that does it.
"""

from __future__ import annotations

import sys
import threading
from typing import Callable, TypeVar, cast

T = TypeVar("T")

_LOCK = sys.__dict__.setdefault("_writeragent_process_global_lock", threading.RLock())


def process_global(name: str, factory: Callable[[], T]) -> T:
    """Return ``sys._writeragent_<name>``, calling *factory* once to create it.

    The factory runs under one process-wide lock, so concurrent first callers
    create the object once. A factory that raises stores nothing.
    """
    key = "_writeragent_" + name
    if key in sys.__dict__:
        return cast("T", sys.__dict__[key])
    with _LOCK:
        if key not in sys.__dict__:
            sys.__dict__[key] = factory()
        return cast("T", sys.__dict__[key])
