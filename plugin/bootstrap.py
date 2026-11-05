"""WriterAgent / LibrePy shared bootstrap sequence and job base."""

from __future__ import annotations

import enum
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable

from plugin.framework.logging import init_logging, log
from plugin.framework.uno_context import get_ctx, set_fallback_ctx, set_package_extension_id


class BootstrapState(enum.Enum):
    UNINITIALIZED = "uninitialized"
    BOOTSTRAPPING = "bootstrapping"
    INITIALIZED = "initialized"


_bootstrap_state: BootstrapState = BootstrapState.UNINITIALIZED
_bootstrap_error: BaseException | None = None


def bootstrap_initialized() -> bool:
    """True when bootstrap_extension has finished successfully."""
    return _bootstrap_state is BootstrapState.INITIALIZED


def bootstrap_error() -> BaseException | None:
    """Last bootstrap failure recorded by finish hooks; None when unset."""
    return _bootstrap_error


def mark_initialized() -> None:
    """Set lifecycle to INITIALIZED. Call only from ``main.py`` / ``main_core.py`` finish hooks."""
    global _bootstrap_state
    _bootstrap_state = BootstrapState.INITIALIZED


def mark_uninitialized() -> None:
    """Set lifecycle to UNINITIALIZED. Call only from ``main.py`` / ``main_core.py`` finish hooks."""
    global _bootstrap_state
    _bootstrap_state = BootstrapState.UNINITIALIZED


def set_bootstrap_error(exc: BaseException) -> None:
    """Store bootstrap failure; does not change ``BootstrapState``. Finish hooks only."""
    global _bootstrap_error
    _bootstrap_error = exc


def clear_bootstrap_error() -> None:
    """Clear stored bootstrap failure. Finish hooks only."""
    global _bootstrap_error
    _bootstrap_error = None


def bootstrap_extension(
    ctx: Any | None = None,
    *,
    extension_id: str,
    register_prompt_hooks: Callable[[], None] | None = None,
    finish: Callable[[Any], None],
) -> None:
    global _bootstrap_state
    # One process, one flag in this module — shared by the pythonloader exec
    # namespace and by plugin.app (importers must not use a second globals() dict).
    # plugin.app.get_menu_text / get_menu_icon depend on returning early when
    # initialized, before the on_python_main_thread check raises BOOTSTRAP_OFF_THREAD.
    if _bootstrap_state is BootstrapState.INITIALIZED:
        return

    from plugin.framework.queue_executor import MainThread

    # A status-listener Dummy upcall is allowed to touch UNO, and it must not enter bootstrap.
    if not MainThread.on_python_main_thread():
        from plugin.framework.errors import WriterAgentException

        raise WriterAgentException(
            "WriterAgent bootstrap must run on the main thread before tools or services are used.",
            code="BOOTSTRAP_OFF_THREAD",
        )

    if _bootstrap_state is BootstrapState.BOOTSTRAPPING:
        return

    # What was wrong: _init_lock wrapped only the _initialized recheck, then
    # released before any init work.
    # How it happened: _bootstrapping was added around the old with-block and
    # the body was dedented out of the lock (bd07cb792).
    # Why this change: bootstrap() already raises BOOTSTRAP_OFF_THREAD on any
    # other thread, and a nested call on this thread returns at BOOTSTRAPPING.
    # The lock excluded nobody. It is also not reentrant, so putting the body
    # back under it would deadlock if a nested call ever reached it.
    _bootstrap_state = BootstrapState.BOOTSTRAPPING
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
            from plugin.framework import gc_policy

            gc_policy.install(main)

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
        if _bootstrap_state is BootstrapState.BOOTSTRAPPING:
            _bootstrap_state = BootstrapState.UNINITIALIZED


class _MainBootstrapJobCore:
    """Shared UNO Job implementation for extension bootstrap."""

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


if TYPE_CHECKING:
    import unohelper
    from com.sun.star.task import XJob, XJobExecutor

    class MainBootstrapJob(_MainBootstrapJobCore, unohelper.Base, XJobExecutor, XJob):
        """Base UNO Job implementation for extension bootstrap."""

else:
    # UNO types are only needed for MainBootstrapJob (loaded inside LibreOffice /
    # when conftest mocks are installed). Keep BootstrapState / bootstrap_extension
    # importable in a clean interpreter for out-of-office scripts and the
    # release-verify subprocess that checks unbound bootstrap without pyuno.
    try:
        import uno  # noqa: F401 — com.sun.star stub requires uno first
        import unohelper
        from com.sun.star.task import XJob, XJobExecutor
    except ImportError:  # headless / release-verify subprocess without pyuno
        unohelper = None  # type: ignore[assignment,misc]

    if unohelper is not None:

        class MainBootstrapJob(_MainBootstrapJobCore, unohelper.Base, XJobExecutor, XJob):
            """Base UNO Job implementation for extension bootstrap."""

    else:

        class MainBootstrapJob(_MainBootstrapJobCore):
            """Placeholder when pyuno is unavailable (headless / release-verify subprocess)."""

            def __init__(self, ctx: Any) -> None:
                raise RuntimeError("MainBootstrapJob requires LibreOffice UNO")
