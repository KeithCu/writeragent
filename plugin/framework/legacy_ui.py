"""Legacy UI functions for settings, input, and Eval Dashboard."""
from plugin.framework.uno_helpers import TabListener, is_checkbox_control, get_checkbox_state, set_checkbox_state
from plugin.modules.core.services.config import get_config, get_current_endpoint, populate_combobox_with_lru
import uno

def input_box(ctx, message, title="", default="", x=None, y=None):
    """ Shows input dialog (EditInputDialog.xdl). Returns (result_text, extra_prompt) if OK, else ("", ""). """
    smgr = ctx.getServiceManager()
    pip = ctx.getValueByName("/singletons/com.sun.star.deployment.PackageInformationProvider")
    base_url = pip.getPackageLocation("org.extension.localwriter")
    dp = smgr.createInstanceWithContext("com.sun.star.awt.DialogProvider", ctx)
    dlg = dp.createDialog(base_url + "/LocalWriterDialogs/EditInputDialog.xdl")
    try:
        dlg.getControl("label").getModel().Label = str(message)
        dlg.getControl("edit").getModel().Text = str(default)
        if title:
            dlg.getModel().Title = title
        
        prompt_ctrl = dlg.getControl("prompt_selector")
        current_prompt = get_config(ctx, "additional_instructions", "")
        populate_combobox_with_lru(ctx, prompt_ctrl, current_prompt, "prompt_lru", "")

        dlg.getControl("edit").setFocus()
        dlg.getControl("edit").setSelection(uno.createUnoStruct("com.sun.star.awt.Selection", 0, len(str(default))))
        
        if dlg.execute():
            ret_text = dlg.getControl("edit").getModel().Text
            ret_prompt = prompt_ctrl.getText()
            return ret_text, ret_prompt
        return "", ""
    finally:
        dlg.dispose()

def settings_box(ctx, title="", x=None, y=None):
    from plugin.framework.settings_dialog import get_settings_field_specs, apply_settings_result
    from plugin.modules.core.services.config import get_image_model, populate_image_model_selector, endpoint_from_selector_text, get_api_key_for_endpoint, populate_endpoint_selector, as_bool

    from plugin.framework.logging import agent_log
    import unohelper
    from com.sun.star.awt import XActionListener, XItemListener, XTextListener

    smgr = ctx.getServiceManager()
    field_specs = get_settings_field_specs(ctx)

    pip = ctx.getValueByName("/singletons/com.sun.star.deployment.PackageInformationProvider")
    base_url = pip.getPackageLocation("org.extension.localwriter")
    dp = smgr.createInstanceWithContext("com.sun.star.awt.DialogProvider", ctx)
    dialog_url = base_url + "/LocalWriterDialogs/SettingsDialog.xdl"
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
        for field in field_specs:
            ctrl = dlg.getControl(field["name"])
            if ctrl:
                if field["name"] == "text_model":
                    populate_combobox_with_lru(ctx, ctrl, field["value"], "model_lru", current_endpoint, strict=True)
                elif field["name"] == "image_model":
                    populate_image_model_selector(ctx, ctrl)
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
                                        populate_combobox_with_lru(self._ctx, text_ctrl, get_config(self._ctx, "text_model", "") or get_config(self._ctx, "model", ""), "model_lru", resolved, strict=True)
                                    if image_ctrl:
                                        if get_config(self._ctx, "image_provider", "aihorde") == "endpoint":
                                            populate_combobox_with_lru(self._ctx, image_ctrl, get_image_model(self._ctx), "image_model_lru", resolved, strict=True)
                                        else:
                                            populate_image_model_selector(self._ctx, image_ctrl)
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
                        # Works for comboboxes
                        ctrl.setText(field["value"])
                    else:
                        try:
                            ctrl.getModel().Text = field["value"]
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
    finally:
        dlg.dispose()

def show_eval_dashboard(ctx):
    import unohelper
    from com.sun.star.awt import XActionListener
    from plugin.modules.core.eval_runner import run_benchmark_suite

    smgr = ctx.getServiceManager()
    pip = ctx.getValueByName("/singletons/com.sun.star.deployment.PackageInformationProvider")
    base_url = pip.getPackageLocation("org.extension.localwriter")
    dp = smgr.createInstanceWithContext("com.sun.star.awt.DialogProvider", ctx)
    dlg = dp.createDialog(base_url + "/LocalWriterDialogs/EvalDialog.xdl")

    try:
        endpoint_ctrl = dlg.getControl("endpoint")
        endpoint_ctrl.getModel().Text = str(get_config(ctx, "endpoint", ""))
        
        model_ctrl = dlg.getControl("models")
        current_model = str(get_config(ctx, "text_model", "") or get_config(ctx, "model", ""))
        current_endpoint = str(get_config(ctx, "endpoint", "")).strip()
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
                
                desktop = self.ctx.getServiceManager().createInstanceWithContext("com.sun.star.frame.Desktop", self.ctx)
                doc = desktop.getCurrentComponent()
                
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
