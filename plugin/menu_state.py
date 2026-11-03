"""WriterAgent dynamic menu status listeners and toolbar/menu icons.

Manages Uno FeatureStateEvent listeners for commands and dynamic image loading into
ModuleUIConfigurationManagerSupplier image managers.
"""

from __future__ import annotations

import threading
from typing import Any, cast

import uno

from plugin.framework.constants import EXTENSION_ID_WRITERAGENT
from plugin.framework.logging import log
from plugin.framework.menu_icon_dpi import menu_icon_asset_url
from plugin.framework.queue_executor import Policy, main
from plugin.framework.thread_guard import background
from plugin.framework.uno_context import (
    get_ctx,
    get_extension_url,
    get_service_manager,
    uno_same,
)

_DISPATCH_PROTOCOL = f"{EXTENSION_ID_WRITERAGENT}:"

# LO document modules that have their own ImageManager
_IMAGE_MANAGER_MODULES = (
    "com.sun.star.text.TextDocument",
    "com.sun.star.sheet.SpreadsheetDocument",
    "com.sun.star.presentation.PresentationDocument",
    "com.sun.star.drawing.DrawingDocument",
)


def command_from_dispatch_url(url: Any) -> str:
    """Extract command from a WriterAgent dispatch URL.

    Debug sidebar record URL: ``org.extension.writeragent:chatbot.debug_sidebar?RECORD_CLICKED``.
    LO may put the debug op in ``Query``, or in ``Path`` as ``chatbot.debug_sidebar?OP``.
    """
    from plugin.framework.url_utils import dispatch_command_from_url

    command = dispatch_command_from_url(url)
    query = getattr(url, "Query", None) or ""
    if query and "?" not in command:
        return "%s.%s" % (command, query)
    return command


class MenuStatusPublisher:
    """Manages LibreOffice menu and toolbar status listeners for WriterAgent commands."""

    listeners: list[tuple[Any, Any]]
    _lock: threading.Lock
    _icons: MenuIcons

    def __init__(self, icons: MenuIcons) -> None:
        self.listeners = []
        self._lock = threading.Lock()
        self._icons = icons

    def add(self, control: Any, url: Any) -> None:
        """Register a status listener, deduplicating existing entries for the same control and URL."""
        with self._lock:
            # What was wrong: re-registering a status listener for the same control and URL added duplicates.
            # How it happened: addStatusListener unconditionally appended without deduplication.
            # Why this change: drop existing entries matching uno_same and Complete URL before appending.
            self._drop_status_listeners([(control, url)])
            self.listeners.append((control, url))
        # Send current state immediately
        looked_up = self._lookup_menu_text(url, "addStatusListener")
        if looked_up is None:
            return
        command, text = looked_up

        if text is not None:
            try:
                self._fire_status_event(control, url, text, command)
            except Exception as e:  # UNO boundary: initial statusChanged callback
                log.warning("addStatusListener: failed to fire initial status event for %s: %s", command, e)

    def remove(self, control: Any, url: Any) -> None:
        """Unregister a status listener."""
        with self._lock:
            self._drop_status_listeners([(control, url)])

    def _drop_status_listeners(self, pairs: list[tuple[Any, Any]]) -> None:
        # Caller holds self._lock.
        # What was wrong: 'is' never matched PyUNO listener proxies.
        # How it happened: each UNO upcall is a new proxy for the same object.
        # Why this change: uno_same plus Complete is the match used for documents.
        self.listeners[:] = [
            (lstnr, u)
            for lstnr, u in self.listeners
            if not any(uno_same(lstnr, fl) and u.Complete == fu.Complete for fl, fu in pairs)
        ]

    def _prune_failed_status_listeners(self, failed: list[tuple[Any, Any]]) -> None:
        with self._lock:
            self._drop_status_listeners(failed)

    def _lookup_menu_text(self, url: Any, where: str) -> tuple[str, str | None] | None:
        try:
            from plugin.main import get_menu_text

            command = command_from_dispatch_url(url)
            return command, get_menu_text(command)
        except Exception:
            log.exception("%s: menu text failed for %s", where, getattr(url, "Complete", ""))
            return None

    def _fire_status_event(self, listener: Any, url: Any, text: str | None, command: str) -> None:
        """Send a FeatureStateEvent to one listener."""
        from plugin.main import tests_bundled

        ev = cast("Any", uno.createUnoStruct("com.sun.star.frame.FeatureStateEvent"))
        ev.FeatureURL = url
        ev.IsEnabled = True
        if command in {"main.RunFormatTests", "main.RunCalcTests", "main.RunCalcIntegrationTests", "main.RunDrawTests"}:
            ev.IsEnabled = tests_bundled()
        ev.Requery = False
        if text is not None:
            ev.State = text
        listener.statusChanged(ev)

    @background
    def publish(self) -> None:
        """Push current menu text and icons to all registered status listeners.

        Called by modules when state changes (e.g. server start/stop).
        """
        # Snapshot the listeners under the lock, then fire OUTSIDE it. _fire_status_event ->
        # listener.statusChanged is a UNO call to a UI control that marshals to the main thread, so
        # holding _status_lock across it deadlocks: the main thread, while rebinding a toolbar
        # controller (addStatusListener), waits for the same lock this background thread holds. (This is
        # what froze LibreOffice on the toolbar's Accept all.) addStatusListener already fires outside
        # the lock; this makes notify_menu_update consistent.
        with self._lock:
            snapshot = list(self.listeners)
        failed = []
        for listener, url in snapshot:
            # What was wrong: menu text retrieval and statusChanged were grouped in a single try block, so text errors treated the listener as dead.
            # How it happened: get_menu_text exception caused the listener to be appended to failed and pruned from listeners.
            # Why this change: isolate command/text lookup; log and skip on text failure without marking listener failed or pruning it.
            looked_up = self._lookup_menu_text(url, "notify_menu_update")
            if looked_up is None:
                continue
            command, text = looked_up
            try:
                self._fire_status_event(listener, url, text, command)
            except Exception as e:
                # UNO boundary: listener.statusChanged callback
                failed.append((listener, url))
                log.warning(
                    "notify_menu_update: failed to fire status event for %s: %s",
                    command,
                    e,
                )
        if failed:
            # What was wrong: this prune called uno_same on the pool worker.
            # uno_same is @main_thread_only, so a dev build raised before the
            # list was filtered and the icon refresh below never ran.
            # How it happened: notify_menu_update is @background and is only
            # started with run_in_background. The unit test calls it on the
            # main thread, where the guard does not fire.
            # Why this change: post the comparison to the main thread. A direct
            # call that is already on the main thread still prunes before return.
            # post, not call: a blocking marshal during startup deadlocks
            # AsyncCallback lazy-init, same as _update_menu_icons.
            if main.is_current():
                self._prune_failed_status_listeners(failed)
            else:
                try:
                    # What was wrong: prune post defaulted to the current send scope and was cancelled if user clicked Stop.
                    # How it happened: main.post omitted bound_scope, inheriting the worker thread's copied contextvar.
                    # Why this change: pass bound_scope=None because listener cleanup is UI maintenance, not send work.
                    main.post(self._prune_failed_status_listeners, list(failed), bound_scope=None)
                except Exception:
                    # UNO boundary: notify_menu_update post to main thread
                    log.exception("notify_menu_update: failed to post listener prune")
        if main.policy is Policy.production:
            from plugin.framework.worker_pool import run_in_background

            run_in_background(self._icons.post_apply)


class MenuIcons:
    """Manages LibreOffice XImageManager icon updates for WriterAgent toolbar/menu actions."""

    _loaded_icon_graphics: dict[tuple[str, int], Any]
    _icon_apply_stamp: tuple[tuple[tuple[str, str], ...], int] | None

    def __init__(self) -> None:
        self._loaded_icon_graphics = {}
        self._icon_apply_stamp = None

    @staticmethod
    def _icon_apply_signature(
        icon_cmds: dict[str, tuple[str, str]], px: int
    ) -> tuple[tuple[tuple[str, str], ...], int]:
        return (tuple(sorted((url, prefix) for url, (_mod, prefix) in icon_cmds.items())), px)

    def _collect_icon_commands(self) -> dict[str, tuple[str, str]]:
        """Collect all command URLs that declare icons in their manifest.

        Returns {command_url: (module_name, icon_prefix)} for the current state.
        """
        try:
            from plugin._manifest import MODULES
        except ImportError:
            return {}

        result: dict[str, tuple[str, str]] = {}

        from plugin.main import get_menu_icon

        for m in cast("list[dict[str, Any]]", MODULES):
            mod_name = str(m["name"])
            action_icons = cast("dict[str, str]", m.get("action_icons", {}))
            for action_name, default_icon in action_icons.items():
                cmd_url = "%s%s.%s" % (_DISPATCH_PROTOCOL, mod_name, action_name)
                # Ask the module for dynamic icon (may override the default)
                dynamic = get_menu_icon("%s.%s" % (mod_name, action_name))
                result[cmd_url] = (mod_name, dynamic or default_icon)
        return result

    def _load_icon_graphic(self, module_name: str, icon_filename: str, ctx: Any) -> Any:
        """Load a PNG icon from OXT assets/ as XGraphic."""
        try:
            from com.sun.star.beans import PropertyValue

            ctx_any = cast("Any", ctx)
            smgr = get_service_manager(ctx_any)
            if smgr is None:
                log.warning("_load_icon_graphic: get_service_manager returned None for %s/%s", module_name, icon_filename)
                return None
            gp = cast("Any", smgr).createInstanceWithContext("com.sun.star.graphic.GraphicProvider", ctx_any)
            ext_url = get_extension_url(ctx_any)
            if not ext_url:
                return None
            pv = PropertyValue()
            pv.Name = "URL"
            pv.Value = menu_icon_asset_url(ext_url, icon_filename)
            graphic = gp.queryGraphic((pv,))
            if graphic is None:
                log.warning("_load_icon_graphic: GraphicProvider returned None for %s (%s)", pv.Value, module_name)
            return graphic
        except Exception as e:  # UNO boundary: GraphicProvider.queryGraphic
            log.warning("_load_icon_graphic failed for %s/%s: %s", module_name, icon_filename, e)
            return None

    def apply(self) -> None:
        """Push current-state icons into every module's ImageManager (UNO main thread)."""
        try:
            # A bootstrap-less uno.getComponentContext() is a different context
            # (no VCL, wrong package). get_ctx() is the context bootstrap stored.
            ctx = get_ctx()
            if ctx is None:
                log.warning("MenuIcons.apply: get_ctx() returned None")
                return

            icon_cmds = self._collect_icon_commands()
            if not icon_cmds:
                return

            from plugin.framework.menu_icon_dpi import (
                image_type_for_pixel_size,
                menu_icon_filename,
                resolve_menu_icon_pixel_size,
            )

            # One-shot DPI probe → pixel size (16 at 1×, 32 on HiDPI).
            # A probe miss caches 32 until a real VCL DPI reading. Warm
            # StyleSettings so startup does not stick on that weak reading.
            from plugin.framework.appearance import get_style_window

            get_style_window(ctx=ctx)
            icon_px = resolve_menu_icon_pixel_size(ctx)
            image_type = image_type_for_pixel_size(icon_px)

            # Each dispatch used to re-query every PNG and rewrite four image
            # managers; the stamp skips that when the command-to-prefix map and the
            # pixel size are unchanged, and it stays unset when a load or replace
            # failed so the next click retries.
            if self._icon_apply_signature(icon_cmds, icon_px) == self._icon_apply_stamp:
                return

            # Group by (module, prefix) to avoid loading the same graphic twice
            key_cmds: dict[tuple[str, str], list[str]] = {}  # (mod_name, prefix) -> [cmd_urls]
            for cmd_url, (mod_name, prefix) in icon_cmds.items():
                key_cmds.setdefault((mod_name, prefix), []).append(cmd_url)

            # Load graphics
            apply_failed = False
            key_graphics = {}
            for key in key_cmds:
                mod_name, prefix = key
                cache_key = (prefix, icon_px)
                graphic = self._loaded_icon_graphics.get(cache_key)
                if graphic is None:
                    filename = menu_icon_filename(prefix, icon_px, ctx)
                    graphic = self._load_icon_graphic(mod_name, filename, ctx)
                    if graphic is not None:
                        self._loaded_icon_graphics[cache_key] = graphic
                if graphic is not None:
                    key_graphics[key] = graphic
                else:
                    apply_failed = True
                    log.warning("Icon graphic is None for assets/%s (module %s)", prefix, mod_name)

            if not key_graphics:
                return

            smgr = get_service_manager(ctx)
            if smgr is None:
                log.warning("MenuIcons.apply: get_service_manager returned None")
                return

            supplier = cast("Any", smgr).createInstanceWithContext("com.sun.star.ui.ModuleUIConfigurationManagerSupplier", ctx)
            for mod_id in _IMAGE_MANAGER_MODULES:
                try:
                    cfg_mgr = supplier.getUIConfigurationManager(mod_id)
                    img_mgr = cfg_mgr.getImageManager()
                    for key, cmds in key_cmds.items():
                        graphic = key_graphics.get(key)
                        if not graphic:
                            continue
                        # Menus usually fetch SIZE_DEFAULT; also stamp LARGE/32 when we
                        # chose a bigger asset so HiDPI/toolbars can pick either.
                        type_set = {0, int(image_type)}
                        for cmd in cmds:
                            for itype in sorted(type_set):
                                try:
                                    if img_mgr.hasImage(itype, cmd):
                                        img_mgr.replaceImages(itype, (cmd,), (graphic,))
                                    else:
                                        img_mgr.insertImages(itype, (cmd,), (graphic,))
                                except Exception as e:
                                    # UNO boundary: XImageManager image insertion/replacement
                                    apply_failed = True
                                    log.warning(
                                        "MenuIcons.apply: failed to insert/replace image for %s type=%s: %s",
                                        cmd,
                                        itype,
                                        e,
                                    )
                except Exception as e:
                    # UNO boundary: ModuleUIConfigurationManagerSupplier / getImageManager
                    apply_failed = True
                    log.warning("MenuIcons.apply: failed to process ImageManager for %s: %s", mod_id, e)

            if not apply_failed:
                self._icon_apply_stamp = self._icon_apply_signature(icon_cmds, icon_px)
        except Exception:
            # UNO boundary: outer ImageManager update
            log.exception("MenuIcons.apply: outer exception")

    @background
    def post_apply(self) -> None:
        """Background entrypoint: marshal UNO icon updates onto the main thread.

        Use post (not blocking execute): at startup the UI thread may be inside
        ``set_context`` / ``init_config``, and a blocking marshal can deadlock
        with AsyncCallback lazy-init.
        """
        from plugin.framework.queue_executor import main

        # What was wrong: icon updates inherited the worker thread's send scope and were cancelled when Stop was clicked.
        # How it happened: main.post omitted bound_scope, binding to the active send task contextvar.
        # Why this change: pass bound_scope=None because menu icon application is UI maintenance, not send work.
        main.post(self.apply, bound_scope=None)
