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


_terminate_listener: Any | None = None


def _install_terminate_listener(ctx: Any) -> None:
    """Register a desktop XTerminateListener to clean up child processes on app exit."""
    global _terminate_listener
    # The first successful addTerminateListener is process-lifetime and is not
    # removed or replaced; a failed install leaves the global None so the next
    # bootstrap retries.
    if _terminate_listener is not None:
        return
    try:
        from plugin.framework.uno_context import get_desktop

        desktop = get_desktop(ctx)
        if desktop is None:
            return

        from plugin.framework.uno_listeners import BaseTerminateListener

        class _TerminateListener(BaseTerminateListener):
            def notifyTermination(self, Event: Any) -> None:
                try:
                    from plugin.audio.kokoro_pool import shutdown_kokoro_pool

                    shutdown_kokoro_pool()
                except Exception:
                    # UNO boundary: XTerminateListener.notifyTermination callback
                    log.exception("Error shutting down Kokoro pool on desktop termination")

                # shutdown_all kill_trees registered children and does not signal
                # register=False venv workers. Do not call
                # PythonWorkerManager.shutdown_all here either: it joins the reap on
                # this thread. Idle children exit on stdin EOF. Linux native stalls
                # use PR_SET_PDEATHSIG in the harness.
                try:
                    from plugin.framework.managed_subprocess import shutdown_all

                    shutdown_all()
                except Exception:
                    # UNO boundary: XTerminateListener.notifyTermination callback
                    log.exception("Error shutting down managed subprocesses on desktop termination")

        listener = _TerminateListener()
        desktop.addTerminateListener(listener)
        _terminate_listener = listener
        import sys

        plugin_main = sys.modules.get("plugin.main")
        if plugin_main is not None:
            setattr(plugin_main, "_terminate_listener", listener)
    except Exception:
        # UNO boundary: desktop.addTerminateListener registration
        log.exception("Failed to install terminate listener")


def bootstrap_extension(
    ctx: Any | None = None,
    *,
    extension_id: str,
    load_modules: bool,
    register: Callable[[], None],
    owner: Any,
) -> None:
    # Off-thread get_menu_text and get_menu_icon depend on returning early
    # when initialized, before the on_python_main_thread check raises BOOTSTRAP_OFF_THREAD.
    if owner._bootstrap_state is BootstrapState.INITIALIZED:
        return

    from plugin.framework.thread_guard import on_python_main_thread

    # A status-listener Dummy upcall is allowed to touch UNO, and it must not enter bootstrap.
    if not on_python_main_thread():
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
    # other thread, and a nested call on this thread returns at _bootstrapping.
    # The lock excluded nobody. It is also not reentrant, so putting the body
    # back under it would deadlock if a nested call ever reached it.
    owner._bootstrap_state = BootstrapState.BOOTSTRAPPING
    try:
        # 1. Basic UNO context
        from plugin.framework.queue_executor import Policy, install_from_environ, main

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

        if load_modules:
            from plugin.writeragent_hooks import register_writeragent_prompt_hooks

            register_writeragent_prompt_hooks()

        from plugin.framework.config import init_config

        init_config(ctx)

        from plugin.scripting.native_binaries import ensure_native_binaries_on_path

        ensure_native_binaries_on_path()

        from plugin.framework.i18n import init_i18n

        init_i18n(ctx)

        if load_modules:
            # 2. Service Container
            from plugin.framework.service import ServiceRegistry

            services = ServiceRegistry()
            services.register("uno", ctx)

            # Skip terminate listener when running under the eval harness.
            # The eval client already has a remote Desktop; this listener would shut down client subprocesses on office teardown.
            if main.policy is not Policy.eval_harness:
                install_term = getattr(owner, "_install_terminate_listener", _install_terminate_listener)
                install_term(ctx)

            # 3. Core Services (Framework)
            from plugin.doc.document_helpers import DocumentService
            from plugin.framework.event_bus import get_event_bus

            services.register("document", DocumentService())
            services.register("events", get_event_bus())

            # 4. Tool Registry
            from plugin.writeragent_hooks import register_writeragent_tool_filters
            from plugin.framework.tool import ToolRegistry

            tools = ToolRegistry(services)
            register_writeragent_tool_filters(tools)
            services.register("tools", tools)

            # 4b. Main Thread Execution Service
            services.register("main_thread", main)

            owner._services = services
            owner._tools = tools

            # Set initialized early to prevent recursive calls from re-running bootstrap
            # but after _services and _tools are created.
            owner._bootstrap_state = BootstrapState.INITIALIZED

            # 5. Load manifest and initialize modules
            from plugin.framework.module_base import ModuleLoader

            module_phase_failed = False
            try:
                owner._modules.extend(ModuleLoader.load_modules(services))
            except Exception as exc:
                module_phase_failed = True
                log.exception("bootstrap failed while loading modules")
                owner._bootstrap_error = exc
                if not owner._modules:
                    owner._bootstrap_state = BootstrapState.UNINITIALIZED
                    return
                # Fall through so core handlers still register; the kept error makes an
                # unhandled command raise BOOTSTRAP_FAILED.

            # 6. Background phase: modules that start listeners/servers (e.g. McpModule when MCP enabled).
            # Isolate each module: one failed start must not keep later servers/listeners from starting.
            for mod in owner._modules:
                try:
                    mod.start_background(services)
                except Exception as exc:
                    module_phase_failed = True
                    log.exception("start_background failed for module %s", getattr(mod, "name", "?"))
                    if owner._bootstrap_error is None:
                        owner._bootstrap_error = exc

            try:
                # Subscribe to menu:update for dynamic menu text + icons.
                from plugin.framework.worker_pool import run_in_background

                services.get("events").subscribe(
                    "menu:update", lambda **kw: run_in_background(owner.menu_status.publish)
                )

                if main.policy is Policy.production:
                    run_in_background(owner.menu_icons.post_apply)

                if main.policy is not Policy.eval_harness:
                    register()
            except Exception as exc:
                log.exception("bootstrap failed after the init flag was set")
                if owner._bootstrap_error is None:
                    owner._bootstrap_error = exc
                return

            if not module_phase_failed:
                owner._bootstrap_error = None
        else:
            register()
            owner._bootstrap_state = BootstrapState.INITIALIZED
            owner._bootstrap_error = None
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
