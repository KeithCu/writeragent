"""WriterAgent extension state and bootstrap entry (import-only; no plugin.main).

``bind_writeragent`` stores the finish hook from the pythonloader exec module.
``plugin.main.bind()`` runs at pythonloader import and from the pytest autouse
fixture in ``tests/conftest.py``. ``bootstrap()`` and ``get_tools()`` raise
``WriterAgentException`` with code ``BOOTSTRAP_FAILED`` until that bind.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Callable

if TYPE_CHECKING:
    from plugin.framework.module_base import ModuleBase
    from plugin.framework.service import ServiceRegistry
    from plugin.framework.tool import ToolRegistry

from plugin.bootstrap import bootstrap_extension
from plugin.framework.constants import EXTENSION_ID_WRITERAGENT
from plugin.framework.main_shared import get_action_handler, report_action_failure

log = logging.getLogger(__name__)

_services: Any = None
_tools: ToolRegistry | None = None
_modules: list[ModuleBase] = []
_terminate_listener: Any | None = None
_finish: Callable[[Any], None] | None = None
_prompt_hooks: Callable[[], None] | None = None

_TESTS_AVAILABLE: bool | None = None


def bind_writeragent(
    *,
    finish: Callable[[Any], None],
    register_prompt_hooks: Callable[[], None] | None = None,
) -> None:
    """Register WriterAgent finish hooks from the pythonloader module (first call wins)."""
    global _finish, _prompt_hooks
    if _finish is not None:
        return
    _finish = finish
    _prompt_hooks = register_prompt_hooks


def is_bound() -> bool:
    """True when a finish handler was registered via bind_writeragent."""
    return _finish is not None


def bootstrap(ctx: Any | None = None) -> None:
    """Bootstraps the service container and modules.

    This is currently triggered eagerly by Jobs.xcu on OnStartApp to ensure the
    MCP server starts automatically in headless mode. We could make this lazy
    again in the future if startup performance becomes a concern.
    """
    if _finish is None:
        from plugin.framework.errors import WriterAgentException

        raise WriterAgentException(
            "WriterAgent bootstrap is not bound to a finish handler.",
            code="BOOTSTRAP_FAILED",
        )
    bootstrap_extension(
        ctx,
        extension_id=EXTENSION_ID_WRITERAGENT,
        register_prompt_hooks=_prompt_hooks,
        finish=_finish,
    )


def bootstrap_complete() -> bool:
    """Return True if bootstrap has fully completed and initialized."""
    from plugin import bootstrap as bootstrap_mod

    return bootstrap_mod.bootstrap_initialized()


def get_services() -> Any:
    global _services
    if _services is None:
        bootstrap()
    if _services is None:
        from plugin.framework.errors import WriterAgentException

        raise WriterAgentException(
            "Service registry is unavailable because bootstrap did not finish.",
            code="BOOTSTRAP_FAILED",
        )
    return _services


def services_or_none() -> ServiceRegistry | None:
    """Return the service registry if bootstrap has already built it, else None.

    Unlike get_services(), this never starts bootstrap. Use it from code that has
    its own answer for "not bootstrapped yet" (pytest, or a module that loads
    before the writer module), so a lookup does not start the whole extension.
    """
    return _services


def get_tools() -> ToolRegistry:
    global _tools
    if _tools is None:
        bootstrap()
    if _tools is None:
        from plugin.framework.errors import WriterAgentException

        raise WriterAgentException(
            "Tool registry is unavailable because bootstrap did not finish.",
            code="BOOTSTRAP_FAILED",
        )
    return _tools


def tests_bundled() -> bool:
    """True when `plugin.tests` test modules are included in this build."""
    global _TESTS_AVAILABLE
    if _TESTS_AVAILABLE is None:
        try:
            import importlib.util

            _TESTS_AVAILABLE = importlib.util.find_spec("plugin.tests.conftest") is not None
        except Exception:
            _TESTS_AVAILABLE = False
    return bool(_TESTS_AVAILABLE)


def _module_for_command(command: str) -> tuple[ModuleBase | None, str]:
    dot = command.find(".")
    if dot <= 0:
        return None, ""
    mod_name, action = command[:dot], command[dot + 1 :]
    for mod in _modules:
        if mod.name == mod_name:
            return mod, action
    return None, action


def dispatch_command(command: str) -> None:
    """Dispatch command using handler registry, falling back to module actions."""
    from plugin import bootstrap as bootstrap_mod

    bootstrap()
    if command.startswith("chatbot.debug_sidebar"):
        # Dev/debug only. sidebar_test_hooks is omitted from release OXTs.
        try:
            from plugin.chatbot.sidebar_test_hooks import handle_debug_sidebar_command

            handle_debug_sidebar_command(command)
        except ImportError:
            log.debug("debug_sidebar omitted (release)")
        except Exception:
            log.exception("chatbot.debug_sidebar failed")
        return
    handler = get_action_handler(command)
    if handler:
        try:
            handler()
        except Exception as e:
            report_action_failure(command, e)
        return

    mod, action = _module_for_command(command)
    if mod is not None:
        mod.on_action(action)
        return

    bootstrap_error = bootstrap_mod.bootstrap_error()
    if bootstrap_error is not None:
        from plugin.framework.errors import WriterAgentException

        raise WriterAgentException(
            f"WriterAgent did not finish starting: {bootstrap_error}",
            code="BOOTSTRAP_FAILED",
        ) from bootstrap_error

    if command.find(".") <= 0:
        log.warning("Unhandled command: %s", command)
        return

    log.warning("No handler or module found for command: %s", command)


def get_menu_text(command: str) -> str | None:
    """Get dynamic menu text for a command, or None for default."""
    bootstrap()
    mod, action = _module_for_command(command)
    if mod is None and command.find(".") <= 0:
        return None

    from plugin.framework.i18n import _

    text = mod.get_menu_text(action) if mod is not None else None
    if text is None:
        # Titles are translated at lookup (locale is not ready at import). None keeps
        # the en-US Addons.xcu string; a raw English literal overwrites the catalog.
        from plugin._manifest import MENU_TITLES, MODULES

        if "." in command:
            mod_name, action_name = command.split(".", 1)
            for m in MODULES:
                if isinstance(m, dict) and m.get("name") == mod_name:
                    action_titles = m.get("action_titles")
                    if isinstance(action_titles, dict):
                        text = action_titles.get(action_name)
                    break
        if text is None:
            text = MENU_TITLES.get(command)
    if text is not None:
        return _(text)
    return None


def get_menu_icon(command: str) -> str | None:
    """Get dynamic icon prefix for a command, or None."""
    bootstrap()
    mod, action = _module_for_command(command)
    if mod is None:
        return None
    return mod.get_menu_icon(action)
