"""Semgrep/Opengrep rule fixtures — expected findings (ruleid)."""

from plugin.framework.worker_pool import run_in_background
from plugin.framework.uno_context import get_desktop
from plugin.framework.thread_guard import background
import threading


def _bad_lambda_worker():
    # ruleid: uno-off-main-thread
    run_in_background(lambda: get_desktop(), name="bad-lambda")


def start_nested():
    def _worker():
        # ruleid: uno-off-main-thread
        get_desktop()

    run_in_background(_worker, name="bad-nested")


@background
def bad_background_worker():
    # ruleid: uno-off-main-thread
    get_desktop()


@background
def bad_cross_function_worker():
    def _touch_desktop_helper():
        # ruleid: uno-off-main-thread
        get_desktop()

    _touch_desktop_helper()


def spawn_raw_thread():
    # ruleid: raw-uno-thread-ban
    threading.Thread(target=bad_background_worker, daemon=True).start()
