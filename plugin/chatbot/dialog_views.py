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
from typing import Any

import uno
from com.sun.star.awt import XItemListener, XTextListener

from plugin.framework.errors import format_error_payload, UnoObjectError, ConfigValidationError
from plugin.framework.uno_context import get_desktop, get_extension_url, menu_icon_asset_url
from plugin.framework.i18n import _
from plugin.framework.config import get_config, get_current_endpoint, set_config, get_config_str, get_config_int
from plugin.framework.config_schema import as_bool
from plugin.framework.client.model_fetcher import get_text_model, get_stt_model, set_text_model
from plugin.framework.logging import init_logging
from plugin.chatbot.config_ui_helpers import populate_combobox_with_lru
from plugin.chatbot.history_db import HAS_SQLITE
from plugin.scripting.venv_probe_ui import ScriptingVenvTestListener, VenvProbeProgressDialog

from plugin.framework.uno_listeners import BaseActionListener, BaseListener
from .dialogs import (
    TabListener, is_checkbox_control, get_checkbox_state, set_checkbox_state,
    get_optional, set_control_enabled, set_control_text, get_control_text, translate_dialog,
    msgbox,
)

log = logging.getLogger(__name__)

# PushButton ImageURL with ImagePosition LeftCenter places the icon directly on the button.
_PROVIDER_STARTER_ICONS = {
    "btn_openrouter": "openrouter",
    "btn_together": "together",
    "btn_hf": "huggingface",
    "btn_nvidia": "nvidia",
}
# com.sun.star.awt.ImagePosition.LeftCenter
_IMAGE_POSITION_LEFT_CENTER = 1


def provider_icon_filename(stem):
    """Asset basename under extension/assets/ (openrouter_48.png)."""
    return "%s_48.png" % stem


def apply_provider_button_icon(ctrl, ctx, stem):
    """Load the mark onto a PushButton control model aligned with LeftCenter."""
    filename = provider_icon_filename(stem)
    try:
        ext_url = get_extension_url(ctx)
        if not ext_url:
            return
        model = ctrl.getModel()
        model.ImageURL = menu_icon_asset_url(ext_url, filename)
        try:
            model.ImagePosition = _IMAGE_POSITION_LEFT_CENTER
        except Exception:
            pass
    except Exception:
        log.debug("Provider button icon %s failed", filename, exc_info=True)


def _load_selection_token_controls(extend_ctrl, edit_extra_ctrl) -> None:
    if extend_ctrl:
        set_control_text(extend_ctrl, str(get_config_int("extend_selection_max_tokens")))
    if edit_extra_ctrl:
        set_control_text(edit_extra_ctrl, str(get_config_int("edit_selection_max_new_tokens")))


def _save_selection_token_controls(extend_ctrl, edit_extra_ctrl) -> None:
    if extend_ctrl:
        set_config("extend_selection_max_tokens", get_control_text(extend_ctrl))
    if edit_extra_ctrl:
        set_config("edit_selection_max_new_tokens", get_control_text(edit_extra_ctrl))


# ── Generic Helpers ──────────────────────────────────────────────────

def input_box(ctx, message, title="", default="", x=None, y=None):
    """Shows input dialog (EditInputDialog.xdl). Returns (result_text, extra_prompt) if OK, else ("", "")."""
    init_logging(ctx)
    log.debug("input_box: opening Edit Input dialog")
    try:
        smgr = ctx.getServiceManager()
        base_url = get_extension_url()
        dp = smgr.createInstanceWithContext("com.sun.star.awt.DialogProvider", ctx)
        dlg_url = base_url + "/Dialogs/EditInputDialog.xdl"
        dlg = dp.createDialog(dlg_url)
    except Exception as e:
        log.exception("input_box: failed to create dialog")
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
        log.exception("input_box failed")
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
        self._download_audio_listener = None
        self._copy_mcp_listener = None
        self._test_tunnel_listener = None
        self._mcp_tunnel_enabled_listener = None
        self._mcp_tunnel_provider_listener = None
        self._mcp_port_listener = None

    def show(self):
        """Execute the settings dialog and apply results."""
        from .settings_dialog import get_settings_field_specs, apply_settings_result

        log.debug("SettingsDialog.show entry")
        init_logging(self._ctx)

        try:
            self._create_dialog()
            if self._dlg is None:
                return {}

            set_active_settings_dialog(self._dlg)

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
                    try:
                        apply_settings_result(self._ctx, result)
                        return result
                    except ConfigValidationError as ve:
                        msgbox(self._ctx, _("Invalid Setting"), str(ve))
                        return {}
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
        dialog_url = base_url + "/Dialogs/SettingsDialog.xdl"
        self._dlg = dp.createDialog(dialog_url)

    def _setup_tabs(self):
        assert self._dlg is not None
        self._dlg.getControl("btn_tab_chat").addActionListener(TabListener(self._dlg, 1))
        self._dlg.getControl("btn_tab_image").addActionListener(TabListener(self._dlg, 2))
        
        edit_config_btn = get_optional(self._dlg, "btn_edit_config_json")
        if edit_config_btn:
            edit_config_btn.addActionListener(EditConfigListener(self._ctx))

        starters = [
            ("btn_openrouter", "https://openrouter.ai/api", "https://openrouter.ai/keys"),
            ("btn_together", "https://api.together.xyz", "https://api.together.ai/settings/api-keys"),
            ("btn_hf", "https://api-inference.huggingface.co/v1", "https://huggingface.co/settings/tokens"),
            ("btn_nvidia", "https://integrate.api.nvidia.com/v1", "https://build.nvidia.com/settings/api-keys"),
        ]
        for btn_id, ep_url, signup_url in starters:
            btn = get_optional(self._dlg, btn_id)
            if not btn:
                continue
            starter = ProviderStarterListener(self._ctx, self._dlg, ep_url, signup_url)
            btn.addActionListener(starter)
            stem = _PROVIDER_STARTER_ICONS.get(btn_id)
            if stem:
                apply_provider_button_icon(btn, self._ctx, stem)

        test_conn_btn = get_optional(self._dlg, "btn_test_conn")
        if test_conn_btn:
            test_conn_btn.addActionListener(TestConnectionListener(self._ctx, self._dlg))

        self._setup_module_tabs()
        test_venv_btn = get_optional(self._dlg, "scripting__test_venv")
        if test_venv_btn:
            self._scripting_venv_test_listener = ScriptingVenvTestListener(self._ctx, self._dlg)
            test_venv_btn.addActionListener(self._scripting_venv_test_listener)

        test_ppt_btn = get_optional(self._dlg, "scripting__test_ppt_master_data")
        if test_ppt_btn:
            self._ppt_master_data_test_listener = PptMasterDataTestListener(self._ctx, self._dlg)
            test_ppt_btn.addActionListener(self._ppt_master_data_test_listener)

        download_audio_btn = get_optional(self._dlg, "scripting__download_audio_binaries")
        if download_audio_btn:
            self._download_audio_listener = DownloadAudioListener(self._ctx, self._dlg)
            download_audio_btn.addActionListener(self._download_audio_listener)

        copy_mcp_btn = get_optional(self._dlg, "mcp__copy_config")
        if copy_mcp_btn:
            self._copy_mcp_listener = CopyMcpConfigListener(self._ctx, self._dlg)
            copy_mcp_btn.addActionListener(self._copy_mcp_listener)

        test_tunnel_btn = get_optional(self._dlg, "mcp__test_tunnel")
        if test_tunnel_btn:
            self._test_tunnel_listener = TestTunnelListener(self._ctx, self._dlg)
            test_tunnel_btn.addActionListener(self._test_tunnel_listener)

        port_ctrl = get_optional(self._dlg, "mcp__mcp_port")
        if port_ctrl and hasattr(port_ctrl, "addTextListener"):
            self._mcp_port_listener = McpPortTextListener(self._dlg)
            port_ctrl.addTextListener(self._mcp_port_listener)

        tunnel_enabled_ctrl = get_optional(self._dlg, "mcp__tunnel_enabled")
        if tunnel_enabled_ctrl and hasattr(tunnel_enabled_ctrl, "addItemListener"):
            self._mcp_tunnel_enabled_listener = McpTunnelEnabledListener(self._dlg)
            tunnel_enabled_ctrl.addItemListener(self._mcp_tunnel_enabled_listener)

        provider_ctrl = get_optional(self._dlg, "mcp__tunnel_provider")
        if provider_ctrl:
            self._mcp_tunnel_provider_listener = McpTunnelProviderListener(self._dlg)
            if hasattr(provider_ctrl, "addItemListener"):
                provider_ctrl.addItemListener(self._mcp_tunnel_provider_listener)
            if hasattr(provider_ctrl, "addTextListener"):
                provider_ctrl.addTextListener(self._mcp_tunnel_provider_listener)

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

        # Populate non-persisted client config snippet
        sync_mcp_config_snippet(self._dlg)

    def _schedule_initial_models_fetch(self, endpoint):
        """OpenRouter/Together skip inline fetch; load full catalog when a saved key exists."""
        from plugin.framework.config import get_api_key_for_endpoint
        from plugin.framework.client.provider_detection import get_provider_from_endpoint

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
        except Exception:
            log.exception("Failed to set options for %s", field.get("name"))

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
                if is_checkbox_control(ctrl):
                    result[name] = get_checkbox_state(ctrl) == 1
                elif hasattr(ctrl, "getText"):
                    result[name] = ctrl.getText()
                else:
                    result[name] = get_control_text(ctrl)
            except Exception:
                log.exception("Failed to extract field %s", name)
                result[name] = ""
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
        if self._download_audio_listener and self._dlg is not None:
            download_audio_btn = get_optional(self._dlg, "scripting__download_audio_binaries")
            if download_audio_btn and hasattr(download_audio_btn, "removeActionListener"):
                try:
                    download_audio_btn.removeActionListener(self._download_audio_listener)
                except Exception:
                    pass
            self._download_audio_listener = None
        if self._copy_mcp_listener and self._dlg is not None:
            copy_mcp_btn = get_optional(self._dlg, "mcp__copy_config")
            if copy_mcp_btn and hasattr(copy_mcp_btn, "removeActionListener"):
                try:
                    copy_mcp_btn.removeActionListener(self._copy_mcp_listener)
                except Exception:
                    pass
            self._copy_mcp_listener = None
        if self._test_tunnel_listener and self._dlg is not None:
            test_tunnel_btn = get_optional(self._dlg, "mcp__test_tunnel")
            if test_tunnel_btn and hasattr(test_tunnel_btn, "removeActionListener"):
                try:
                    test_tunnel_btn.removeActionListener(self._test_tunnel_listener)
                except Exception:
                    pass
            self._test_tunnel_listener = None
        if self._mcp_tunnel_enabled_listener and self._dlg is not None:
            tunnel_enabled_ctrl = get_optional(self._dlg, "mcp__tunnel_enabled")
            if tunnel_enabled_ctrl and hasattr(tunnel_enabled_ctrl, "removeItemListener"):
                try:
                    tunnel_enabled_ctrl.removeItemListener(self._mcp_tunnel_enabled_listener)
                except Exception:
                    pass
            self._mcp_tunnel_enabled_listener = None
        if self._mcp_tunnel_provider_listener and self._dlg is not None:
            provider_ctrl = get_optional(self._dlg, "mcp__tunnel_provider")
            if provider_ctrl:
                if hasattr(provider_ctrl, "removeItemListener"):
                    try:
                        provider_ctrl.removeItemListener(self._mcp_tunnel_provider_listener)
                    except Exception:
                        pass
                if hasattr(provider_ctrl, "removeTextListener"):
                    try:
                        provider_ctrl.removeTextListener(self._mcp_tunnel_provider_listener)
                    except Exception:
                        pass
            self._mcp_tunnel_provider_listener = None
        if self._mcp_port_listener and self._dlg is not None:
            port_ctrl = get_optional(self._dlg, "mcp__mcp_port")
            if port_ctrl and hasattr(port_ctrl, "removeTextListener"):
                try:
                    port_ctrl.removeTextListener(self._mcp_port_listener)
                except Exception:
                    pass
            self._mcp_port_listener = None
        clear_active_settings_dialog(self._dlg)
        if self._dlg:
            self._dlg.dispose()


def settings_box(ctx, **kwargs):
    """Entry point for settings dialog."""
    return SettingsDialog(ctx).show()


# ── Listeners ────────────────────────────────────────────────────────

def open_system_url(ctx: Any, url_str: str) -> None:
    """Open URL in default browser via UNO SystemShellExecute."""
    if not url_str:
        return
    try:
        smgr = ctx.getServiceManager()
        shell = smgr.createInstanceWithContext("com.sun.star.system.SystemShellExecute", ctx)
        shell.execute(url_str, "", 0)
    except Exception as e:
        log.warning("Failed to open URL %s: %s", url_str, e)


class EditConfigListener(BaseActionListener):
    def __init__(self, ctx):
        self._ctx = ctx
    def on_action_performed(self, rEvent):
        from .external_editor import open_writeragent_json_in_editor
        open_writeragent_json_in_editor(self._ctx)


class ProviderStarterListener(BaseActionListener):
    """When a provider starter button is clicked, select its endpoint, sync key, and open signup page."""

    def __init__(self, ctx: Any, dlg: Any, endpoint_url: str, signup_url: str):
        self._ctx = ctx
        self._dlg = dlg
        self._endpoint_url = endpoint_url
        self._signup_url = signup_url

    def on_action_performed(self, rEvent: Any) -> None:
        endpoint_ctrl = get_optional(self._dlg, "endpoint")
        if endpoint_ctrl:
            set_control_text(endpoint_ctrl, self._endpoint_url)
            ak_ctrl = get_optional(self._dlg, "api_key")
            if ak_ctrl:
                from plugin.framework.config import get_api_key_for_endpoint
                set_control_text(ak_ctrl, get_api_key_for_endpoint(self._endpoint_url))
                if hasattr(ak_ctrl, "setFocus"):
                    ak_ctrl.setFocus()
        if self._signup_url:
            open_system_url(self._ctx, self._signup_url)


class GetApiKeyListener(BaseActionListener):
    def __init__(self, ctx, dlg):
        self._ctx = ctx
        self._dlg = dlg

    def on_action_performed(self, rEvent):
        from plugin.chatbot.config_ui_helpers import endpoint_from_selector_text, get_signup_url_for_endpoint

        endpoint_ctrl = get_optional(self._dlg, "endpoint")
        endpoint_text = str(get_control_text(endpoint_ctrl)) if endpoint_ctrl else ""
        resolved = endpoint_from_selector_text(endpoint_text)
        signup_url = get_signup_url_for_endpoint(resolved)
        if signup_url:
            open_system_url(self._ctx, signup_url)


class TestConnectionListener(BaseActionListener):
    def __init__(self, ctx, dlg):
        self._ctx = ctx
        self._dlg = dlg

    def on_action_performed(self, rEvent):
        from plugin.chatbot.config_ui_helpers import endpoint_from_selector_text
        from plugin.chatbot.quick_setup import check_endpoint_connection
        from plugin.framework.worker_pool import run_in_background
        from plugin.framework.queue_executor import post_to_main_thread

        btn_test = get_optional(self._dlg, "btn_test_conn")
        lbl_status = get_optional(self._dlg, "lbl_test_status")
        if btn_test:
            set_control_enabled(btn_test, False)
        if lbl_status:
            set_control_text(lbl_status, _("Testing connection..."))

        endpoint_ctrl = get_optional(self._dlg, "endpoint")
        endpoint_text = str(get_control_text(endpoint_ctrl)) if endpoint_ctrl else ""
        endpoint = endpoint_from_selector_text(endpoint_text)

        api_key_ctrl = get_optional(self._dlg, "api_key")
        api_key = str(get_control_text(api_key_ctrl)) if api_key_ctrl else ""

        def _worker():
            msg = check_endpoint_connection(endpoint, api_key)[1]

            def _apply():
                if btn_test:
                    set_control_enabled(btn_test, True)
                if lbl_status:
                    set_control_text(lbl_status, _(msg))

            post_to_main_thread(_apply)

        run_in_background(_worker, name="settings-test-conn")


def _dialog_parent_for_child(ctx, parent_dlg):  # pyright: ignore[reportUnusedFunction]  # settings peer parent helper; used by tests
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

        VenvProbeProgressDialog(self._ctx, parent_dlg=self._dlg).run_modal_probe(probe)


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
        from plugin.framework.client.provider_detection import get_provider_from_endpoint
        from plugin.framework.client.model_fetcher import (
            endpoint_url_suitable_for_v1_models_fetch, fetch_available_models, fetch_available_image_models,
            get_image_model,
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

        resolved_init = self.endpoint_from_selector_text(self._ctrl.getText())
        self._update_key_link_state(resolved_init)

    def _update_key_link_state(self, resolved):
        from plugin.chatbot.config_ui_helpers import get_signup_url_for_endpoint
        url = get_signup_url_for_endpoint(resolved)
        btn_key = get_optional(self._dlg, "btn_get_api_key")
        if btn_key:
            set_control_enabled(btn_key, bool(url))
        lbl_status = get_optional(self._dlg, "lbl_test_status")
        if lbl_status:
            set_control_text(lbl_status, "")

    def _live_api_key(self):
        ak_ctrl = get_optional(self._dlg, "api_key")
        return str(get_control_text(ak_ctrl)) if ak_ctrl else ""

    def _apply_dropdowns(self, resolved, models=None, skip_fetch=False):
        api_key_ov = self._live_api_key()
        skip_remote = bool(skip_fetch)
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
                api_key_override=api_key_ov,
                skip_remote_fetch=skip_remote,
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
                api_key_override=api_key_ov,
                skip_remote_fetch=skip_remote,
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
                api_key_override=api_key_ov,
                skip_remote_fetch=skip_remote,
            )

    def close(self):
        self._closed = True
        self._debounce_gen += 1
        if self._timer:
            self._timer.cancel()

    def _sync_api_key(self):
        resolved = self.endpoint_from_selector_text(self._ctrl.getText())
        self._update_key_link_state(resolved)
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



# ── Helper for module tabs ───────────────────────────────────────────

def setup_module_tabs(dlg):
    """Register action listeners for module-specific tabs in the Settings dialog."""
    try:
        from plugin._manifest import MODULES
        from plugin.chatbot.settings_tab_order import iter_settings_tab_modules

        # Map button ID to step index (starting from 3 for module tabs)
        # Core tabs: 1=Chat, 2=Image
        step = 3
        for m in iter_settings_tab_modules(MODULES):
            m_name = str(m.get("name", ""))
            prefix = m_name.replace(".", "_")
            btn_id = f"btn_tab_{prefix}"
            btn = get_optional(dlg, btn_id)
            if btn:
                btn.addActionListener(TabListener(dlg, step))
                step += 1
    except ImportError:
        pass
    except Exception:
        log.exception("Failed to setup module tabs")


class DownloadAudioListener(BaseActionListener):
    """Settings → Python: download audio binaries and pure Python dependencies from GitHub."""

    def __init__(self, ctx, dlg):
        self._ctx = ctx
        self._dlg = dlg

    def on_action_performed(self, rEvent):
        from plugin.scripting.audio_recorder_service import run_audio_download

        def probe(on_display, on_status):
            ok = run_audio_download(on_display, on_status)
            return ok, ""

        VenvProbeProgressDialog(self._ctx, parent_dlg=self._dlg).run_modal_probe(
            probe, title=_("Audio Library Download")
        )


# ── Evaluation Dashboard ─────────────────────────────────────────────

from plugin.chatbot.eval_dashboard_ui import (
    EvalDashboard,
    EvalRunListener,
    SimpleCloseListener,
    show_eval_dashboard,
)


# ── MCP UI Integration ───────────────────────────────────────────────

from plugin.mcp.mcp_ui import (
    CopyMcpConfigListener,
    McpPortTextListener,
    McpTunnelEnabledListener,
    McpTunnelProviderListener,
    TestTunnelListener,
    _PROVIDER_DEFAULT_URLS,
    _tested_provider_tunnel_urls,
    build_mcp_config_snippet,
    clear_active_settings_dialog,
    notify_tunnel_url_acquired,
    set_active_settings_dialog,
    sync_mcp_config_snippet,
)

__all__ = [
    "CopyMcpConfigListener",
    "DownloadAudioListener",
    "EndpointCombinedListener",
    "EvalDashboard",
    "EvalRunListener",
    "GetApiKeyListener",
    "McpPortTextListener",
    "McpTunnelEnabledListener",
    "McpTunnelProviderListener",
    "ProviderStarterListener",
    "SettingsDialog",
    "SimpleCloseListener",
    "TestConnectionListener",
    "TestTunnelListener",
    "_PROVIDER_DEFAULT_URLS",
    "_tested_provider_tunnel_urls",
    "build_mcp_config_snippet",
    "clear_active_settings_dialog",
    "input_box",
    "notify_tunnel_url_acquired",
    "open_system_url",
    "set_active_settings_dialog",
    "settings_box",
    "setup_module_tabs",
    "show_eval_dashboard",
    "sync_mcp_config_snippet",
]



