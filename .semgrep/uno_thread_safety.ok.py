"""Semgrep/Opengrep rule fixtures — must not match (ok)."""

from plugin.framework.worker_pool import run_in_background
from plugin.framework.uno_context import get_desktop
from plugin.framework.queue_executor import execute_on_main_thread
from plugin.framework.thread_guard import background


@background
def marshalled_worker():
    # ok: uno-off-main-thread
    execute_on_main_thread(get_desktop)


def start_marshalled():
    def _worker():
        # ok: uno-off-main-thread
        execute_on_main_thread(get_desktop)

    run_in_background(_worker, name="good-nested")


def main_path():
    # ok: uno-off-main-thread
    get_desktop()


@background
def good_cross_function_worker():
    def _marshalled_helper():
        # ok: uno-off-main-thread
        execute_on_main_thread(get_desktop)

    _marshalled_helper()
