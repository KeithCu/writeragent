"""WriterAgent extension state and bootstrap entry (import-only; no plugin.main).

``bind_writeragent`` stores the finish hook from the pythonloader exec module.
``plugin.main.bind()`` runs at pythonloader import and from the pytest autouse
fixture in ``tests/conftest.py``. ``bootstrap()`` and ``get_tools()`` raise
``WriterAgentException`` with code ``BOOTSTRAP_FAILED`` until that bind.
"""

from __future__ import annotations

import importlib.util
import logging
from typing import TYPE_CHECKING, Any, Callable

if TYPE_CHECKING:
    from plugin.framework.module_base import ModuleBase
    from plugin.framework.service import ServiceRegistry
    from plugin.framework.tool import ToolRegistry

from plugin.bootstrap import bootstrap_extension
from plugin.framework.constants import EXTENSION_ID_WRITERAGENT
from plugin.framework.errors import WriterAgentException
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
    """Build the service container and modules.

    Jobs.xcu triggers this on OnStartApp so the MCP server starts in headless mode.
    """
    if _finish is None:
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
    if _services is None:
        bootstrap()
    if _services is None:
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
    if _tools is None:
        bootstrap()
    if _tools is None:
        raise WriterAgentException(
            "Tool registry is unavailable because bootstrap did not finish.",
            code="BOOTSTRAP_FAILED",
        )
    return _tools


def set_services(services: ServiceRegistry) -> None:
    """Publish the service registry that ``plugin.main`` bootstrap finish builds."""
    global _services
    _services = services


def set_tools(tools: ToolRegistry) -> None:
    """Publish the tool registry that ``plugin.main`` bootstrap finish builds."""
    global _tools
    _tools = tools


def loaded_modules() -> list[ModuleBase]:
    """The live list of loaded modules; bootstrap finish extends it in place."""
    return _modules


def terminate_listener() -> Any | None:
    """The desktop XTerminateListener installed at bootstrap, or None before it is installed."""
    return _terminate_listener


def set_terminate_listener(listener: Any) -> None:
    """Record the installed desktop XTerminateListener; it is never removed for the process lifetime."""
    global _terminate_listener
    _terminate_listener = listener


def tests_bundled() -> bool:
    """True when `plugin.tests` test modules are included in this build."""
    global _TESTS_AVAILABLE
    if _TESTS_AVAILABLE is None:
        try:
            _TESTS_AVAILABLE = importlib.util.find_spec("plugin.tests.conftest") is not None
        except ModuleNotFoundError:
            _TESTS_AVAILABLE = False
    return bool(_TESTS_AVAILABLE)


def _module_for_command(command: str) -> tuple[ModuleBase | None, str, str]:
    dot = command.find(".")
    if dot <= 0:
        return None, "", ""
    mod_name, action = command[:dot], command[dot + 1 :]
    for mod in _modules:
        if mod.name == mod_name:
            return mod, mod_name, action
    return None, mod_name, action


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

    mod, _mod_name, action = _module_for_command(command)
    if mod is not None:
        mod.on_action(action)
        return

    bootstrap_error = bootstrap_mod.bootstrap_error()
    if bootstrap_error is not None:
        raise WriterAgentException(
            f"WriterAgent did not finish starting: {bootstrap_error}",
            code="BOOTSTRAP_FAILED",
        ) from bootstrap_error

    log.warning("Unhandled command: %s", command)


def get_menu_text(command: str) -> str | None:
    """Get dynamic menu text for a command, or None for default."""
    bootstrap()
    mod, mod_name, action = _module_for_command(command)
    if not mod_name:
        return None

    from plugin.framework.i18n import _

    text = mod.get_menu_text(action) if mod is not None else None
    if text is None:
        # Titles are translated at lookup (locale is not ready at import). None keeps
        # the en-US Addons.xcu string; a raw English literal overwrites the catalog.
        from plugin._manifest import MENU_TITLES, MODULES

        for m in MODULES:
            if isinstance(m, dict) and m.get("name") == mod_name:
                action_titles = m.get("action_titles")
                if isinstance(action_titles, dict):
                    text = action_titles.get(action)
                break
        if text is None:
            text = MENU_TITLES.get(command)
    if text is not None:
        return _(text)
    return None


def get_menu_icon(command: str) -> str | None:
    """Get dynamic icon prefix for a command, or None."""
    bootstrap()
    mod, _mod_name, action = _module_for_command(command)
    if mod is None:
        return None
    return mod.get_menu_icon(action)
