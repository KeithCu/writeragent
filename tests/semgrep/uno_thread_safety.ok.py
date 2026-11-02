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


def approved_vcl_pump(ctx):
    from plugin.framework.uno_context import process_events_to_idle

    # ok: raw-process-events-to-idle
    process_events_to_idle(ctx)


def broad_except_reraises_disposed(doc):
    from plugin.framework.errors import reraise_if_disposed

    # ok: broad-except-swallows-disposal
    try:
        doc.getText()
    except Exception as exc:
        reraise_if_disposed(exc)
        return None


def broad_except_reraises(doc):
    # ok: broad-except-swallows-disposal
    try:
        doc.getText()
    except Exception:
        raise


def narrow_except(doc):
    # ok: broad-except-swallows-disposal
    try:
        doc.getText()
    except ValueError:
        return None
