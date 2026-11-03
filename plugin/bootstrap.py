"""WriterAgent / LibrePy shared bootstrap sequence and job base."""

from __future__ import annotations

import enum
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable

import unohelper

from com.sun.star.task import XJob, XJobExecutor
from plugin.framework.logging import init_logging, log
from plugin.framework.uno_context import get_ctx, set_fallback_ctx, set_package_extension_id


class BootstrapState(enum.Enum):
    UNINITIALIZED = "uninitialized"
    BOOTSTRAPPING = "bootstrapping"
    INITIALIZED = "initialized"


def bootstrap_extension(
    ctx: Any | None = None,
    *,
    extension_id: str,
    register_prompt_hooks: Callable[[], None] | None = None,
    finish: Callable[[Any], None],
    owner: Any,
) -> None:
    # Off-thread get_menu_text and get_menu_icon depend on returning early
    # when initialized, before the on_python_main_thread check raises BOOTSTRAP_OFF_THREAD.
    if owner._bootstrap_state is BootstrapState.INITIALIZED:
        return

    from plugin.framework.queue_executor import MainThread

    # A status-listener Dummy upcall is allowed to touch UNO, and it must not enter bootstrap.
    if not MainThread.on_python_main_thread():
        from plugin.framework.errors import WriterAgentException

        raise WriterAgentException(
            "WriterAgent bootstrap must run on the main thread before tools or services are used.",
            code="BOOTSTRAP_OFF_THREAD",
        )

    if owner._bootstrap_state is BootstrapState.BOOTSTRAPPING:
        return

    # What was wrong: _init_lock wrapped only the _initialized recheck, then
    # released before any init work.
    # How it happened: _bootstrapping was added around the old with-block and
    # the body was dedented out of the lock (bd07cb792).
    # Why this change: bootstrap() already raises BOOTSTRAP_OFF_THREAD on any
    # other thread, and a nested call on this thread returns at BOOTSTRAPPING.
    # The lock excluded nobody. It is also not reentrant, so putting the body
    # back under it would deadlock if a nested call ever reached it.
    owner._bootstrap_state = BootstrapState.BOOTSTRAPPING
    try:
        from plugin.framework.queue_executor import install_from_environ, main

        if ctx is None:
            ctx = get_ctx()

        if ctx is not None:
            # get_ctx() prefers this over uno.getComponentContext(). LibrePy
            # already pins it; without it WriterAgent lookups can hit a
            # different context (no VCL, wrong package, Desktop segfault).
            set_fallback_ctx(ctx)
            install_from_environ()
            main.set_context(ctx)

        set_package_extension_id(extension_id)

        from plugin.bootstrap_ui_hooks import register_framework_ui_hooks

        register_framework_ui_hooks()

        if register_prompt_hooks is not None:
            register_prompt_hooks()

        from plugin.framework.config import init_config

        init_config(ctx)

        from plugin.scripting.native_binaries import ensure_native_binaries_on_path

        ensure_native_binaries_on_path()

        from plugin.framework.i18n import init_i18n

        init_i18n(ctx)

        finish(ctx)
    finally:
        if owner._bootstrap_state is BootstrapState.BOOTSTRAPPING:
            owner._bootstrap_state = BootstrapState.UNINITIALIZED


class MainBootstrapJob(unohelper.Base, XJobExecutor, XJob):
    """Base UNO Job implementation for extension bootstrap."""

    ctx: Any

    def __init__(self, ctx: Any) -> None:
        self.ctx = ctx

    def init_logging(self, ctx: Any) -> None:
        init_logging(ctx)

    def execute(self, Arguments: Any) -> tuple[()]:
        """Called by the Jobs framework on OnStartApp."""
        try:
            self.init_logging(self.ctx)
        except Exception:  # UNO boundary: XJob.execute entry point
            log.exception("MainBootstrapJob.execute: init_logging failed")
        try:
            self.on_start(self.ctx)
        except Exception as e:  # UNO boundary: XJob.execute entry point
            log.exception("MainBootstrapJob.execute failed on_start: %s", e)
        return ()

    def on_start(self, ctx: Any) -> None:
        raise NotImplementedError

    def trigger(self, Event: str) -> None:
        raise NotImplementedError
