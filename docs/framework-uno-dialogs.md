# UNO Dialog & Wizard Development Reference

This document is the single reference for building, loading, wiring, and extending **UNO XDL Dialogs, Wizards, and Custom Window Controls** in WriterAgent and LibrePy.

---

## 1. Overview & Architectural Rules

When creating dialogs or multi-step wizards in LibreOffice Python (PyUNO):

1. **XDL Files**: Define UI layouts in XML Description Language (`.xdl` files) under `extension/Dialogs/`.
2. **Context & Loader**: Load dialogs via `load_writeragent_dialog()` or `DialogProvider` using the extension's base URL. **Never** use `vnd.sun.star.script:...?location=application` (causes deadlocks/hangs).
3. **Execution Modes**:
   - **Modal**: `dlg.execute() == 1` (blocks caller until closed; requires `dlg.dispose()` unless closed via Cancel where execute returns False).
   - **Modeless / Non-Blocking**: `dlg.setVisible(True)` with parent container window binding (allows simultaneous document editing).
4. **Listeners**: Always inherit from `BaseActionListener`, `BaseItemListener`, `BaseTextListener`, or `BaseListener` from `plugin.framework.uno_listeners` to prevent exceptions from crashing PyUNO or LibreOffice.
5. **Localization**: Always run `translate_dialog(dlg)` after loading to auto-translate labels/titles via `gettext` (`_()`).
6. **Multi-Page / Wizards**: LibreOffice does **not** support `tabpagecontainer` or `tabpage` in Python extensions reliably. Use `dlg:page` on controls + `dlg.getModel().Step = page_num` with `TabListener` or Wizard Step buttons.
7. **Button graphics**: VCL `PushButton` (`com.sun.star.awt.UnoControlButtonModel`) supports `ImageURL` and `ImagePosition` (e.g. `LeftCenter = 1`) to place icons directly on buttons alongside labels. Settings provider buttons set `ImageURL` and `ImagePosition` directly at runtime via `apply_provider_button_icon`.

---

## 2. Directory & File Layout

| Path / Module | Purpose |
| :--- | :--- |
| [`extension/Dialogs/*.xdl`](../extension/Dialogs/) | XDL XML layout definitions for dialogs and panels |
| [`plugin/chatbot/dialogs.py`](../plugin/chatbot/dialogs.py) | Core dialog helpers: XDL loader, safe getters/setters, message boxes, clipboard |
| [`plugin/chatbot/dialog_views.py`](../plugin/chatbot/dialog_views.py) | High-level dialog view implementations (`SettingsDialog`, `input_box`); re-exports eval dashboard |
| [`plugin/chatbot/eval_dashboard_ui.py`](../plugin/chatbot/eval_dashboard_ui.py) | Eval dashboard XDL (`EvalDashboard`, `show_eval_dashboard`) |
| [`plugin/chatbot/bug_report.py`](../plugin/chatbot/bug_report.py) | GitHub issue URL builder and browser launcher |
| [`plugin/chatbot/config_ui_helpers.py`](../plugin/chatbot/config_ui_helpers.py) | Combobox / Listbox loaders, LRU history, endpoint selectors |
| [`plugin/framework/uno_listeners.py`](../plugin/framework/uno_listeners.py) | Safe base listener classes (`BaseActionListener`, `BaseItemListener`, `BaseTextListener`, etc.) |
| [`plugin/framework/uno_context.py`](../plugin/framework/uno_context.py) | Context resolution (`get_ctx`, `get_desktop`, `get_toolkit`, `get_extension_url`) |
| [`plugin/framework/i18n.py`](../plugin/framework/i18n.py) | Translation wrapper `_()` for localized text |

---

## 3. Catalog of Key UNO Functions & Classes

### 3.1 Dialog Loading & Context Resolution

| Function / Symbol | Location | Purpose & Usage |
| :--- | :--- | :--- |
| `get_ctx()` | `plugin.framework.uno_context` | Resolves the active UNO component context. |
| `get_desktop(ctx=None)` | `plugin.framework.uno_context` | Returns `com.sun.star.frame.Desktop`. |
| `get_toolkit(ctx=None)` | `plugin.framework.uno_context` | Returns `com.sun.star.awt.Toolkit`. |
| `get_extension_url(ctx=None)` | `plugin.framework.uno_context` | Returns the `vnd.sun.star.extension://...` base URL. |
| `load_writeragent_dialog(name, ctx=None)` | `plugin.chatbot.dialogs` | Loads `extension/Dialogs/<name>.xdl`, runs `translate_dialog`, and returns the `UnoControlDialog` instance. |
| `load_writeragent_dialog_detail(name, ctx=None)` | `plugin.chatbot.dialogs` | Returns `(dlg, error_detail)` tuple with deep diagnostics if loading fails. |
| `translate_dialog(dlg)` | `plugin.chatbot.dialogs` | Recursively walks all controls in the dialog and translates `Label`, `Text`, `Title`, `HelpText` using `_()`. |

### 3.2 Safe Control Accessors (Null-Safe & Property Agnostic)

LibreOffice controls often differ in method vs model property names across OS builds and widget types. Always use these safe accessors:

| Function | Location | Description |
| :--- | :--- | :--- |
| `get_optional(dlg, name)` | `plugin.chatbot.dialogs` | Safely gets a control by ID; returns `None` if not present without throwing. |
| `get_control_text(ctrl, default="")` | `plugin.chatbot.dialogs` | Gets text from `.getText()` or `model.Text`. |
| `set_control_text(ctrl, text)` | `plugin.chatbot.dialogs` | Sets text on `.setText()`, `model.Text`, and `model.Label` (ensures FixedText labels update). |
| `is_checkbox_control(ctrl)` | `plugin.chatbot.dialogs` | Checks if a control is a checkbox (`UnoControlCheckBox` or has `State`). |
| `get_checkbox_state(ctrl)` | `plugin.chatbot.dialogs` | Returns `1` (checked) or `0` (unchecked) via `getState()` or `model.State`. |
| `set_checkbox_state(ctrl, value)` | `plugin.chatbot.dialogs` | Sets `1` or `0` on checkbox control / model. |
| `set_control_enabled(ctrl, enabled)` | `plugin.chatbot.dialogs` | Safely enables/disables control via `setEnable()` or `model.Enabled`. |
| `set_control_visible(ctrl, visible)` | `plugin.chatbot.dialogs` | Safely shows/hides control via `setVisible()` or `model.Visible`. |
| `copy_to_clipboard(ctx, text)` | `plugin.chatbot.dialogs` | Sets system clipboard text via `com.sun.star.datatransfer.clipboard.SystemClipboard`. |

### 3.3 Dynamic Control Generation (Programmatic Dialogs)

For building dialogs entirely in code or adding dynamic widgets:

| Function | Location | Description |
| :--- | :--- | :--- |
| `add_dialog_button(model, name, label, x, y, w, h, push_button_type=None, enabled=True)` | `plugin.chatbot.dialogs` | Creates and inserts a `UnoControlButtonModel`. |
| `add_dialog_label(model, name, label, x, y, w, h, multiline=True)` | `plugin.chatbot.dialogs` | Creates and inserts a `UnoControlFixedTextModel`. |
| `add_dialog_edit(model, name, text, x, y, w, h, readonly=False)` | `plugin.chatbot.dialogs` | Creates and inserts a `UnoControlEditModel`. |
| `add_dialog_hyperlink(model, name, label, url, x, y, w, h)` | `plugin.chatbot.dialogs` | Creates and inserts a clickable URL button / hyperlink. |

### 3.4 Pre-Built Dialog Helpers

| Function / Class | Location | Description |
| :--- | :--- | :--- |
| `msgbox(ctx, title, message, *, box_type=1)` | `plugin.chatbot.dialogs` | Native message box (1=INFO, 2=WARNING, 3=ERROR, 4=QUERY). |
| `msgbox_with_copy(ctx, title, message, copy_text)` | `plugin.chatbot.dialogs` | Message box with a "Copy to Clipboard" button. |
| `msgbox_with_report(ctx, title, message, ...)` | `plugin.chatbot.dialogs` | Error message box with a "Report Issue" button opening GitHub. |
| `input_box(ctx, message, title="", default="")` | `plugin.chatbot.dialog_views` | Multi-line text input dialog (`EditInputDialog.xdl`) with prompt/model LRU comboboxes. |
| `show_text_input_dialog(ctx, message, title="", default="")` | `plugin.chatbot.dialogs` | Simple single-line text input dialog (`ShortTextInputDialog.xdl`). |
| `status_dialog(ctx, title, build_status_fn, copy_url_fn=None)` | `plugin.chatbot.dialogs` | Modeless live-updating probe/status dialog (`ServerStatusDialog.xdl`). |

### 3.5 Combobox & Dropdown Populators

| Function | Location | Description |
| :--- | :--- | :--- |
| `populate_combobox_with_lru(ctx, ctrl, current_val, lru_key, endpoint)` | `plugin.chatbot.config_ui_helpers` | Populates dropdown items from recent history + provider catalog with search / fallback. |
| `populate_endpoint_selector(ctx, ctrl, current_endpoint)` | `plugin.chatbot.config_ui_helpers` | Populates the endpoint dropdown with known provider URLs. |
| `update_lru_history(val, lru_key, endpoint, max_items=None)` | `plugin.chatbot.config_ui_helpers` | Appends a selected item to the LRU history in `writeragent.json`. |

### 3.6 Exception-Safe UNO Listeners (`plugin.framework.uno_listeners`)

All listener base classes catch and log exceptions in callbacks, preventing PyUNO bridge crashes:

```python
from plugin.framework.uno_listeners import (
    BaseActionListener,      # override on_action_performed(self, rEvent)
    BaseItemListener,        # override on_item_state_changed(self, rEvent)
    BaseTextListener,        # override on_text_changed(self, rEvent)
    BaseKeyListener,         # override on_key_pressed(self, e), on_key_released(self, e)
    BaseWindowListener,      # override on_window_resized, on_window_moved, on_window_shown, on_window_hidden
    BaseDocumentEventListener,
    BaseCloseListener,
    BaseTerminateListener,
    BaseActivationEventListener, # override on_active_spreadsheet_changed(self, aEvent) for Calc sheets
)
```

---

## 4. Multi-Page Wizard Pattern (Step Switching)

In LibreOffice XDL, create controls with `dlg:page="N"` where `N` is the 1-indexed step number. Controls without `dlg:page` (like Next, Back, Cancel buttons) remain visible on all steps.

### 4.1 XDL Snippet (`WizardDialog.xdl`)

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE dlg:window PUBLIC "-//OpenOffice.org//DTD OfficeDocument 1.0//EN" "dialog.dtd">
<dlg:window xmlns:dlg="http://openoffice.org/2000/dialog"
  dlg:id="WizardDialog" dlg:left="100" dlg:top="50" dlg:width="360" dlg:height="240"
  dlg:closeable="true" dlg:moveable="true" dlg:title="Quick Setup Wizard" dlg:page="1">
  <dlg:bulletinboard>
    <!-- Step 1 Controls -->
    <dlg:text dlg:id="lbl_step1" dlg:page="1" dlg:left="10" dlg:top="10" dlg:width="340" dlg:height="12" dlg:value="Step 1: Choose AI Provider"/>
    <dlg:combobox dlg:id="provider_combo" dlg:page="1" dlg:left="10" dlg:top="30" dlg:width="200" dlg:height="14" dlg:dropdown="true"/>

    <!-- Step 2 Controls -->
    <dlg:text dlg:id="lbl_step2" dlg:page="2" dlg:left="10" dlg:top="10" dlg:width="340" dlg:height="12" dlg:value="Step 2: MCP Server Settings"/>
    <dlg:checkbox dlg:id="chk_mcp" dlg:page="2" dlg:left="10" dlg:top="30" dlg:width="200" dlg:height="12" dlg:value="Enable Local MCP Server"/>

    <!-- Navigation Buttons (Always Visible) -->
    <dlg:button dlg:id="btn_back" dlg:left="150" dlg:top="215" dlg:width="60" dlg:height="16" dlg:value="< Back" dlg:enabled="false"/>
    <dlg:button dlg:id="btn_next" dlg:left="215" dlg:top="215" dlg:width="60" dlg:height="16" dlg:value="Next >"/>
    <dlg:button dlg:id="btn_cancel" dlg:left="285" dlg:top="215" dlg:width="65" dlg:height="16" dlg:value="Cancel"/>
  </dlg:bulletinboard>
</dlg:window>
```

### 4.2 Python Wizard Controller

```python
from plugin.chatbot.dialogs import (
    load_writeragent_dialog,
    get_optional,
    set_control_enabled,
    set_control_text,
    get_control_text,
    get_checkbox_state,
)
from plugin.framework.uno_listeners import BaseActionListener

class QuickSetupWizard:
    def __init__(self, ctx):
        self._ctx = ctx
        self._dlg = load_writeragent_dialog("WizardDialog", ctx=ctx)
        self._current_step = 1
        self._max_steps = 2
        self._wire_events()

    def _wire_events(self):
        btn_next = self._dlg.getControl("btn_next")
        btn_back = self._dlg.getControl("btn_back")
        btn_cancel = self._dlg.getControl("btn_cancel")

        class NextListener(BaseActionListener):
            def __init__(self, wizard):
                self._w = wizard
            def on_action_performed(self, rEvent):
                self._w.next_step()

        class BackListener(BaseActionListener):
            def __init__(self, wizard):
                self._w = wizard
            def on_action_performed(self, rEvent):
                self._w.back_step()

        class CancelListener(BaseActionListener):
            def __init__(self, dialog):
                self._dlg = dialog
            def on_action_performed(self, rEvent):
                self._dlg.endExecute()

        btn_next.addActionListener(NextListener(self))
        btn_back.addActionListener(BackListener(self))
        btn_cancel.addActionListener(CancelListener(self._dlg))

    def _update_step_ui(self):
        self._dlg.getModel().Step = self._current_step
        btn_back = self._dlg.getControl("btn_back")
        btn_next = self._dlg.getControl("btn_next")

        set_control_enabled(btn_back, self._current_step > 1)
        if self._current_step == self._max_steps:
            set_control_text(btn_next, "Finish")
        else:
            set_control_text(btn_next, "Next >")

    def next_step(self):
        if self._current_step < self._max_steps:
            self._current_step += 1
            self._update_step_ui()
        else:
            self.save_and_close()

    def back_step(self):
        if self._current_step > 1:
            self._current_step -= 1
            self._update_step_ui()

    def save_and_close(self):
        # Read values using safe accessors
        # set_config(...)
        self._dlg.endExecute()

    def show(self):
        self._update_step_ui()
        res = self._dlg.execute()
        self._dlg.dispose()
        return res
```

---

## 5. Asynchronous Background Probes in Dialogs

To probe network endpoints (like checking Ollama / LM Studio or testing API keys) without freezing the UI dialog:

```python
from plugin.framework.worker_pool import run_in_background
from plugin.chatbot.dialogs import set_control_text, set_control_enabled

def probe_local_servers_async(dlg, status_ctrl, on_complete=None):
    set_control_text(status_ctrl, "Scanning local servers...")
    
    def _worker():
        # Do network requests / socket connect with short timeout
        found_url = None
        # e.g. check http://localhost:11434/api/tags
        # ...
        
        # Update UI back on main thread or directly via safe setter
        def _update_ui():
            if found_url:
                set_control_text(status_ctrl, f"Found local server: {found_url}")
            else:
                set_control_text(status_ctrl, "No local server detected.")
            if on_complete:
                on_complete(found_url)
                
        # Invoke UI update
        _update_ui()

    run_in_background(_worker)
```

---

## 6. Common Pitfalls & Invariants

1. **Double Dispose Segfault**:
   When `dlg.execute()` returns `False` (user pressed Esc or standard window close X), calling `dlg.dispose()` can segfault LibreOffice on some Linux desktop builds. Always guard `dlg.dispose()` or only call when `execute()` returned `True` (or inside a guarded `finally` block with check).
2. **StringItemList on ComboBox / ListBox**:
   Setting `.Text` on a ComboBox only sets the edit line, not the dropdown list. To set choices, assign a tuple of strings to `ctrl.getModel().StringItemList = tuple(items)`.
3. **Use Extension's Component Context**:
   Always pass `self._ctx` (or `get_ctx()`) to dialog constructors. Never call `uno.getComponentContext()` directly.
4. **AppFont Coordinate Scaling**:
   XDL positions (`dlg:left`, `dlg:top`, `dlg:width`, `dlg:height`) are in LibreOffice AppFont units (derived from system font metrics, ~8pt grid), not pixels.
