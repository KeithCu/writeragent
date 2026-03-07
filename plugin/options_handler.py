"""XContainerWindowEventHandler for Tools > Options > LocalWriter pages.

Each module with config gets its own Options page (XDL generated at build time).
A hidden ``__module__`` control in each XDL identifies which module the page belongs to.

List-detail pages (widget: list_detail) are identified by an additional
``__list_detail__`` hidden control. They manage a JSON-serialized list of
items with add/remove/edit functionality.

The handler reads/writes config via ConfigService and emits config:changed events.

This file is registered as a UNO component in META-INF/manifest.xml.
"""

import json
import logging
import os
import sys

# Ensure plugin parent is on path so "plugin.xxx" imports work
_plugin_dir = os.path.dirname(os.path.abspath(__file__))
_parent = os.path.dirname(_plugin_dir)
if _parent not in sys.path:
    sys.path.insert(0, _parent)

import unohelper
from com.sun.star.awt import (
    XContainerWindowEventHandler, XActionListener, XItemListener)
from com.sun.star.lang import XServiceInfo

log = logging.getLogger("localwriter.options")


# ── List-detail state and UNO listeners ──────────────────────────────


class _ListDetailState:
    """State for a list_detail widget on an Options page."""

    __slots__ = ("items", "item_fields", "field_name", "xWindow",
                 "selected_idx", "full_key", "name_field",
                 "label_func", "_updating", "_item_options")

    def __init__(self, items, item_fields, field_name, full_key, xWindow,
                 name_field="name", label_func=None):
        self.items = items
        self.item_fields = item_fields
        self.field_name = field_name
        self.full_key = full_key
        self.xWindow = xWindow
        self.selected_idx = -1
        self.name_field = name_field
        self.label_func = label_func
        self._updating = False
        self._item_options = {}  # fname -> resolved options list


class _LDItemListener(unohelper.Base, XItemListener):
    """Listbox item selection listener for list_detail."""

    def __init__(self, state, handler):
        self._state = state
        self._handler = handler

    def itemStateChanged(self, evt):
        if self._state._updating:
            return
        try:
            listbox = self._handler._get_control(
                self._state.xWindow, "lst_%s" % self._state.field_name)
            if listbox:
                new_idx = listbox.getSelectedItemPos()
                if new_idx != self._state.selected_idx:
                    self._handler._ld_on_select(self._state, new_idx)
        except Exception:
            log.exception("itemStateChanged failed")

    def disposing(self, evt):
        pass


class _LDAddListener(unohelper.Base, XActionListener):
    """Add button listener for list_detail."""

    def __init__(self, state, handler):
        self._state = state
        self._handler = handler

    def actionPerformed(self, evt):
        try:
            self._handler._ld_on_add(self._state)
        except Exception:
            log.exception("Add button failed")

    def disposing(self, evt):
        pass


class _LDRemoveListener(unohelper.Base, XActionListener):
    """Remove button listener for list_detail."""

    def __init__(self, state, handler):
        self._state = state
        self._handler = handler

    def actionPerformed(self, evt):
        try:
            self._handler._ld_on_remove(self._state)
        except Exception:
            log.exception("Remove button failed")

    def disposing(self, evt):
        pass


class _LDApplyListener(unohelper.Base, XActionListener):
    """Apply button listener for list_detail — saves current detail fields."""

    def __init__(self, state, handler):
        self._state = state
        self._handler = handler

    def actionPerformed(self, evt):
        try:
            self._handler._ld_save_current_detail(self._state)
        except Exception:
            log.exception("Apply button failed")

    def disposing(self, evt):
        pass


# ── Browse button listener ───────────────────────────────────────────


class _BrowseListener(unohelper.Base, XActionListener):
    """Opens a FilePicker and writes the result to a paired textfield."""

    def __init__(self, text_ctrl, widget, file_filter=""):
        self._text_ctrl = text_ctrl
        self._widget = widget  # "file" or "folder"
        self._file_filter = file_filter

    def actionPerformed(self, evt):
        try:
            from plugin.framework.uno_context import get_ctx
            ctx = get_ctx()
            if not ctx:
                return
            smgr = ctx.ServiceManager
            if self._widget == "folder":
                picker = smgr.createInstanceWithContext(
                    "com.sun.star.ui.dialogs.FolderPicker", ctx)
                current = self._text_ctrl.getModel().Text
                if current:
                    import uno
                    picker.setDisplayDirectory(
                        uno.systemPathToFileUrl(current))
                if picker.execute() == 1:
                    import uno
                    path = uno.fileUrlToSystemPath(picker.getDirectory())
                    self._text_ctrl.getModel().Text = path
            else:
                picker = smgr.createInstanceWithContext(
                    "com.sun.star.ui.dialogs.FilePicker", ctx)
                if self._file_filter:
                    parts = self._file_filter.split("|")
                    for i in range(0, len(parts) - 1, 2):
                        picker.appendFilter(parts[i].strip(),
                                            parts[i + 1].strip())
                current = self._text_ctrl.getModel().Text
                if current:
                    import uno
                    import os
                    parent = os.path.dirname(current)
                    if parent:
                        picker.setDisplayDirectory(
                            uno.systemPathToFileUrl(parent))
                if picker.execute() == 1:
                    import uno
                    files = picker.getFiles()
                    if files:
                        path = uno.fileUrlToSystemPath(files[0])
                        self._text_ctrl.getModel().Text = path
        except Exception:
            log.exception("Browse action failed")

    def disposing(self, evt):
        pass


# ── Main handler ─────────────────────────────────────────────────────


class OptionsHandler(unohelper.Base, XContainerWindowEventHandler, XServiceInfo):
    """Handles initialize / ok / back events for all LocalWriter Options pages."""

    IMPLE_NAME = "org.extension.localwriter.OptionsHandler"
    SERVICE_NAMES = (IMPLE_NAME,)

    def __init__(self, ctx):
        self.ctx = ctx
        self._cached_values = {}  # full_key -> value at init time
        self._ld_states = {}      # full_key -> _ListDetailState

    # ── XContainerWindowEventHandler ─────────────────────────────────

    def callHandlerMethod(self, xWindow, eventObject, methodName):
        log.debug("OptionsHandler: method=%s event=%s", methodName, eventObject)
        if methodName != "external_event":
            return False
        try:
            if eventObject == "initialize":
                self._on_initialize(xWindow)
                return True
            elif eventObject == "ok":
                self._on_ok(xWindow)
                return True
            elif eventObject == "back":
                self._on_back(xWindow)
                return True
        except Exception:
            log.exception("OptionsHandler event '%s' failed", eventObject)
        return False

    def getSupportedMethodNames(self):
        return ("external_event",)

    # ── XServiceInfo ─────────────────────────────────────────────────

    def supportsService(self, name):
        return name in self.SERVICE_NAMES

    def getImplementationName(self):
        return self.IMPLE_NAME

    def getSupportedServiceNames(self):
        return self.SERVICE_NAMES

    # ── Event handlers ───────────────────────────────────────────────

    def _on_initialize(self, xWindow):
        """Load config values into the Options page controls."""
        module_name = self._detect_module(xWindow)
        log.info("Options page initialize: module=%s", module_name)
        if not module_name:
            log.warning("Could not detect module from Options page")
            return

        # Check if this is a list_detail page
        ld_field = self._detect_list_detail(xWindow)
        if ld_field:
            self._ld_on_initialize(xWindow, module_name, ld_field)
            return

        manifest = self._get_manifest()
        mod_config = self._get_module_config(manifest, module_name)
        config_svc = self._get_config_service()

        if mod_config:
            self._load_module_fields(xWindow, module_name, mod_config,
                                     config_svc, prefix="")

        # Handle inline submodules (e.g. tunnel.bore, tunnel.ngrok)
        inline_names = self._detect_inline_modules(xWindow)
        if inline_names:
            for child_name in inline_names:
                child_config = self._get_module_config(manifest, child_name)
                if not child_config:
                    continue
                child_safe = child_name.replace(".", "_")
                self._load_module_fields(
                    xWindow, child_name, child_config, config_svc,
                    prefix=child_safe + "__")

    def _load_module_fields(self, xWindow, module_name, mod_config,
                            config_svc, prefix=""):
        """Load config fields into controls, with optional ID prefix."""
        for field_name, schema in mod_config.items():
            widget = schema.get("widget", "text")
            if widget == "list_detail":
                if not prefix and schema.get("inline"):
                    self._ld_on_initialize(xWindow, module_name, field_name)
                continue  # non-inline handled on separate page

            full_key = "%s.%s" % (module_name, field_name)

            # Read via ConfigService (uses global ctx via get_ctx())
            val = config_svc.get(full_key) if config_svc else None
            if val is None:
                val = schema.get("default")

            self._cached_values[full_key] = val

            ctrl_id = prefix + field_name
            ctrl = self._get_control(xWindow, ctrl_id)
            if ctrl is None:
                continue

            try:
                if widget == "checkbox":
                    ctrl.getModel().State = 1 if val else 0
                elif widget in ("text", "password", "file", "folder"):
                    ctrl.getModel().Text = str(val) if val else ""
                elif widget == "textarea":
                    ctrl.getModel().Text = str(val) if val else ""
                elif widget in ("number", "slider"):
                    ctrl.getModel().Value = float(val) if val is not None else 0
                elif widget == "select":
                    resolved = self._resolve_options(schema)
                    self._populate_select(ctrl, resolved, val)
                elif widget == "combo":
                    resolved = self._resolve_options(schema)
                    self._populate_combo(ctrl, resolved, val)
            except Exception:
                log.exception("Error loading %s", full_key)

            # Wire browse button for file/folder widgets
            if widget in ("file", "folder"):
                btn = self._get_control(xWindow, "btn_%s" % ctrl_id)
                if btn and ctrl:
                    btn.addActionListener(_BrowseListener(
                        ctrl, widget, schema.get("file_filter", "")))

    def _on_ok(self, xWindow):
        """Write control values via ConfigService and emit event."""
        module_name = self._detect_module(xWindow)
        if not module_name:
            return

        # Check if this is a list_detail page
        ld_field = self._detect_list_detail(xWindow)
        if ld_field:
            self._ld_on_ok(xWindow, module_name, ld_field)
            return

        manifest = self._get_manifest()
        mod_config = self._get_module_config(manifest, module_name)

        config_svc = self._get_config_service()
        if not config_svc:
            log.error("_on_ok: ConfigService not available")
            return

        changes = {}
        if mod_config:
            changes = self._save_module_fields(xWindow, module_name, mod_config,
                                               prefix="")

        # Handle inline submodules
        inline_names = self._detect_inline_modules(xWindow)
        if inline_names:
            for child_name in inline_names:
                child_config = self._get_module_config(manifest, child_name)
                if not child_config:
                    continue
                child_safe = child_name.replace(".", "_")
                child_changes = self._save_module_fields(
                    xWindow, child_name, child_config,
                    prefix=child_safe + "__")
                changes.update(child_changes)

        if changes:
            diffs = config_svc.set_batch(changes, old_values=self._cached_values)
            # Update cache with new values
            for key, val in changes.items():
                self._cached_values[key] = val
            if diffs:
                log.info("_on_ok: %d change(s) saved for %s", len(diffs), module_name)

    def _save_module_fields(self, xWindow, module_name, mod_config, prefix=""):
        """Read control values for a module's fields. Returns dict of changes."""
        changes = {}
        for field_name, schema in mod_config.items():
            widget = schema.get("widget", "text")
            if widget == "list_detail":
                if not prefix and schema.get("inline"):
                    self._ld_on_ok(xWindow, module_name, field_name)
                continue

            full_key = "%s.%s" % (module_name, field_name)
            field_type = schema.get("type", "string")

            ctrl_id = prefix + field_name
            ctrl = self._get_control(xWindow, ctrl_id)
            if ctrl is None:
                continue

            try:
                resolved = self._resolve_options(schema) if widget == "select" else schema
                new_val = self._read_control(ctrl, widget, field_type, resolved)
                changes[full_key] = new_val
            except Exception:
                log.exception("_on_ok: read control FAILED for %s", full_key)
        return changes

    def _on_back(self, xWindow):
        """Reload values (revert unsaved changes)."""
        self._on_initialize(xWindow)

    # ── List-detail page handlers ────────────────────────────────────

    def _ld_on_initialize(self, xWindow, module_name, field_name):
        """Initialize a list_detail page."""
        full_key = "%s.%s" % (module_name, field_name)

        manifest = self._get_manifest()
        mod_config = self._get_module_config(manifest, module_name)
        schema = mod_config.get(field_name, {})
        item_fields = schema.get("item_fields", {})
        name_field = schema.get("name_field", "name")

        # Resolve label_func (optional Python callable path)
        label_func = self._resolve_label_func(schema.get("label_func"))

        config_svc = self._get_config_service()
        json_str = config_svc.get(full_key) if config_svc else None
        if json_str is None:
            json_str = schema.get("default", "[]")

        try:
            items = json.loads(json_str)
            if not isinstance(items, list):
                items = []
        except (json.JSONDecodeError, TypeError):
            items = []

        self._cached_values[full_key] = json_str

        state = _ListDetailState(
            items=items,
            item_fields=item_fields,
            field_name=field_name,
            full_key=full_key,
            xWindow=xWindow,
            name_field=name_field,
            label_func=label_func,
        )
        self._ld_states[full_key] = state

        # Pre-resolve options for select/combo item_fields (options_from / options_provider)
        for fname, fschema in item_fields.items():
            if fschema.get("widget") not in ("select", "combo"):
                continue
            options = list(fschema.get("options", []))
            # options_from: reference another config field's options
            ref = fschema.get("options_from")
            if ref:
                ref_schema = mod_config.get(ref, {})
                ref_resolved = self._resolve_options(ref_schema)
                ref_options = ref_resolved.get("options", [])
                seen = {o.get("value") for o in options}
                options = [o for o in ref_options if o.get("value") not in seen] + options
            # options_provider: call a function directly
            elif fschema.get("options_provider"):
                resolved = self._resolve_options(fschema)
                extra = resolved.get("options", [])
                seen = {o.get("value") for o in options}
                options = [o for o in extra if o.get("value") not in seen] + options
            if options:
                state._item_options[fname] = options

        # Populate listbox
        listbox = self._get_control(xWindow, "lst_%s" % field_name)
        if listbox:
            labels = tuple(
                self._ld_get_item_label(state, item) for item in items)
            listbox.getModel().StringItemList = labels
            if items:
                listbox.selectItemPos(0, True)
                state.selected_idx = 0

            # Attach item selection listener
            listbox.addItemListener(_LDItemListener(state, self))

        # Show first item's details (or blank)
        self._ld_show_detail(state, 0 if items else -1)

        # Wire add/remove/apply buttons
        add_btn = self._get_control(xWindow, "add_%s" % field_name)
        if add_btn:
            add_btn.addActionListener(_LDAddListener(state, self))

        del_btn = self._get_control(xWindow, "del_%s" % field_name)
        if del_btn:
            del_btn.addActionListener(_LDRemoveListener(state, self))

        apply_btn = self._get_control(xWindow, "apply_%s" % field_name)
        if apply_btn:
            apply_btn.addActionListener(_LDApplyListener(state, self))

        log.info("List-detail initialized: %s (%d items)", full_key, len(items))

    def _ld_on_ok(self, xWindow, module_name, field_name):
        """Save list_detail state to config as JSON."""
        full_key = "%s.%s" % (module_name, field_name)
        state = self._ld_states.get(full_key)
        if not state:
            return

        # Save current detail fields to current item
        self._ld_save_current_detail(state)

        # Serialize to JSON
        new_json = json.dumps(state.items, ensure_ascii=False)
        old_json = self._cached_values.get(full_key, "[]")

        config_svc = self._get_config_service()
        if config_svc and new_json != old_json:
            config_svc.set(full_key, new_json, caller_module=None)
            self._cached_values[full_key] = new_json
            log.info("List-detail saved: %s (%d items)",
                     full_key, len(state.items))

    def _ld_on_select(self, state, new_idx):
        """Handle listbox selection change."""
        if state.selected_idx >= 0:
            self._ld_save_current_detail(state, update_label=False)
        state.selected_idx = new_idx
        self._ld_show_detail(state, new_idx)

    def _ld_on_add(self, state):
        """Add a new item."""
        # Save current detail (no label update, we're switching away)
        if state.selected_idx >= 0:
            self._ld_save_current_detail(state, update_label=False)

        # Create item with defaults
        new_item = {}
        for fname, fschema in state.item_fields.items():
            default = fschema.get("default")
            if default is not None:
                new_item[fname] = default
            else:
                new_item[fname] = ""
        if state.name_field in new_item or state.name_field in state.item_fields:
            new_item[state.name_field] = "New"

        state.items.append(new_item)
        idx = len(state.items) - 1

        listbox = self._get_control(state.xWindow, "lst_%s" % state.field_name)
        if listbox:
            listbox.addItem(self._ld_get_item_label(state, new_item), idx)
            listbox.selectItemPos(idx, True)

        state.selected_idx = idx
        self._ld_show_detail(state, idx)

    def _ld_on_remove(self, state):
        """Remove the selected item."""
        idx = state.selected_idx
        if idx < 0 or idx >= len(state.items):
            return

        state.items.pop(idx)

        listbox = self._get_control(state.xWindow, "lst_%s" % state.field_name)
        if listbox:
            listbox.removeItems(idx, 1)

        if state.items:
            new_idx = min(idx, len(state.items) - 1)
            state.selected_idx = new_idx
            if listbox:
                listbox.selectItemPos(new_idx, True)
            self._ld_show_detail(state, new_idx)
        else:
            state.selected_idx = -1
            self._ld_show_detail(state, -1)

    def _ld_show_detail(self, state, idx):
        """Populate detail fields from an item (or clear if idx=-1)."""
        item = state.items[idx] if 0 <= idx < len(state.items) else {}
        has_item = 0 <= idx < len(state.items)

        for fname, fschema in state.item_fields.items():
            ctrl_id = "%s__%s" % (state.field_name, fname)
            ctrl = self._get_control(state.xWindow, ctrl_id)
            if ctrl is None:
                continue

            widget = fschema.get("widget", "text")
            val = item.get(fname, fschema.get("default", ""))

            try:
                if widget == "checkbox":
                    ctrl.getModel().State = 1 if val else 0
                elif widget in ("text", "password"):
                    ctrl.getModel().Text = str(val) if val else ""
                elif widget in ("number", "slider"):
                    ctrl.getModel().Value = float(val) if val is not None else 0
                elif widget == "select":
                    opts = state._item_options.get(fname)
                    sel_schema = dict(fschema, options=opts) if opts else fschema
                    self._populate_select(ctrl, sel_schema, val)
                elif widget == "combo":
                    opts = state._item_options.get(fname)
                    combo_schema = dict(fschema, options=opts) if opts else fschema
                    self._populate_combo(ctrl, combo_schema, val)
                else:
                    ctrl.getModel().Text = str(val) if val else ""

                ctrl.getModel().Enabled = has_item
            except Exception:
                log.exception("Error showing detail field %s", ctrl_id)

    def _ld_save_current_detail(self, state, update_label=True):
        """Save detail fields to the current item.

        Args:
            update_label: If True, also update the listbox display text.
                Set to False when called during selection change to avoid glitch.
        """
        idx = state.selected_idx
        if idx < 0 or idx >= len(state.items):
            return

        item = state.items[idx]

        for fname, fschema in state.item_fields.items():
            ctrl_id = "%s__%s" % (state.field_name, fname)
            ctrl = self._get_control(state.xWindow, ctrl_id)
            if ctrl is None:
                continue

            widget = fschema.get("widget", "text")
            field_type = fschema.get("type", "string")
            try:
                if widget == "select" and fname in state._item_options:
                    read_schema = dict(fschema, options=state._item_options[fname])
                else:
                    read_schema = fschema
                item[fname] = self._read_control(ctrl, widget, field_type, read_schema)
            except Exception:
                log.exception("Error reading detail field %s", ctrl_id)

        if not update_label:
            return

        # Update listbox display text using remove+add to avoid full list reset
        listbox = self._get_control(state.xWindow, "lst_%s" % state.field_name)
        if listbox and idx >= 0:
            new_label = self._ld_get_item_label(state, item)
            try:
                state._updating = True
                listbox.removeItems(idx, 1)
                listbox.addItem(new_label, idx)
                listbox.selectItemPos(idx, True)
            except Exception:
                log.debug("Failed to update listbox text at %d", idx)
            finally:
                state._updating = False

    # ── List-detail label helpers ────────────────────────────────────

    def _ld_get_item_label(self, state, item):
        """Compute the display label for a list_detail item.

        If a label_func is set, call it. Otherwise join all field values
        with " - ".
        """
        if state.label_func:
            try:
                return state.label_func(item)
            except Exception:
                log.debug("label_func failed, falling back to default")

        # Default: join all field values with " - "
        parts = []
        for fname in state.item_fields:
            val = item.get(fname, "")
            if val:
                parts.append(str(val))
        return " - ".join(parts) if parts else "?"

    def _resolve_label_func(self, func_path):
        """Import a label_func from a dotted path. Returns callable or None."""
        if not func_path:
            return None
        try:
            module_path, func_name = func_path.rsplit(":", 1)
            import importlib
            mod = importlib.import_module(module_path)
            return getattr(mod, func_name)
        except Exception:
            log.exception("Failed to resolve label_func: %s", func_path)
            return None

    # ── Helpers ──────────────────────────────────────────────────────

    def _read_control(self, ctrl, widget, field_type, schema):
        """Read the current value from a control."""
        if widget == "checkbox":
            return ctrl.getModel().State == 1
        elif widget in ("text", "password", "textarea", "file", "folder", "combo"):
            return ctrl.getModel().Text or ""
        elif widget in ("number", "slider"):
            raw = ctrl.getModel().Value
            return int(raw) if field_type == "int" else float(raw)
        elif widget == "select":
            return self._read_select(ctrl, schema)
        return ctrl.getModel().Text or ""

    def _detect_module(self, xWindow):
        """Read the hidden __module__ control to find which module this page is for.

        dlg:text controls expose their dlg:value via the Label property on the model.
        """
        try:
            ctrl = xWindow.getControl("__module__")
            if ctrl:
                model = ctrl.getModel()
                # dlg:text (XFixedText) stores dlg:value in Label, not Text
                return getattr(model, "Label", "") or getattr(model, "Text", "")
        except Exception:
            log.exception("_detect_module failed")
        return None

    def _detect_list_detail(self, xWindow):
        """Read the hidden __list_detail__ control. Returns field name or None."""
        try:
            ctrl = xWindow.getControl("__list_detail__")
            if ctrl:
                model = ctrl.getModel()
                return getattr(model, "Label", "") or getattr(model, "Text", "")
        except Exception:
            pass
        return None

    def _detect_inline_modules(self, xWindow):
        """Read the hidden __inline_modules__ control. Returns list of names or None."""
        try:
            ctrl = xWindow.getControl("__inline_modules__")
            if ctrl:
                model = ctrl.getModel()
                raw = getattr(model, "Label", "") or getattr(model, "Text", "")
                if raw:
                    return [n.strip() for n in raw.split(",") if n.strip()]
        except Exception:
            pass
        return None

    def _get_control(self, xWindow, field_name):
        """Get a control by field name, returning None if missing."""
        try:
            return xWindow.getControl(field_name)
        except Exception:
            return None

    def _get_config_service(self):
        """Get the ConfigService from the framework."""
        try:
            from plugin.main import get_services
            services = get_services()
            return services.config if services else None
        except Exception:
            log.exception("Could not get ConfigService")
            return None

    def _get_manifest(self):
        """Get the manifest modules list."""
        try:
            from plugin._manifest import MODULES
            return MODULES
        except ImportError:
            return []

    def _get_module_config(self, manifest, module_name):
        """Find the config dict for a given module name."""
        for m in manifest:
            if m.get("name") == module_name:
                return m.get("config", {})
        return {}

    def _resolve_options(self, schema):
        """If schema has an options_provider, call it to get dynamic options."""
        provider_path = schema.get("options_provider")
        if not provider_path:
            return schema
        try:
            options = self._call_options_provider(provider_path)
            return dict(schema, options=options)
        except Exception:
            log.exception("Failed to resolve options_provider: %s", provider_path)
            return schema

    def _call_options_provider(self, provider_path):
        """Import a module and call a function to get options.

        provider_path format: "plugin.framework.ai:get_text_instance_options"
        The function receives the ServiceRegistry as its argument.
        """
        module_path, func_name = provider_path.rsplit(":", 1)
        import importlib
        mod = importlib.import_module(module_path)
        func = getattr(mod, func_name)
        from plugin.main import get_services
        services = get_services()
        return func(services)

    def _populate_combo(self, ctrl, schema, current_value):
        """Populate a combobox dropdown with options and set text value.

        Note: dlg:combobox dropdown doesn't work in LO Options pages.
        Kept for potential use in standalone dialogs.
        """
        options = schema.get("options", [])
        if options:
            labels = tuple(o.get("label", o.get("value", "")) for o in options)
            ctrl.getModel().StringItemList = labels
        ctrl.setText(str(current_value) if current_value else "")

    def _populate_select(self, ctrl, schema, current_value):
        """Populate a menulist/listbox with options and select current value.

        If current_value is not in the options list, it is appended so that
        custom/unknown values are preserved in the UI.
        """
        options = list(schema.get("options", []))
        if not options:
            return

        values = [o.get("value", "") for o in options]

        # Inject current value if not already in options (preserves custom values)
        if current_value and current_value not in values:
            options.append({"value": current_value, "label": current_value})
            values.append(current_value)

        labels = tuple(o.get("label", o.get("value", "")) for o in options)

        model = ctrl.getModel()
        model.StringItemList = labels

        if current_value in values:
            ctrl.selectItemPos(values.index(current_value), True)
        elif options:
            ctrl.selectItemPos(0, True)

    def _read_select(self, ctrl, schema):
        """Read the selected value from a menulist/listbox."""
        options = schema.get("options", [])
        sel = ctrl.getSelectedItemPos()
        if 0 <= sel < len(options):
            return options[sel].get("value", "")
        return schema.get("default", "")


# ── UNO component registration ──────────────────────────────────────

g_ImplementationHelper = unohelper.ImplementationHelper()
g_ImplementationHelper.addImplementation(
    OptionsHandler,
    OptionsHandler.IMPLE_NAME,
    OptionsHandler.SERVICE_NAMES,
)
