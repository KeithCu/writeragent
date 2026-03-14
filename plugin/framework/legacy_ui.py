# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2024 John Balis
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.
from plugin.framework.uno_helpers import get_desktop, get_active_document, get_extension_url, TabListener, is_checkbox_control, get_checkbox_state, set_checkbox_state, get_optional
from plugin.framework.config import get_config, get_current_endpoint, get_text_model, populate_combobox_with_lru, set_config, update_lru_history
from plugin.framework.logging import init_logging, debug_log, agent_log
from plugin.framework.sqlite_available import HAS_SQLITE
import uno

def input_box(ctx, message, title="", default="", x=None, y=None):
    """ Shows input dialog (EditInputDialog.xdl). Returns (result_text, extra_prompt) if OK, else ("", ""). """
    init_logging(ctx)
    debug_log("input_box: opening Edit Input dialog (message=%r)" % (message[:40] + "..." if len(message) > 40 else message), context="Chat")
    try:
        smgr = ctx.getServiceManager()
        base_url = get_extension_url()
        debug_log("input_box: base_url=%s" % (base_url or ""), context="Chat")
        dp = smgr.createInstanceWithContext("com.sun.star.awt.DialogProvider", ctx)
        dlg_url = base_url + "/WriterAgentDialogs/EditInputDialog.xdl"
        dlg = dp.createDialog(dlg_url)
        debug_log("input_box: dialog created successfully", context="Chat")
    except Exception as e:
        import traceback
        debug_log("input_box: failed to create dialog: %s" % e, context="Chat")
        debug_log("input_box: traceback: %s" % traceback.format_exc(), context="Chat")
        raise
    try:
        dlg.getControl("label").getModel().Label = str(message)
        dlg.getControl("edit").getModel().Text = str(default)
        if title:
            dlg.getModel().Title = title
        
        prompt_ctrl = dlg.getControl("prompt_selector")
        current_prompt = get_config(ctx, "additional_instructions")
        populate_combobox_with_lru(ctx, prompt_ctrl, current_prompt, "prompt_lru", "")

        model_selector = get_optional(dlg, "model_selector")
        if model_selector:
            current_endpoint = get_current_endpoint(ctx)
            current_model = get_text_model(ctx)
            populate_combobox_with_lru(ctx, model_selector, current_model, "model_lru", current_endpoint)

        dlg.getControl("edit").setFocus()
        dlg.getControl("edit").setSelection(uno.createUnoStruct("com.sun.star.awt.Selection", 0, len(str(default))))
        
        debug_log("input_box: showing dialog (execute)", context="Chat")
        if dlg.execute():
            ret_text = dlg.getControl("edit").getModel().Text
            ret_prompt = prompt_ctrl.getText()
            if model_selector:
                chosen = model_selector.getText()
                if chosen:
                    set_config(ctx, "text_model", chosen)
                    update_lru_history(ctx, chosen, "model_lru", get_current_endpoint(ctx))
            debug_log("input_box: user clicked OK, returning (text len=%d)" % len(ret_text or ""), context="Chat")
            return ret_text, ret_prompt
        debug_log("input_box: user cancelled", context="Chat")
        return "", ""
    except Exception as e:
        import traceback
        debug_log("input_box: error while showing or reading dialog: %s" % e, context="Chat")
        debug_log("input_box: traceback: %s" % traceback.format_exc(), context="Chat")
        raise
    finally:
        try:
            dlg.dispose()
        except Exception:
            pass

def settings_box(ctx, title="Settings", x=None, y=None):
    from plugin.framework.settings_dialog import get_settings_field_specs, apply_settings_result
    from plugin.framework.config import get_image_model, get_stt_model, populate_combobox_with_lru, populate_image_model_selector, endpoint_from_selector_text, get_api_key_for_endpoint, populate_endpoint_selector, as_bool

    from plugin.framework.logging import debug_log
    debug_log("settings_box entry", context="Settings")
    import unohelper
    from com.sun.star.awt import XActionListener, XItemListener, XTextListener

    smgr = ctx.getServiceManager()
    debug_log("Calling get_settings_field_specs", context="Settings")
    field_specs = get_settings_field_specs(ctx)
    debug_log(f"get_settings_field_specs returned {len(field_specs)} fields", context="Settings")

    base_url = get_extension_url()
    dp = smgr.createInstanceWithContext("com.sun.star.awt.DialogProvider", ctx)
    dialog_url = base_url + "/WriterAgentDialogs/SettingsDialog.xdl"
    try:
        dlg = dp.createDialog(dialog_url)
    except Exception as e:
        error_msg = getattr(e, "Message", str(e))
        agent_log("legacy_ui:settings_box", "createDialog failed", data={"url": dialog_url, "error": error_msg}, hypothesis_id="H5")
        raise Exception(f"Could not create dialog from {dialog_url}: {error_msg}")

    dlg.getControl("btn_tab_chat").addActionListener(TabListener(dlg, 1))
    dlg.getControl("btn_tab_image").addActionListener(TabListener(dlg, 2))
    
    try:
        from plugin._manifest import MODULES
        
        inline_targets = {}
        for m in MODULES:
            inline_val = m.get("config_inline")
            if not inline_val:
                continue
            target = inline_val if isinstance(inline_val, str) else (m["name"].rsplit(".", 1)[0] if "." in m["name"] else None)
            if target:
                inline_targets[m["name"]] = target

        inline_set = set()
        for name, target in inline_targets.items():
            if target not in inline_targets:
                inline_set.add(name)

        by_name = {m["name"]: m for m in MODULES}
        inline_map = {}
        for name in inline_set:
            target = inline_targets[name]
            inline_map.setdefault(target, []).append((by_name[name], by_name[name].get("config", {})))

        page_num = 3
        for m in MODULES:
            if m["name"] in ("main", "ai") or m["name"] in inline_set:
                continue
            if m["name"] in ("tunnel", "launcher"):
                continue

            config = m.get("config", {})
            children = inline_map.get(m["name"])
            
            if not config and not children:
                continue

            btn_id = f"btn_tab_{m['name'].replace('.', '_')}"
            ctrl = dlg.getControl(btn_id)
            if ctrl:
                ctrl.addActionListener(TabListener(dlg, page_num))
                page_num += 1
    except ImportError:
        pass

    current_endpoint = get_current_endpoint(ctx)

    try:
        from plugin.framework.logging import debug_log
        for field in field_specs:
            debug_log(f"Processing setting field: {field['name']} (options: {'yes' if 'options' in field else 'no'})", context="Settings")
            ctrl = dlg.getControl(field["name"])
            if ctrl:
                if field["name"] == "text_model":
                    populate_combobox_with_lru(ctx, ctrl, field["value"], "model_lru", current_endpoint)
                elif field["name"] == "image_model":
                    populate_image_model_selector(ctx, ctrl)
                elif field["name"] == "stt_model":
                    populate_combobox_with_lru(ctx, ctrl, field["value"], "audio_model_lru", current_endpoint)
                elif field["name"] == "additional_instructions":
                    populate_combobox_with_lru(ctx, ctrl, field["value"], "prompt_lru", "")
                elif field["name"] == "endpoint":
                    populate_endpoint_selector(ctx, ctrl, field["value"])
                    if hasattr(ctrl, "addItemListener"):
                        class EndpointCombinedListener(unohelper.Base, XItemListener, XTextListener):
                            def __init__(self, dialog, context, combo_ctrl):
                                self._dlg = dialog
                                self._ctx = context
                                self._ctrl = combo_ctrl
                            
                            def update_dropdowns(self):
                                try:
                                    resolved = endpoint_from_selector_text(self._ctrl.getText())
                                    if not resolved: return
                                    text_ctrl = self._dlg.getControl("text_model")
                                    image_ctrl = self._dlg.getControl("image_model")
                                    if text_ctrl:
                                        populate_combobox_with_lru(self._ctx, text_ctrl, "", "model_lru", resolved)
                                    if image_ctrl:
                                        if get_config(self._ctx, "image_provider") == "endpoint":
                                            populate_combobox_with_lru(self._ctx, image_ctrl, "", "image_model_lru", resolved)
                                        else:
                                            populate_image_model_selector(self._ctx, image_ctrl)
                                    stt_ctrl = self._dlg.getControl("stt_model")
                                    if stt_ctrl:
                                        populate_combobox_with_lru(self._ctx, stt_ctrl, "", "audio_model_lru", resolved)
                                    api_key_ctrl = self._dlg.getControl("api_key")
                                    if api_key_ctrl:
                                        api_key_ctrl.getModel().Text = get_api_key_for_endpoint(self._ctx, resolved)
                                except Exception:
                                    pass

                            def itemStateChanged(self, ev):
                                try:
                                    idx = getattr(ev, "Selected", -1)
                                    if idx < 0: return
                                    item_text = self._ctrl.getItem(idx)
                                    if item_text:
                                        url = endpoint_from_selector_text(item_text)
                                        if url: self._ctrl.setText(url)
                                        self.update_dropdowns()
                                except Exception:
                                    pass

                            def textChanged(self, ev):
                                self.update_dropdowns()

                            def disposing(self, ev):
                                pass
                        
                        listener = EndpointCombinedListener(dlg, ctx, ctrl)
                        ctrl.addItemListener(listener)
                        if hasattr(ctrl, "addTextListener"):
                            ctrl.addTextListener(listener)
                elif field["name"] == "image_base_size":
                    populate_combobox_with_lru(ctx, ctrl, field["value"], "image_base_size_lru", "")
                else:
                    is_checkbox = is_checkbox_control(ctrl)
                    if field.get("type") == "bool" and is_checkbox:
                        try:
                            set_checkbox_state(ctrl, 1 if as_bool(field["value"]) else 0)
                        except Exception as e:
                            pass
                    elif hasattr(ctrl, "setText"):
                        # Populate options if provided (for select/combo widgets)
                        if "options" in field:
                            opts = field["options"]
                            # For ComboBox/ListBox, we set the items
                            try:
                                # ComboBox/ListBox typically have StringItemList or can be added directly
                                # In SettingsDialog.xdl, select=menulist, combo=combobox
                                labels = tuple(o.get("label", o.get("value", "")) for o in opts)
                                model = ctrl.getModel()
                                if hasattr(model, "StringItemList"):
                                    debug_log(f"Populating {field['name']} with {len(labels)} options: {labels}", context="Settings")
                                    model.StringItemList = labels
                                else:
                                    debug_log(f"Control {field['name']} model does NOT have StringItemList", context="Settings")
                            except Exception as e:
                                debug_log(f"Failed to set StringItemList for {field['name']}: {e}", context="Settings")
                        
                        ctrl.setText(field["value"])
                    else:
                        try:
                            ctrl.getModel().Text = field["value"]
                        except Exception:
                            pass
        if not HAS_SQLITE:
            for name in ("web_cache_max_mb", "web_cache_validity_days"):
                ctrl = get_optional(dlg, name)
                if ctrl:
                    try:
                        ctrl.getModel().Enabled = False
                    except Exception:
                        pass
        dlg.getControl("endpoint").setFocus()

        result = {}
        if dlg.execute():
            for field in field_specs:
                try:
                    ctrl = dlg.getControl(field["name"])
                    if ctrl:
                        if hasattr(ctrl, "getText") and not is_checkbox_control(ctrl):
                            control_text = ctrl.getText()
                        else:
                            try:
                                control_text = ctrl.getModel().Text
                            except Exception:
                                control_text = ""
                        
                        field_type = field.get("type", "text")
                        if field_type == "int":
                            result[field["name"]] = int(control_text) if control_text.isdigit() else control_text
                        elif field_type == "bool":
                            val = as_bool(control_text)
                            if is_checkbox_control(ctrl):
                                val = (get_checkbox_state(ctrl) == 1)
                            result[field["name"]] = val
                        elif field_type == "float":
                            try:
                                result[field["name"]] = float(control_text)
                            except ValueError:
                                result[field["name"]] = control_text
                        else:
                            result[field["name"]] = control_text
                    else:
                        result[field["name"]] = ""
                except Exception:
                    result[field["name"]] = ""

        if result:
            apply_settings_result(ctx, result)
        return result
    except Exception as e:
        from plugin.framework.dialogs import msgbox
        import traceback
        msgbox(ctx, "Error", f"Failed to open Settings: {e}\n\n{traceback.format_exc()}")
        return {}
    finally:
        dlg.dispose()

def show_eval_dashboard(ctx):
    import unohelper
    from com.sun.star.awt import XActionListener
    from plugin.tests.eval_runner import run_benchmark_suite

    smgr = ctx.getServiceManager()
    base_url = get_extension_url()
    dp = smgr.createInstanceWithContext("com.sun.star.awt.DialogProvider", ctx)
    dlg = dp.createDialog(base_url + "/WriterAgentDialogs/EvalDialog.xdl")

    try:
        endpoint_ctrl = dlg.getControl("endpoint")
        endpoint_ctrl.getModel().Text = str(get_config(ctx, "endpoint") or "")
        
        model_ctrl = dlg.getControl("models")
        current_model = str(get_config(ctx, "text_model") or get_config(ctx, "model") or "")
        current_endpoint = str(get_config(ctx, "endpoint") or "").strip()
        populate_combobox_with_lru(ctx, model_ctrl, current_model, "model_lru", current_endpoint)

        class EvalRunListener(unohelper.Base, XActionListener):
            def __init__(self, ctx, dialog, toolkit):
                self.ctx = ctx
                self.dialog = dialog
                self.toolkit = toolkit
                self.is_running = False

            def actionPerformed(self, ev):
                if self.is_running: return
                self.is_running = True
                try:
                    self.run_suite()
                finally:
                    self.is_running = False

            def run_suite(self):
                model_name = self.dialog.getControl("models").getText()
                categories = []
                if self.dialog.getControl("cat_writer").getState(): categories.append("Writer")
                if self.dialog.getControl("cat_calc").getState(): categories.append("Calc")
                if self.dialog.getControl("cat_draw").getState(): categories.append("Draw")
                if self.dialog.getControl("cat_multimodal").getState(): categories.append("Multimodal")
                
                self.dialog.getControl("log_area").setText(f"Starting benchmark for model: {model_name}...\n")
                self.dialog.getControl("status").setText("Running...")
                self.toolkit.processEventsToIdle()
                
                desktop = get_desktop(self.ctx)
                doc = get_active_document(self.ctx)
                
                summary = run_benchmark_suite(self.ctx, doc, model_name, categories)
                
                log_text = f"Benchmarks Complete for {model_name}!\n"
                log_text += f"Passed: {summary['passed']}, Failed: {summary['failed']}\n"
                log_text += f"Total Est. Cost: ${summary['total_cost']:.4f}\n\n Details:\n"
                for res in summary['results']:
                    log_text += f"[{res['status']}] {res['name']} ({res.get('latency', 0):.1f}s)\n"
                
                self.dialog.getControl("log_area").setText(log_text)
                self.dialog.getControl("status").setText("Finished")

            def disposing(self, ev): pass

        toolkit = smgr.createInstanceWithContext("com.sun.star.awt.Toolkit", ctx)
        dlg.getControl("btn_run").addActionListener(EvalRunListener(ctx, dlg, toolkit))
        
        class CloseListener(unohelper.Base, XActionListener):
            def __init__(self, dialog): self.dialog = dialog
            def actionPerformed(self, ev): self.dialog.endDialog(0)
            def disposing(self, ev): pass
        dlg.getControl("btn_close").addActionListener(CloseListener(dlg))

        dlg.execute()
    finally:
        dlg.dispose()
