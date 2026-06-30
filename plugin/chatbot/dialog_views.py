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
import logging
import threading
import uno
from com.sun.star.awt import XItemListener, XTextListener

from plugin.framework.errors import format_error_payload, UnoObjectError
from plugin.framework.uno_context import get_active_document, get_desktop, get_extension_url, get_toolkit
from plugin.framework.i18n import _
from plugin.framework.config import get_config, get_current_endpoint, set_config, get_config_str, get_config_int, as_bool, parse_int_robust, parse_float_robust
from plugin.framework.client.model_fetcher import get_text_model, get_stt_model, set_text_model
from plugin.framework.logging import init_logging
from plugin.chatbot.config_ui_helpers import populate_combobox_with_lru, update_lru_history
from plugin.chatbot.history_db import HAS_SQLITE

from .listeners import BaseActionListener, BaseListener
from .dialogs import (
    TabListener, is_checkbox_control, get_checkbox_state, set_checkbox_state,
    get_optional, set_control_enabled, set_control_text, get_control_text, translate_dialog,
    msgbox, load_writeragent_dialog,
)

log = logging.getLogger(__name__)

_EXTEND_MAX_TOKENS_MIN = 10
_EXTEND_MAX_TOKENS_MAX = 4096
_EDIT_EXTRA_TOKENS_MIN = 0
_EDIT_EXTRA_TOKENS_MAX = 4096


def _load_selection_token_controls(extend_ctrl, edit_extra_ctrl) -> None:
    if extend_ctrl:
        set_control_text(extend_ctrl, str(get_config_int("extend_selection_max_tokens")))
    if edit_extra_ctrl:
        set_control_text(edit_extra_ctrl, str(get_config_int("edit_selection_max_new_tokens")))


def _save_selection_token_controls(extend_ctrl, edit_extra_ctrl) -> None:
    if extend_ctrl:
        try:
            extend_val = parse_int_robust(get_control_text(extend_ctrl))
        except ValueError:
            extend_val = get_config_int("extend_selection_max_tokens")
        extend_val = max(_EXTEND_MAX_TOKENS_MIN, min(_EXTEND_MAX_TOKENS_MAX, extend_val))
        set_config("extend_selection_max_tokens", extend_val)
    if edit_extra_ctrl:
        try:
            edit_val = parse_int_robust(get_control_text(edit_extra_ctrl))
        except ValueError:
            edit_val = get_config_int("edit_selection_max_new_tokens")
        edit_val = max(_EDIT_EXTRA_TOKENS_MIN, min(_EDIT_EXTRA_TOKENS_MAX, edit_val))
        set_config("edit_selection_max_new_tokens", edit_val)


# ── Generic Helpers ──────────────────────────────────────────────────

def input_box(ctx, message, title="", default="", x=None, y=None):
    """Shows input dialog (EditInputDialog.xdl). Returns (result_text, extra_prompt) if OK, else ("", "")."""
    init_logging(ctx)
    log.debug("input_box: opening Edit Input dialog")
    try:
        smgr = ctx.getServiceManager()
        base_url = get_extension_url()
        dp = smgr.createInstanceWithContext("com.sun.star.awt.DialogProvider", ctx)
        dlg_url = base_url + "/WriterAgentDialogs/EditInputDialog.xdl"
        dlg = dp.createDialog(dlg_url)
    except Exception as e:
        log.error("input_box: failed to create dialog: %s", e)
        raise UnoObjectError(f"Failed to create dialog: {e}") from e

    need_dispose = True
    try:
        translate_dialog(dlg)

        dlg.getControl("label").getModel().Label = str(message)
        set_control_text(dlg.getControl("edit"), str(default))
        if title:
            dlg.getModel().Title = title

        prompt_ctrl = dlg.getControl("prompt_selector")
        current_prompt = get_config_str("additional_instructions")
        populate_combobox_with_lru(ctx, prompt_ctrl, current_prompt, "prompt_lru", "")

        model_selector = get_optional(dlg, "model_selector")
        if model_selector:
            current_endpoint = get_current_endpoint()
            current_model = get_text_model()
            populate_combobox_with_lru(ctx, model_selector, current_model, "model_lru", current_endpoint)

        extend_tokens_ctrl = get_optional(dlg, "extend_max_tokens")
        extra_tokens_ctrl = get_optional(dlg, "edit_extra_tokens")
        _load_selection_token_controls(extend_tokens_ctrl, extra_tokens_ctrl)

        dlg.getControl("edit").setFocus()
        dlg.getControl("edit").setSelection(uno.createUnoStruct("com.sun.star.awt.Selection", 0, len(str(default))))

        if dlg.execute():
            ret_text = get_control_text(dlg.getControl("edit"))
            ret_prompt = prompt_ctrl.getText()
            if model_selector:
                chosen = model_selector.getText()
                if chosen:
                    set_text_model(chosen, update_lru=True)
            _save_selection_token_controls(extend_tokens_ctrl, extra_tokens_ctrl)
            return ret_text, ret_prompt
        # ESC/close: execute() returned false — skip dispose in finally (double dispose segfaults LO).
        need_dispose = False
        return "", ""
    except Exception as e:
        log.error("input_box error: %s", e)
        raise UnoObjectError(f"Error in input_box: {e}") from e
    finally:
        if need_dispose:
            dlg.dispose()


class SettingsDialog:
    """Manages the lifecycle of the WriterAgent Settings dialog."""

    def __init__(self, ctx):
        self._ctx = ctx
        self._dlg = None
        self._endpoint_listener = None
        self._api_key_listener = None
        self._scripting_venv_test_listener = None
        self._ppt_master_data_test_listener = None

    def show(self):
        """Execute the settings dialog and apply results."""
        from .settings_dialog import get_settings_field_specs, apply_settings_result

        log.debug("SettingsDialog.show entry")
        init_logging(self._ctx)

        try:
            self._create_dialog()
            if self._dlg is None:
                return {}
                
            field_specs = get_settings_field_specs(self._ctx)
            current_endpoint = get_current_endpoint()

            self._setup_tabs()
            self._populate_fields(field_specs, current_endpoint)
            self._schedule_initial_models_fetch(current_endpoint)
            self._apply_sqlite_restrictions()
            
            translate_dialog(self._dlg)
            try:
                self._dlg.getModel().Title = _("Settings")
            except Exception:
                pass

            self._dlg.getControl("endpoint").setFocus()

            if self._dlg.execute():
                result = self._extract_results(field_specs)
                if result:
                    apply_settings_result(self._ctx, result)
                    return result
            return {}
        except Exception as e:
            log.exception("Failed to open Settings")
            msgbox(self._ctx, _("Error"), _("Failed to open Settings: {0}").format(e))
            return format_error_payload(e)
        finally:
            self._cleanup()

    def _create_dialog(self):
        smgr = self._ctx.getServiceManager()
        base_url = get_extension_url()
        dp = smgr.createInstanceWithContext("com.sun.star.awt.DialogProvider", self._ctx)
        dialog_url = base_url + "/WriterAgentDialogs/SettingsDialog.xdl"
        self._dlg = dp.createDialog(dialog_url)

    def _setup_tabs(self):
        assert self._dlg is not None
        self._dlg.getControl("btn_tab_chat").addActionListener(TabListener(self._dlg, 1))
        self._dlg.getControl("btn_tab_image").addActionListener(TabListener(self._dlg, 2))
        
        edit_config_btn = get_optional(self._dlg, "btn_edit_config_json")
        if edit_config_btn:
            edit_config_btn.addActionListener(EditConfigListener(self._ctx))

        self._setup_module_tabs()
        test_venv_btn = get_optional(self._dlg, "scripting__test_venv")
        if test_venv_btn:
            self._scripting_venv_test_listener = ScriptingVenvTestListener(self._ctx, self._dlg)
            test_venv_btn.addActionListener(self._scripting_venv_test_listener)

        test_ppt_btn = get_optional(self._dlg, "scripting__test_ppt_master_data")
        if test_ppt_btn:
            self._ppt_master_data_test_listener = PptMasterDataTestListener(self._ctx, self._dlg)
            test_ppt_btn.addActionListener(self._ppt_master_data_test_listener)

    def _setup_module_tabs(self):
        try:
            # Register module tabs in the Settings dialog
            setup_module_tabs(self._dlg)
        except Exception:
            pass

    def _api_key_from_field_specs(self, field_specs):
        for field in field_specs:
            if field.get("name") == "api_key":
                return str(field.get("value") or "")
        return ""

    def _populate_fields(self, field_specs, current_endpoint):
        assert self._dlg is not None
        from plugin.chatbot.config_ui_helpers import (
            populate_combobox_with_lru, populate_image_model_selector, populate_endpoint_selector
        )

        api_key_val = self._api_key_from_field_specs(field_specs)

        for field in field_specs:
            ctrl = self._dlg.getControl(field["name"])
            if not ctrl:
                continue

            name = field["name"]
            val = field["value"]

            if name == "text_model":
                populate_combobox_with_lru(
                    self._ctx, ctrl, val, "model_lru", current_endpoint, api_key_override=api_key_val,
                )
            elif name == "image_model":
                populate_image_model_selector(
                    self._ctx, ctrl, override_endpoint=current_endpoint, api_key_override=api_key_val,
                )
            elif name == "stt_model":
                populate_combobox_with_lru(
                    self._ctx, ctrl, val, "audio_model_lru", current_endpoint, api_key_override=api_key_val,
                )
            elif name == "additional_instructions":
                populate_combobox_with_lru(self._ctx, ctrl, val, "prompt_lru", "")
            elif name == "endpoint":
                populate_endpoint_selector(self._ctx, ctrl, val)
                self._setup_endpoint_listener(ctrl)
            elif name == "image_base_size":
                populate_combobox_with_lru(self._ctx, ctrl, val, "image_base_size_lru", "")
            else:
                self._populate_generic_field(ctrl, field)

    def _schedule_initial_models_fetch(self, endpoint):
        """OpenRouter/Together skip inline fetch; load full catalog when a saved key exists."""
        from plugin.framework.config import get_api_key_for_endpoint
        from plugin.framework.client.model_fetcher import get_provider_from_endpoint

        listener = self._endpoint_listener
        if not listener or not endpoint:
            return
        provider = get_provider_from_endpoint(endpoint)
        if provider not in {"openrouter", "together"}:
            return
        if not str(get_api_key_for_endpoint(endpoint) or "").strip():
            return
        listener._schedule_debounced_models_fetch()

    def _populate_generic_field(self, ctrl, field):
        if is_checkbox_control(ctrl):
            set_checkbox_state(ctrl, 1 if as_bool(field["value"]) else 0)
        elif hasattr(ctrl, "setText"):
            if "options" in field:
                self._set_ctrl_options(ctrl, field)
            ctrl.setText(str(field.get("value", "")))
        else:
            set_control_text(ctrl, field["value"])

    def _set_ctrl_options(self, ctrl, field):
        try:
            opts = field["options"]
            labels = tuple(o.get("label", o.get("value", "")) for o in opts if isinstance(o, dict))
            model = ctrl.getModel()
            if hasattr(model, "StringItemList"):
                model.StringItemList = labels
        except Exception as e:
            log.error(f"Failed to set options for {field['name']}: {e}")

    def _setup_endpoint_listener(self, ctrl):
        if hasattr(ctrl, "addItemListener"):
            self._endpoint_listener = EndpointCombinedListener(self._dlg, self._ctx, ctrl)
            ctrl.addItemListener(self._endpoint_listener)
            if hasattr(ctrl, "addTextListener"):
                ctrl.addTextListener(self._endpoint_listener)

            ak_ctrl = get_optional(self._dlg, "api_key")
            if ak_ctrl and hasattr(ak_ctrl, "addTextListener"):
                self._api_key_listener = ApiKeyTextListener(self._endpoint_listener)
                ak_ctrl.addTextListener(self._api_key_listener)

    def _apply_sqlite_restrictions(self):
        if not HAS_SQLITE:
            for name in (
                "chatbot__web_cache_max_mb",
                "chatbot__web_cache_validity_days",
                "chatbot__web_research_cache_enabled",
            ):
                ctrl = get_optional(self._dlg, name)
                if ctrl:
                    set_control_enabled(ctrl, False)

    def _extract_results(self, field_specs):
        assert self._dlg is not None
        result = {}
        for field in field_specs:
            name = field["name"]
            ctrl = self._dlg.getControl(name)
            if not ctrl:
                result[name] = ""
                continue

            try:
                if hasattr(ctrl, "getText") and not is_checkbox_control(ctrl):
                    val = ctrl.getText()
                else:
                    val = get_control_text(ctrl)

                field_type = field.get("type", "text")
                if field_type == "int":
                    try:
                        result[name] = parse_int_robust(val)
                    except ValueError:
                        result[name] = val
                elif field_type == "bool":
                    if is_checkbox_control(ctrl):
                        result[name] = get_checkbox_state(ctrl) == 1
                    else:
                        result[name] = as_bool(val)
                elif field_type == "float":
                    try:
                        result[name] = parse_float_robust(val)
                    except ValueError:
                        result[name] = val
                else:
                    result[name] = val
            except Exception as e:
                log.error(f"Failed to extract field {name}: {e}")
        return result

    def _cleanup(self):
        if self._api_key_listener:
            ak = get_optional(self._dlg, "api_key")
            if ak and hasattr(ak, "removeTextListener"):
                ak.removeTextListener(self._api_key_listener)
        if self._endpoint_listener:
            self._endpoint_listener.close()
        if self._scripting_venv_test_listener and self._dlg is not None:
            test_venv_btn = get_optional(self._dlg, "scripting__test_venv")
            if test_venv_btn and hasattr(test_venv_btn, "removeActionListener"):
                try:
                    test_venv_btn.removeActionListener(self._scripting_venv_test_listener)
                except Exception:
                    pass
            self._scripting_venv_test_listener = None
        if self._ppt_master_data_test_listener and self._dlg is not None:
            test_ppt_btn = get_optional(self._dlg, "scripting__test_ppt_master_data")
            if test_ppt_btn and hasattr(test_ppt_btn, "removeActionListener"):
                try:
                    test_ppt_btn.removeActionListener(self._ppt_master_data_test_listener)
                except Exception:
                    pass
            self._ppt_master_data_test_listener = None
        if self._dlg:
            self._dlg.dispose()


def settings_box(ctx, **kwargs):
    """Entry point for settings dialog."""
    return SettingsDialog(ctx).show()


# ── Listeners ────────────────────────────────────────────────────────

class EditConfigListener(BaseActionListener):
    def __init__(self, ctx):
        self._ctx = ctx
    def on_action_performed(self, rEvent):
        from .external_editor import open_writeragent_json_in_editor
        open_writeragent_json_in_editor(self._ctx)


def _dialog_parent_for_child(ctx, parent_dlg):
    """Resolve a parent window for a child modal opened above an executing dialog."""
    if parent_dlg is not None:
        try:
            peer = parent_dlg.getPeer()
            if peer is not None:
                return peer
        except Exception:
            log.debug("parent_dlg.getPeer failed for child modal", exc_info=True)
    try:
        desktop = get_desktop(ctx)
        frame = desktop.getCurrentFrame() if desktop else None
        if frame is not None:
            return frame.getContainerWindow()
    except Exception:
        log.debug("getCurrentFrame parent fallback failed for child modal", exc_info=True)
    return None


class _VenvProbeProgressDialog:
    """Modal progress window for Settings → Python Test (probe runs in a worker thread)."""

    def __init__(self, ctx, parent_dlg=None):
        self._ctx = ctx
        self._parent_dlg = parent_dlg
        self._dlg = None

    def run_modal_probe(self, probe_fn) -> None:
        """Show a modal dialog immediately and run *probe_fn(on_display, on_status)* in a worker."""
        from plugin.framework.queue_executor import post_to_main_thread
        from plugin.framework.worker_pool import run_in_background

        self._create_dialog()

        def on_display(text: str) -> None:
            post_to_main_thread(lambda body=text: self.set_display(body))

        def on_status(text: str) -> None:
            post_to_main_thread(lambda status=text: self.set_status(status))

        def work() -> None:
            try:
                ok, _msg = probe_fn(on_display, on_status)

                def finish_ui() -> None:
                    self.finish(_("Venv OK") if ok else _("Venv check failed"), ok)

                post_to_main_thread(finish_ui)
            except Exception as exc:
                log.exception("Scripting venv probe failed")

                def error_ui(exc=exc) -> None:
                    self.set_display(str(exc))
                    self.finish(_("Venv check failed"), False)

                post_to_main_thread(error_ui)

        run_in_background(work, name="settings-venv-test")
        dlg = self._dlg
        assert dlg is not None
        try:
            dlg.execute()
        finally:
            self._dispose()

    def _create_dialog(self) -> None:
        dlg = load_writeragent_dialog("PythonTestProgressDialog", self._ctx)
        if dlg is None:
            raise RuntimeError("Failed to load PythonTestProgressDialog")
        self._dlg = dlg
        
        # When peer parent is needed for a child modal (aboveSettingsDialog),
        # DialogProvider2 dialogs can be re-parented or we can just use the peer created automatically.
        # Let's add the action listener to BtnClose
        btn_close = dlg.getControl("BtnClose")
        if btn_close is not None:
            btn_close.addActionListener(_VenvProbeCloseListener(self))

    def set_display(self, text: str) -> None:
        if self._dlg is None:
            return
        set_control_text(self._dlg.getControl("LogArea"), text)
        self._pump_events()

    def set_status(self, text: str) -> None:
        if self._dlg is None:
            return
        status = text.strip() or _("Testing Python environment...")
        if len(status) > 80:
            status = status[:77] + "..."
        set_control_text(self._dlg.getControl("StatusLbl"), status)
        self._pump_events()

    def finish(self, title: str, ok: bool) -> None:
        if self._dlg is None:
            return
        try:
            self._dlg.getModel().Title = _(title)
        except Exception:
            pass
        set_control_text(self._dlg.getControl("StatusLbl"), _("Done") if ok else _("Failed"))
        set_control_enabled(self._dlg.getControl("BtnClose"), True)
        self._pump_events()

    def _dispose(self) -> None:
        dlg = self._dlg
        self._dlg = None
        if dlg is None:
            return
        try:
            dlg.dispose()
        except Exception:
            log.debug("Failed to dispose venv probe progress dialog", exc_info=True)

    def _pump_events(self) -> None:
        toolkit = get_toolkit(self._ctx)
        if toolkit and hasattr(toolkit, "processEventsToIdle"):
            toolkit.processEventsToIdle()


class _VenvProbeCloseListener(BaseActionListener):
    def __init__(self, progress: _VenvProbeProgressDialog):
        self._progress = progress

    def on_action_performed(self, rEvent):
        dlg = self._progress._dlg
        if dlg is not None:
            try:
                dlg.endDialog(0)
            except Exception:
                log.debug("Failed to close venv probe progress dialog", exc_info=True)


class ScriptingVenvTestListener(BaseActionListener):
    """Settings → Python: run a quick subprocess check using the path in the text field (saved or not)."""

    def __init__(self, ctx, dlg):
        self._ctx = ctx
        self._dlg = dlg

    def on_action_performed(self, rEvent):
        from plugin.scripting.venv_worker import probe_venv_path_with_progress
        from plugin.scripting.payload_codec import fast_flatten_grid_2d

        path_ctrl = get_optional(self._dlg, "scripting__python_venv_path")
        raw = get_control_text(path_ctrl) if path_ctrl else ""

        host_optimized = fast_flatten_grid_2d is not None
        host_status = "Active (Optimized)" if host_optimized else "Inactive (Pure Python)"

        cython_line = f"Cython Accelerator: {host_status}"

        def probe(on_display, on_status):
            return probe_venv_path_with_progress(
                raw,
                on_display,
                on_status=on_status,
                extra_lines_after_header=(cython_line,),
            )

        progress = _VenvProbeProgressDialog(self._ctx, parent_dlg=self._dlg)
        progress.run_modal_probe(probe)


class PptMasterDataTestListener(BaseActionListener):
    """Settings → Python: verify ppt-master skill tree at the path in the text field (saved or not)."""

    def __init__(self, ctx, dlg):
        self._ctx = ctx
        self._dlg = dlg

    def on_action_performed(self, rEvent):
        from plugin.ppt_master.paths import probe_data_path_with_progress

        path_ctrl = get_optional(self._dlg, "scripting__ppt_master_data_path")
        raw = get_control_text(path_ctrl) if path_ctrl else ""

        def probe(on_display, on_status):
            return probe_data_path_with_progress(raw, on_display, on_status=on_status)

        progress = _VenvProbeProgressDialog(self._ctx, parent_dlg=self._dlg)
        progress.run_modal_probe(probe)


class ApiKeyTextListener(BaseListener, XTextListener):
    def __init__(self, endpoint_listener):
        self._el = endpoint_listener
    def textChanged(self, rEvent):
        self._el._schedule_debounced_models_fetch()


class EndpointCombinedListener(BaseListener, XItemListener, XTextListener):
    def __init__(self, dialog, context, combo_ctrl):
        from plugin.framework.queue_executor import post_to_main_thread
        from plugin.framework.worker_pool import run_in_background
        from plugin.framework.config import get_api_key_for_endpoint
        from plugin.chatbot.config_ui_helpers import (
            populate_combobox_with_lru, populate_image_model_selector, endpoint_from_selector_text,
            _sanitize_model_combobox_value,
        )
        from plugin.framework.client.model_fetcher import (
            endpoint_url_suitable_for_v1_models_fetch, fetch_available_models, fetch_available_image_models,
            get_provider_from_endpoint, get_image_model,
        )

        self._dlg = dialog
        self._ctx = context
        self._ctrl = combo_ctrl
        self._debounce_gen = 0
        self._closed = False
        self._timer = None
        
        self.post_to_main_thread = post_to_main_thread
        self.run_in_background = run_in_background
        self.get_api_key_for_endpoint = get_api_key_for_endpoint
        self.populate_combobox_with_lru = populate_combobox_with_lru
        self.populate_image_model_selector = populate_image_model_selector
        self.endpoint_from_selector_text = endpoint_from_selector_text
        self.endpoint_url_suitable_for_v1_models_fetch = endpoint_url_suitable_for_v1_models_fetch
        self.fetch_available_models = fetch_available_models
        self.fetch_available_image_models = fetch_available_image_models
        self._sanitize_model_combobox_value = _sanitize_model_combobox_value
        self.get_provider_from_endpoint = get_provider_from_endpoint
        self.get_image_model = get_image_model

    def _live_api_key(self):
        ak_ctrl = get_optional(self._dlg, "api_key")
        return str(get_control_text(ak_ctrl)) if ak_ctrl else ""

    def _apply_dropdowns(self, resolved, models=None, skip_fetch=False):
        api_key_ov = self._live_api_key()
        populate_kw = {"api_key_override": api_key_ov, "skip_remote_fetch": skip_fetch}
        resolved_provider = self.get_provider_from_endpoint(resolved)
        saved_provider = self.get_provider_from_endpoint(get_current_endpoint())
        same_provider = bool(resolved_provider and resolved_provider == saved_provider)

        text_ctrl = get_optional(self._dlg, "text_model")
        if text_ctrl:
            current = self._sanitize_model_combobox_value(str(text_ctrl.getText() or ""))
            if not current:
                current = get_text_model() if same_provider else ""
            self.populate_combobox_with_lru(
                self._ctx,
                text_ctrl,
                current,
                "model_lru",
                resolved,
                remote_models=models,
                **populate_kw,
            )

        stt_ctrl = get_optional(self._dlg, "stt_model")
        if stt_ctrl:
            stt_val = self._sanitize_model_combobox_value(str(stt_ctrl.getText() or ""))
            if not stt_val:
                if same_provider:
                    stt_val = str(get_config("stt_model") or get_stt_model() or "")
                else:
                    stt_val = ""
            stt_remote = None if resolved_provider in {"openrouter", "together"} else models
            self.populate_combobox_with_lru(
                self._ctx,
                stt_ctrl,
                stt_val,
                "audio_model_lru",
                resolved,
                remote_models=stt_remote,
                **populate_kw,
            )

        image_ctrl = get_optional(self._dlg, "image_model")
        if image_ctrl:
            image_models = (
                self.fetch_available_image_models(resolved, api_key_override=api_key_ov)
                if models is not None
                else None
            )
            image_val = self._sanitize_model_combobox_value(str(image_ctrl.getText() or ""))
            if not image_val:
                image_val = str(self.get_image_model() or "")
            self.populate_combobox_with_lru(
                self._ctx,
                image_ctrl,
                image_val,
                "image_model_lru",
                resolved,
                remote_models=image_models,
                **populate_kw,
            )

    def close(self):
        self._closed = True
        self._debounce_gen += 1
        if self._timer:
            self._timer.cancel()

    def _sync_api_key(self):
        resolved = self.endpoint_from_selector_text(self._ctrl.getText())
        if not resolved: return
        ak_ctrl = get_optional(self._dlg, "api_key")
        if ak_ctrl:
            set_control_text(ak_ctrl, self.get_api_key_for_endpoint(resolved))

    def _bg_fetch(self, gen, resolved):
        if self._closed or gen != self._debounce_gen: return

        ak_ctrl = get_optional(self._dlg, "api_key")
        key_ov = str(get_control_text(ak_ctrl)) if ak_ctrl else None

        models = None
        if resolved and self.endpoint_url_suitable_for_v1_models_fetch(resolved):
            models = self.fetch_available_models(resolved, api_key_override=key_ov)

        def apply_ui():
            if self._closed or gen != self._debounce_gen: return
            if self.endpoint_from_selector_text(self._ctrl.getText()) != resolved: return
            self._apply_dropdowns(resolved, models=models, skip_fetch=(models is None))

        self.post_to_main_thread(apply_ui)

    def _schedule_debounced_models_fetch(self):
        if self._timer: self._timer.cancel()
        self._debounce_gen += 1
        gen = self._debounce_gen
        self._timer = threading.Timer(1.0, lambda: self.post_to_main_thread(lambda: self._run_fetch(gen)))
        self._timer.daemon = True
        self._timer.start()

    def _run_fetch(self, gen):
        resolved = self.endpoint_from_selector_text(self._ctrl.getText())
        if resolved:
            self.run_in_background(lambda: self._bg_fetch(gen, resolved), name="settings-fetch")

    def textChanged(self, rEvent):
        self._sync_api_key()
        self._schedule_debounced_models_fetch()

    def itemStateChanged(self, rEvent):
        idx = getattr(rEvent, "Selected", -1)
        if idx < 0: return
        item = self._ctrl.getItem(idx)
        if not item: return
        
        url = self.endpoint_from_selector_text(item)
        if url: self._ctrl.setText(url)
        
        if self._timer: self._timer.cancel()
        self._debounce_gen += 1
        resolved = self.endpoint_from_selector_text(self._ctrl.getText())
        if resolved:
            self._sync_api_key()
            provider = self.get_provider_from_endpoint(resolved)
            skip_sync_fetch = provider in {"openrouter", "together"}
            self._apply_dropdowns(resolved, models=None, skip_fetch=skip_sync_fetch)
            self.run_in_background(lambda: self._bg_fetch(self._debounce_gen, resolved), name="settings-select")


# ── Evaluation Dashboard ─────────────────────────────────────────────

class EvalDashboard:
    def __init__(self, ctx):
        self._ctx = ctx
        self._dlg = None

    def show(self):
        smgr = self._ctx.getServiceManager()
        base_url = get_extension_url()
        dp = smgr.createInstanceWithContext("com.sun.star.awt.DialogProvider", self._ctx)
        self._dlg = dp.createDialog(base_url + "/WriterAgentDialogs/EvalDialog.xdl")

        try:
            self._populate()
            if self._dlg:
                self._dlg.execute()
        finally:
            self._dlg.dispose()

    def _populate(self):
        assert self._dlg is not None
        endpoint_ctrl = self._dlg.getControl("endpoint")
        set_control_text(endpoint_ctrl, get_config_str("endpoint"))

        model_ctrl = self._dlg.getControl("models")
        current_model = str(get_text_model())
        current_endpoint = get_config_str("endpoint").strip()
        populate_combobox_with_lru(self._ctx, model_ctrl, current_model, "model_lru", current_endpoint)

        self._dlg.getControl("btn_run").addActionListener(EvalRunListener(self._ctx, self._dlg))
        self._dlg.getControl("btn_close").addActionListener(SimpleCloseListener(self._dlg))


class EvalRunListener(BaseActionListener):
    def __init__(self, ctx, dialog):
        self.ctx = ctx
        self.dialog = dialog
        self.is_running = False

    def on_action_performed(self, rEvent):
        if self.is_running: return
        self.is_running = True
        try:
            self.run_suite()
        finally:
            self.is_running = False

    def run_suite(self):
        from tests.eval_runner import run_benchmark_suite
        toolkit = get_toolkit(self.ctx)
        
        model_name = self.dialog.getControl("models").getText()
        categories = []
        for cat in ("writer", "calc", "draw", "multimodal"):
            if self.dialog.getControl(f"cat_{cat}").getState():
                categories.append(cat.capitalize())

        self.dialog.getControl("log_area").setText(f"Starting benchmark for {model_name}...\n")
        self.dialog.getControl("status").setText("Running...")
        if toolkit:
            toolkit.processEventsToIdle()

        doc = get_active_document(self.ctx)
        summary = run_benchmark_suite(self.ctx, doc, model_name, categories)

        log_text = f"Benchmarks Complete for {model_name}!\n"
        log_text += f"Passed: {summary['passed']}, Failed: {summary['failed']}\n"
        log_text += f"Total Est. Cost: ${summary['total_cost']:.4f}\n\n Details:\n"
        for res in summary["results"]:
            log_text += f"[{res['status']}] {res['name']} ({res.get('latency', 0):.1f}s)\n"

        self.dialog.getControl("log_area").setText(log_text)
        self.dialog.getControl("status").setText("Finished")


class SimpleCloseListener(BaseActionListener):
    def __init__(self, dialog):
        self.dialog = dialog
    def on_action_performed(self, rEvent):
        self.dialog.endDialog(0)


def show_eval_dashboard(ctx):
    EvalDashboard(ctx).show()


# ── Helper for module tabs ───────────────────────────────────────────

def setup_module_tabs(dlg):
    """Register action listeners for module-specific tabs in the Settings dialog."""
    try:
        from plugin._manifest import MODULES

        # Map button ID to step index (starting from 3 for module tabs)
        # Core tabs: 1=Chat, 2=Image
        step = 3
        for m in MODULES:
            m_name = str(m.get("name", ""))
            if m_name in ("main", "ai"):
                continue

            # Skip core modules that don't have user-facing config
            m_config = m.get("config", {})
            if not m_config:
                continue

            has_visible = False
            if not isinstance(m_config, dict):
                continue
            for schema in m_config.values():
                if isinstance(schema, dict) and not schema.get("internal") and schema.get("widget") != "list_detail" and schema.get("settings_persist") is not False:
                    has_visible = True
                    break
            
            if not has_visible:
                continue

            prefix = m_name.replace(".", "_")
            btn_id = f"btn_tab_{prefix}"
            btn = get_optional(dlg, btn_id)
            if btn:
                btn.addActionListener(TabListener(dlg, step))
                step += 1
    except ImportError:
        pass
    except Exception as e:
        log.error(f"Failed to setup module tabs: {e}")
