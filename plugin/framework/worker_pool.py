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

"""Centralized management for background worker threads."""

import logging
import threading
import traceback

log = logging.getLogger("writeragent.framework.worker_pool")

def run_in_background(func, *args, name=None, error_callback=None, daemon=True, **kwargs):
    """
    Spawns a background thread to execute a function, catching any exceptions.

    :param func: The callable to execute.
    :param args: Positional arguments for func.
    :param name: Optional thread name.
    :param error_callback: Optional callable(Exception) to run if func raises.
    :param daemon: Whether the thread should be a daemon (default True).
    :param kwargs: Keyword arguments for func.
    :return: The spawned threading.Thread instance.
    """
    def _worker():
        try:
            func(*args, **kwargs)
        except Exception as e:
            log.error("Unhandled exception in background worker '%s': %s\n%s",
                      name or func.__name__, e, traceback.format_exc())
            if error_callback:
                try:
                    error_callback(e)
                except Exception as ec:
                    log.error("Error in error_callback for '%s': %s", name or func.__name__, ec)

    thread_name = name or f"worker-{getattr(func, '__name__', 'anon')}"
    t = threading.Thread(target=_worker, name=thread_name, daemon=daemon)
    t.start()
    return t
